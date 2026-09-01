import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from hermes_life_bridge.cognition_model import CognitiveTaskEnvelope
from hermes_life_bridge.cognition_service import CognitionService
from hermes_life_bridge.config import BridgeConfig

class FakeApi:
    def __init__(self): self.calls=0
    def cognize(self, *, instruction, task_id):
        self.calls += 1
        return "wire-ok", f"hlb-cognition-{task_id}"

def make_task():
    now=datetime.now(timezone.utc)
    return CognitiveTaskEnvelope(
        task_id="wire-task",idempotency_key="wire-idem",life_did="did:test:life",event_id="e1",
        basis_state_sequence=1,basis_state_hash="a"*64,purpose="wire",instruction="test",
        projection_ref="p://1",projection_hash="b"*64,risk_level="L0",
        created_at=now.isoformat().replace("+00:00","Z"),expires_at=(now+timedelta(minutes=2)).isoformat().replace("+00:00","Z")
    )

async def _roundtrip(tmp_path):
    sock=tmp_path/"c.sock"; api=FakeApi()
    cfg=BridgeConfig("did:test:life","/tmp/r",str(tmp_path/"t.jsonl"),cognition_socket=str(sock),cognition_db=str(tmp_path/"c.db"))
    svc=CognitionService(cfg,api_client=api)
    server=await asyncio.start_unix_server(svc.handle_client,path=str(sock))
    async with server:
        reader,writer=await asyncio.open_unix_connection(str(sock))
        import json
        writer.write((json.dumps(make_task().to_dict())+"\n").encode()); await writer.drain()
        payload=json.loads((await reader.readline()).decode()); writer.close(); await writer.wait_closed()
        assert payload["ok"] is True
        assert payload["receipt"]["output_text"] == "wire-ok"
        assert api.calls == 1

def test_cognition_socket_worker_thread_is_sqlite_safe(tmp_path):
    asyncio.run(_roundtrip(tmp_path))
