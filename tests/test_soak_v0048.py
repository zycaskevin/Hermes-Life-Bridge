from __future__ import annotations

import json
import stat

from hermes_life_bridge import soak


def test_accelerated_soak_proves_bounded_growth_and_idempotency():
    report = soak.run_accelerated_soak(200)
    assert report["ok"] is True
    assert report["runtime_state_advances"] == 200
    assert report["runtime_calls"] == 200
    assert report["duplicate_submissions"] == 20
    assert report["outbox_remaining"] == 0
    assert report["remaining_operations_after_maintenance"] == 0
    assert report["forbidden_representation"] is False
    assert report["memory_peak_bytes"] < 256 * 1024 * 1024
    assert report["trace_bytes"] <= report["trace_limit_bytes"] + 65536
    assert report["operation_db_bytes_after_maintenance"] <= report["operation_db_bytes_before_maintenance"]


def test_monitor_writes_private_summary_without_exact_route(monkeypatch, tmp_path):
    times = iter([0.0, 100.0])
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(soak.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        soak,
        "run_doctor",
        lambda config: {
            "overall": "HEALTHY",
            "operations": {"delivery_unknown": 0},
            "route": {"status": "fresh", "platform": "feishu"},
            "components": {
                "ingress": {"status": "healthy"},
                "cognition": {"status": "healthy"},
                "contact": {"status": "healthy"},
            },
        },
    )
    monkeypatch.setattr(
        soak.BridgeConfig,
        "from_env",
        classmethod(
            lambda cls: cls(
                "did:x",
                "/tmp/runtime.sock",
                str(tmp_path / "trace.jsonl"),
                operation_db=str(tmp_path / "operations.sqlite3"),
                contact_db=str(tmp_path / "contact.sqlite3"),
                cognition_db=str(tmp_path / "cognition.sqlite3"),
            )
        ),
    )
    output = tmp_path / "monitor.jsonl"
    failures = soak.run_monitor(
        hours=0.001,
        interval_seconds=10,
        output_path=str(output),
    )
    assert failures == 0
    data = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert data["overall"] == "HEALTHY"
    assert data["route"] == {"status": "fresh", "platform": "feishu"}
    assert "chat_id" not in json.dumps(data)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
