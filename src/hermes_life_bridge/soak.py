from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from typing import Any

from .bridge import HermesLifeBridge
from .config import BridgeConfig
from .doctor import run_doctor
from .maintenance import run_maintenance
from .model import RuntimeReceipt
from .operation_store import OperationStore
from .representation import contains_forbidden_representation_bytes


@dataclass
class _Source:
    platform: str = "feishu"
    chat_id: str = "soak-private-chat"
    thread_id: str = ""
    message_id: str = ""


@dataclass
class _Event:
    source: _Source
    message_id: str
    text: str


class _SoakRuntime:
    def __init__(self):
        self.calls = 0
        self.state_advances = 0
        self.persisted: set[str] = set()

    def send_percept(self, event: dict[str, Any]) -> RuntimeReceipt:
        self.calls += 1
        key = str(event["idempotency_key"])
        duplicate = key in self.persisted
        if not duplicate:
            self.persisted.add(key)
            self.state_advances += 1
        return RuntimeReceipt(
            ok=True,
            duplicate=duplicate,
            persisted=True,
            state_sequence=self.state_advances,
            state_hash=f"soak-{self.state_advances}",
            decision_outcome="defer",
        )


def _file_bytes(path: Path) -> int:
    total = 0
    for candidate in path.parent.glob(f"{path.name}*"):
        if candidate.is_file() and not candidate.name.endswith(".lock"):
            total += candidate.stat().st_size
    return total


def run_accelerated_soak(iterations: int = 2000) -> dict[str, Any]:
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise ValueError("soak_iterations_must_be_int")
    if iterations < 100 or iterations > 100000:
        raise ValueError("soak_iterations_out_of_range")

    started = time.monotonic()
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="hlb-soak-") as root:
        base = Path(root)
        config = BridgeConfig(
            "did:soak:life",
            str(base / "runtime.sock"),
            str(base / "trace.jsonl"),
            operation_db=str(base / "operations.sqlite3"),
            route_path=str(base / "route.json"),
            trace_max_bytes=64 * 1024,
            trace_backup_count=2,
            operation_retention_seconds=1,
        )
        runtime = _SoakRuntime()
        bridge = HermesLifeBridge(config, transport=runtime)
        duplicate_submissions = 0

        for index in range(iterations):
            event = _Event(
                source=_Source(message_id=f"source-{index}"),
                message_id=f"message-{index}",
                text=f"PRIVATE_SOAK_MESSAGE_{index}",
            )
            receipt = bridge.gateway_message(event, session_ref="soak-session")
            if receipt is None or not receipt.ok:
                raise RuntimeError("accelerated_soak_percept_failed")
            if index % 10 == 0:
                duplicate = bridge.gateway_message(event, session_ref="soak-session")
                duplicate_submissions += 1
                if duplicate is None or not duplicate.ok or not duplicate.duplicate:
                    raise RuntimeError("accelerated_soak_duplicate_failed")

        store = bridge.percepts.store
        outbox_remaining = len(store.list_percept_outbox_operation_ids())
        completed_before = len(store.list_operations())
        store.close()

        operation_db = Path(config.operation_db)
        db_bytes_before_maintenance = _file_bytes(operation_db)
        trace_bytes = _file_bytes(Path(config.trace_path))
        trace_limit = config.trace_max_bytes * (config.trace_backup_count + 1)

        # Production maintenance uses age retention. The accelerated harness moves
        # the cutoff forward explicitly so cleanup can be verified without waiting.
        maintenance = run_maintenance(config)
        store_after = OperationStore(config.operation_db)
        accelerated_purged = store_after.purge_terminal(
            before=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            limit=10000,
        )
        if accelerated_purged:
            store_after.compact()
        remaining_operations = len(store_after.list_operations())
        store_after.close()
        db_bytes_after_maintenance = _file_bytes(operation_db)

        forbidden = False
        private_marker = b"PRIVATE_SOAK_MESSAGE_"
        for candidate in (
            Path(config.operation_db),
            Path(config.trace_path),
            Path(f"{config.trace_path}.1"),
            Path(f"{config.trace_path}.2"),
        ):
            if not candidate.exists():
                continue
            raw = candidate.read_bytes()
            forbidden = forbidden or contains_forbidden_representation_bytes(raw)
            if private_marker in raw:
                raise RuntimeError("accelerated_soak_private_message_persisted")

        current_memory, peak_memory = tracemalloc.get_traced_memory()
        elapsed = time.monotonic() - started
        passed = all(
            (
                runtime.state_advances == iterations,
                runtime.calls == iterations,
                outbox_remaining == 0,
                remaining_operations == 0,
                trace_bytes <= trace_limit + config.trace_max_bytes,
                not forbidden,
                peak_memory < 256 * 1024 * 1024,
                bool(maintenance.get("contact_dedupe_retained")),
            )
        )
        report = {
            "ok": passed,
            "iterations": iterations,
            "duplicate_submissions": duplicate_submissions,
            "runtime_calls": runtime.calls,
            "runtime_state_advances": runtime.state_advances,
            "outbox_remaining": outbox_remaining,
            "completed_operations_before_maintenance": completed_before,
            "remaining_operations_after_maintenance": remaining_operations,
            "trace_bytes": trace_bytes,
            "trace_limit_bytes": trace_limit,
            "operation_db_bytes_before_maintenance": db_bytes_before_maintenance,
            "operation_db_bytes_after_maintenance": db_bytes_after_maintenance,
            "memory_current_bytes": current_memory,
            "memory_peak_bytes": peak_memory,
            "forbidden_representation": forbidden,
            "elapsed_seconds": round(elapsed, 3),
        }
    tracemalloc.stop()
    return report


def run_monitor(
    *,
    hours: float,
    interval_seconds: float = 60.0,
    output_path: str,
) -> int:
    if hours <= 0:
        raise ValueError("monitor_hours_must_be_positive")
    if interval_seconds < 10:
        raise ValueError("monitor_interval_too_short")
    config = BridgeConfig.from_env()
    deadline = time.monotonic() + hours * 3600
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with output.open("a", encoding="utf-8") as handle:
        while True:
            report = run_doctor(config)
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "overall": report.get("overall"),
                "operations": report.get("operations", {}),
                "route": report.get("route", {}),
                "components": {
                    name: value.get("status")
                    for name, value in report.get("components", {}).items()
                },
                "sizes": {
                    "operations": _file_bytes(Path(config.operation_db)),
                    "contact": _file_bytes(Path(config.contact_db)),
                    "cognition": _file_bytes(Path(config.cognition_db)),
                    "trace": _file_bytes(Path(config.trace_path)),
                },
            }
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            if report.get("overall") == "BLOCKED":
                failures += 1
            if time.monotonic() >= deadline:
                break
            time.sleep(interval_seconds)
    try:
        output.chmod(0o600)
    except Exception:
        pass
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-life-soak")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--monitor-hours", type=float)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument(
        "--output",
        default=str(Path.home() / ".local/state/hermes-life-bridge/soak-monitor.jsonl"),
    )
    args = parser.parse_args(argv)
    if args.monitor_hours is not None:
        failures = run_monitor(
            hours=args.monitor_hours,
            interval_seconds=args.interval,
            output_path=args.output,
        )
        print(json.dumps({"ok": failures == 0, "blocked_samples": failures}))
        return 0 if failures == 0 else 2
    report = run_accelerated_soak(args.iterations)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
