from __future__ import annotations

from pathlib import Path
import json

import pytest

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge import work_producer as work_producer_module
from hermes_life_bridge.work_producer import (
    CREDENTIALS_SCHEMA_VERSION,
    REPORT_TOOL_NAME,
    HermesToolEvidenceRegistry,
    HermesWorkProducer,
    LifeRuntimeWorkClient,
    WorkDeclaration,
    WorkIngressReceipt,
    WorkProducerCredentials,
    WorkProducerEvidenceError,
    WorkProducerValidationError,
    report_work_event_handler,
)


def config(tmp_path: Path, **changes: object) -> BridgeConfig:
    values: dict[str, object] = {
        "life_did": "did:example:life",
        "runtime_socket": str(tmp_path / "runtime.sock"),
        "trace_path": str(tmp_path / "trace.jsonl"),
        "work_producer_enabled": True,
        "work_producer_endpoint": "http://127.0.0.1:8791",
        "work_producer_credentials_file": str(tmp_path / "producer.json"),
        "work_producer_timeout_seconds": 0.5,
    }
    values.update(changes)
    return BridgeConfig(**values)


def credentials() -> WorkProducerCredentials:
    return WorkProducerCredentials(
        principal_id="hermes:nancy:work",
        runtime_bearer="runtime-bearer-0123456789abcdef",
        work_bearer="work-bearer-0123456789abcdef",
    )


class FakeClient:
    def __init__(self):
        self.events: list[dict] = []
        self.activities: list[dict] = []

    def post_event(self, event):
        self.events.append(dict(event))
        return WorkIngressReceipt(
            http_status=202,
            status="accepted",
            state="pending_idle",
            reason="awaiting_idle_deadline",
        )

    def post_context_activity(self, activity):
        self.activities.append(dict(activity))
        return WorkIngressReceipt(http_status=202, status="accepted")


def declaration(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "work_id": "hermes:disk-cleanup:001",
        "event_type": "verified_completion",
        "significance": "normal",
        "label": "磁碟空間整理",
        "summary": "整理已完成並通過驗證",
        "metrics": {"reclaimed_storage_gb": 354},
        "evidence_count": 1,
    }
    value.update(changes)
    return value


def test_credentials_file_is_owner_private_and_repr_does_not_expose_tokens(tmp_path: Path) -> None:
    path = tmp_path / "producer.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": CREDENTIALS_SCHEMA_VERSION,
                "principal_id": "hermes:nancy:work",
                "runtime_bearer": "runtime-bearer-0123456789abcdef",
                "work_bearer": "work-bearer-0123456789abcdef",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    loaded = WorkProducerCredentials.from_file(str(path))
    assert loaded.principal_id == "hermes:nancy:work"
    assert "runtime-bearer" not in repr(loaded)
    assert "work-bearer" not in repr(loaded)

    path.chmod(0o640)
    with pytest.raises(WorkProducerValidationError, match="owner_only"):
        WorkProducerCredentials.from_file(str(path))


def test_declaration_semantics_are_closed_and_reject_raw_material() -> None:
    parsed = WorkDeclaration.from_args(declaration())
    assert parsed.event_type == "verified_completion"
    assert parsed.metrics == {"reclaimed_storage_gb": 354}

    with pytest.raises(WorkProducerValidationError):
        WorkDeclaration.from_args(declaration(prompt="PRIVATE"))
    with pytest.raises(WorkProducerValidationError):
        WorkDeclaration.from_args(declaration(summary="結果在 /home/zycas/private.txt"))
    with pytest.raises(WorkProducerValidationError):
        WorkDeclaration.from_args(declaration(summary="通知 feishu:oc_PRIVATE"))
    with pytest.raises(WorkProducerValidationError):
        WorkDeclaration.from_args(declaration(summary="Authorization: Bearer private"))


def test_report_tool_handler_only_validates_and_never_emits() -> None:
    result = json.loads(report_work_event_handler(declaration()))
    assert result["ok"] is True
    assert result["status"] == "declaration_validated"
    assert result["note"] == "emission_requires_matching_current_turn_tool_evidence"

    invalid = json.loads(report_work_event_handler(declaration(summary="`raw shell`")))
    assert invalid == {"error": "invalid_work_event_declaration"}


def test_evidence_registry_keeps_metadata_only_and_matches_event_semantics() -> None:
    registry = HermesToolEvidenceRegistry()
    observed = registry.observe(
        tool_name="terminal",
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="call-001",
    )
    assert observed is not None
    assert observed.evidence_ref.startswith("evidence://hermes/tool/")
    assert registry.snapshot_metadata() == {"contexts": 1, "evidence": 1}
    assert registry.select(
        session_id="session-001",
        turn_id="turn-001",
        event_type="verified_completion",
        count=1,
    )[0].status == "ok"
    with pytest.raises(WorkProducerEvidenceError):
        registry.select(
            session_id="session-001",
            turn_id="turn-001",
            event_type="material_failure",
            count=1,
        )

    registry.observe(
        tool_name="terminal",
        status="error",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="call-002",
    )
    assert registry.select(
        session_id="session-001",
        turn_id="turn-001",
        event_type="material_failure",
        count=1,
    )[0].status == "error"


def test_report_tool_itself_is_never_accepted_as_evidence() -> None:
    registry = HermesToolEvidenceRegistry()
    assert registry.observe(
        tool_name=REPORT_TOOL_NAME,
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="report-call",
    ) is None
    assert registry.snapshot_metadata() == {"contexts": 0, "evidence": 0}


