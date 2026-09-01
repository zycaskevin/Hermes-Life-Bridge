from datetime import datetime, timedelta, timezone
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_model import ContactDecisionEnvelope, ContactIntentEnvelope
from hermes_life_bridge.contact_service import ContactService

def iso(dt): return dt.isoformat().replace("+00:00","Z")

def intent():
    import hashlib
    msg="hello"
    return ContactIntentEnvelope(
        intent_id="i1",idempotency_key="contact:i1",life_did="did:x",
        basis_state_sequence=1,basis_state_hash="h",source_event_id="e",
        cognitive_receipt_id="c",target="telegram",message_text=msg,
        message_hash=hashlib.sha256(msg.encode()).hexdigest(),utility=.8,urgency=.8,
        evidence_refs=["cognitive:c"],created_at=iso(datetime.now(timezone.utc)),
        expires_at=iso(datetime.now(timezone.utc)+timedelta(minutes=5))
    )

def decision():
    return ContactDecisionEnvelope("d","i1","contact",.8,0,["ok"],iso(datetime.now(timezone.utc)))

class Sender:
    def __init__(self): self.calls=0
    def send(self,**kwargs): self.calls+=1; return "provider-1"

def cfg(tmp_path,enabled=False):
    return BridgeConfig(
        "did:x","/tmp/r",str(tmp_path/"trace.jsonl"),
        contact_socket=str(tmp_path/"contact.sock"),
        contact_db=str(tmp_path/"contact.sqlite3"),
        contact_delivery_enabled=enabled,
        contact_target="telegram",
    )

def test_default_is_dry_run(tmp_path):
    s=Sender(); service=ContactService(cfg(tmp_path,False),sender=s)
    r=service.process(intent(),decision())
    assert r.status=="dry_run"
    assert s.calls==0

def test_enabled_sends_once_and_dedupes(tmp_path):
    s=Sender(); service=ContactService(cfg(tmp_path,True),sender=s)
    r1=service.process(intent(),decision())
    r2=service.process(intent(),decision())
    assert r1.status=="delivered"
    assert r2.duplicate is True
    assert s.calls==1

def test_target_allowlist(tmp_path):
    x=intent()
    from dataclasses import replace
    s=Sender(); service=ContactService(cfg(tmp_path,True),sender=s)
    try:
        service.process(replace(x,target="discord"),decision())
        assert False
    except ValueError as e:
        assert "target_not_allowlisted" in str(e)
