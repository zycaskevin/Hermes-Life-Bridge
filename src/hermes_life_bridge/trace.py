from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .representation import canonicalize_operational_value


STAGES = (
    "HOOK_RECEIVED",
    "NORMALIZED",
    "DEDUPE_CHECK",
    "SOCKET_CONNECT",
    "EVENT_SENT",
    "RUNTIME_ACK",
    "STATE_ADVANCED",
    "STATE_NOT_ADVANCED",
    "FAILED",
    "CONTACT_REQUEST_RECEIVED",
    "CONTACT_DEDUPE_HIT",
    "CONTACT_DRY_RUN",
    "HERMES_SEND_START",
    "HERMES_SEND_SUCCESS",
    "DELIVERY_RECEIPT_SENT",
    "CONTACT_FAILED",
    "CONTACT_FAILED_SAFE",
    "CONTACT_DELIVERY_UNKNOWN",
    "CONTACT_RECONCILIATION_FAILED",
    "COGNITION_TASK_RECEIVED",
    "COGNITION_DEDUPE_HIT",
    "HERMES_API_CONNECT",
    "HERMES_API_RESPONSE",
    "COGNITIVE_RECEIPT_SENT",
    "COGNITION_FAILED",
)


class BridgeTracer:
    def __init__(
        self,
        path: str,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 3,
    ):
        self.path = Path(path)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1024:
            raise ValueError("trace_max_bytes_must_be_int_at_least_1024")
        if (
            isinstance(backup_count, bool)
            or not isinstance(backup_count, int)
            or backup_count < 1
            or backup_count > 20
        ):
            raise ValueError("trace_backup_count_out_of_range")
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.lock_path = Path(f"{self.path}.lock")

    def _secure(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            path.chmod(0o600)
        except Exception:
            pass

    def _rotate_locked(self, incoming_bytes: int) -> None:
        current_size = self.path.stat().st_size if self.path.exists() else 0
        if current_size == 0 or current_size + incoming_bytes <= self.max_bytes:
            return
        oldest = Path(f"{self.path}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = Path(f"{self.path}.{index}")
            if source.exists():
                source.replace(Path(f"{self.path}.{index + 1}"))
        if self.path.exists():
            self.path.replace(Path(f"{self.path}.1"))
        for index in range(1, self.backup_count + 1):
            self._secure(Path(f"{self.path}.{index}"))

    def emit(
        self,
        *,
        trace_id: str,
        stage: str,
        status: str = "pass",
        **fields: Any,
    ) -> None:
        if stage not in STAGES:
            raise ValueError(f"invalid trace stage: {stage}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "trace_id": trace_id,
            "stage": stage,
            "status": status,
            **fields,
        }
        # Privacy guard: never accept raw message text or exact messaging route.
        forbidden = {
            "text",
            "message",
            "user_message",
            "raw_message",
            "content",
            "target",
            "canonical_target",
            "reply_target",
            "chat_id",
            "thread_id",
        }
        for key in tuple(record):
            if key.lower() in forbidden:
                record.pop(key, None)
        record = canonicalize_operational_value(record)
        encoded = (
            json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            with os.fdopen(lock_fd, "r+b", closefd=False) as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                self._rotate_locked(len(encoded))
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_WRONLY | os.O_APPEND,
                    0o600,
                )
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, encoded)
                finally:
                    os.close(fd)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
        self._secure(self.lock_path)

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > 10000:
            raise ValueError("trace_tail_limit_out_of_range")
        paths = [
            Path(f"{self.path}.{index}")
            for index in range(self.backup_count, 0, -1)
        ] + [self.path]
        lines: list[str] = []
        for path in paths:
            if not path.exists():
                continue
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except Exception:
                continue
        output: list[dict[str, Any]] = []
        for line in lines[-n:]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                output.append(value)
        return output