def test_verified_completion_requires_prior_success_and_emits_canonical_event(tmp_path: Path) -> None:
    client = FakeClient()
    producer = HermesWorkProducer(
        config(tmp_path), credentials=credentials(), client=client
    )

    with pytest.raises(WorkProducerEvidenceError):
        producer.observe_post_tool_call(
            tool_name=REPORT_TOOL_NAME,
            args=declaration(),
            status="ok",
            session_id="session-001",
            turn_id="turn-001",
            tool_call_id="report-001",
        )

    producer.observe_post_tool_call(
        tool_name="terminal",
        args=None,
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="terminal-001",
    )
    emitted = producer.observe_post_tool_call(
        tool_name=REPORT_TOOL_NAME,
        args=declaration(),
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="report-001",
    )
    assert emitted.emitted is True
    assert emitted.receipt is not None
    assert emitted.receipt.state == "pending_idle"
    assert len(client.events) == 1
    event = client.events[0]
    assert event["schema_version"] == "work-event.v0.1"
    assert event["producer_id"] == "hermes:nancy:work"
    assert event["work_id"] == "hermes:disk-cleanup:001"
    assert event["event_type"] == "verified_completion"
    assert event["context_ref"].startswith("context:hermes:")
    assert "session-001" not in event["context_ref"]
    assert event["evidence"]["verified"] is True
    assert event["evidence"]["refs"][0].startswith("evidence://hermes/tool/")
    assert event["supersedes_event_id"] is None


def test_material_failure_requires_failed_terminal_evidence(tmp_path: Path) -> None:
    client = FakeClient()
    producer = HermesWorkProducer(
        config(tmp_path), credentials=credentials(), client=client
    )
    producer.observe_post_tool_call(
        tool_name="terminal",
        args=None,
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="terminal-ok",
    )
    with pytest.raises(WorkProducerEvidenceError):
        producer.observe_post_tool_call(
            tool_name=REPORT_TOOL_NAME,
            args=declaration(
                event_type="material_failure",
                label="部署驗證失敗",
                summary="正式驗證遇到需要處理的失敗",
            ),
            status="ok",
            session_id="session-001",
            turn_id="turn-001",
            tool_call_id="report-fail",
        )

    producer.observe_post_tool_call(
        tool_name="terminal",
        args=None,
        status="error",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="terminal-error",
    )
    emitted = producer.observe_post_tool_call(
        tool_name=REPORT_TOOL_NAME,
        args=declaration(
            event_type="material_failure",
            label="部署驗證失敗",
            summary="正式驗證遇到需要處理的失敗",
        ),
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="report-fail",
    )
    assert emitted.emitted is True
    assert client.events[-1]["event_type"] == "material_failure"


def test_context_activity_uses_opaque_context_and_does_not_expose_session(tmp_path: Path) -> None:
    client = FakeClient()
    producer = HermesWorkProducer(
        config(tmp_path), credentials=credentials(), client=client
    )
    producer.note_context_activity("oc_REAL_CHAT_ID_MUST_NOT_LEAK")
    assert len(client.activities) == 1
    activity = client.activities[0]
    assert activity["schema_version"] == "work-context-activity.v0.1"
    assert activity["observer_id"] == "hermes:nancy:work"
    assert activity["context_ref"].startswith("context:hermes:")
    assert "oc_REAL_CHAT_ID_MUST_NOT_LEAK" not in json.dumps(activity)


def test_http_client_rejects_non_loopback_plain_http(tmp_path: Path) -> None:
    with pytest.raises(WorkProducerValidationError, match="loopback"):
        LifeRuntimeWorkClient(
            "http://example.com:8791",
            credentials(),
            timeout_seconds=0.5,
        )


def test_http_client_sends_dual_auth_and_bounded_json_receipt(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit):
            assert limit == 65_537
            return json.dumps(
                {
                    "ok": True,
                    "acceptance": {
                        "status": "accepted",
                        "state": "pending_idle",
                        "reason": "awaiting_idle_deadline",
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(work_producer_module.urlrequest, "urlopen", fake_urlopen)
    client = LifeRuntimeWorkClient(
        "http://127.0.0.1:8791",
        credentials(),
        timeout_seconds=0.5,
    )
    receipt = client.post_event(
        {
            "schema_version": "work-event.v0.1",
            "event_id": "event:hermes:abc",
        }
    )
    assert receipt.status == "accepted"
    assert receipt.state == "pending_idle"
    assert captured["url"] == "http://127.0.0.1:8791/v1/work/events"
    assert captured["headers"]["authorization"].startswith("Bearer ")
    assert captured["headers"]["x-life-work-principal"] == "hermes:nancy:work"
    assert captured["headers"]["x-life-work-token"] == "work-bearer-0123456789abcdef"
    assert captured["body"]["event_id"] == "event:hermes:abc"
    assert captured["timeout"] == 0.5


def test_event_id_is_deterministic_for_same_report_tool_call(tmp_path: Path) -> None:
    client = FakeClient()
    producer = HermesWorkProducer(
        config(tmp_path), credentials=credentials(), client=client
    )
    producer.observe_post_tool_call(
        tool_name="terminal",
        args=None,
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="terminal-001",
    )
    first = producer.observe_post_tool_call(
        tool_name=REPORT_TOOL_NAME,
        args=declaration(),
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="report-001",
    )
    second = producer.observe_post_tool_call(
        tool_name=REPORT_TOOL_NAME,
        args=declaration(),
        status="ok",
        session_id="session-001",
        turn_id="turn-001",
        tool_call_id="report-001",
    )
    assert first.event_id == second.event_id
    assert client.events[0]["idempotency_key"] == client.events[1]["idempotency_key"]
