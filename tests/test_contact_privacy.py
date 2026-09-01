from datetime import datetime, timedelta, timezone
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_model import ContactDecisionEnvelope, ContactIntentEnvelope
from hermes_life_bridge.contact_service import ContactService
import hashlib, sqlite3

class Sender:
    def send(self,**kwargs): return "m"

def test_store_does_not_persist_raw_message(tmp_path):
    now=datetime.now(timezone.utc)
    msg="PRIVATE CONTACT CONTENT"
    intent=ContactIntentEnvelope(
        "i","contact:i","did:x",1,"h","e","c","telegram",msg,
        hashlib.sha256(msg.encode()).hexdigest(),.8,.8,["ev"],
        now.isoformat().replace("+00:00","Z"),
        (now+timedelta(minutes=5)).isoformat().replace("+00:00","Z")
    )
    decision=ContactDecisionEnvelope("d","i","contact",.8,0,["ok"],now.isoformat().replace("+00:00","Z"))
    db=tmp_path/"c.sqlite3"
    cfg=BridgeConfig("did:x","/tmp/r",str(tmp_path/"t.jsonl"),contact_db=str(db),
                     contact_delivery_enabled=True,contact_target="telegram")
    ContactService(cfg,Sender()).process(intent,decision)
    data=db.read_bytes()
    assert msg.encode() not in data
