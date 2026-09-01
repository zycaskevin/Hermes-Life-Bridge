from datetime import datetime, timedelta, timezone
from pathlib import Path
from hermes_life_bridge.cognition_model import CognitiveTaskEnvelope, sha256_text
from hermes_life_bridge.cognition_service import CognitionService
from hermes_life_bridge.config import BridgeConfig

class FakeApi:
    def __init__(self): self.calls = 0
    def cognize(self, *, instruction, task_id):
        self.calls += 1
        return "bounded-result", f"hlb-cognition-{task_id}"

def task():
    now = datetime.now(timezone.utc)
    return CognitiveTaskEnvelope(
        task_id="task-1", idempotency_key="idem-1", life_did="did:test:life", event_id="e1",
        basis_state_sequence=7, basis_state_hash="a"*64, purpose="test", instruction="do bounded test",
        projection_ref="projection://1", projection_hash="b"*64, risk_level="L1",
        created_at=now.isoformat().replace("+00:00","Z"), expires_at=(now+timedelta(minutes=5)).isoformat().replace("+00:00","Z")
    )

def test_cognition_receipt_and_idempotency(tmp_path):
    cfg=BridgeConfig("did:test:life","/tmp/runtime",str(tmp_path/"trace.jsonl"), cognition_socket=str(tmp_path/"c.sock"), cognition_db=str(tmp_path/"c.db"))
    api=FakeApi(); svc=CognitionService(cfg, api_client=api)
    first=svc.process(task()); second=svc.process(task())
    assert first.status == "completed"
    assert first.output_hash == sha256_text("bounded-result")
    assert second.duplicate is True
    assert api.calls == 1

def test_rejects_l2(tmp_path):
    cfg=BridgeConfig("did:test:life","/tmp/runtime",str(tmp_path/"trace.jsonl"), cognition_socket=str(tmp_path/"c.sock"), cognition_db=str(tmp_path/"c.db"))
    svc=CognitionService(cfg, api_client=FakeApi())
    data=task().to_dict(); data["risk_level"]="L2"
    from hermes_life_bridge.cognition_model import CognitiveTaskEnvelope
    import pytest
    with pytest.raises(ValueError, match="risk_requires"):
        svc.process(CognitiveTaskEnvelope(**data))
