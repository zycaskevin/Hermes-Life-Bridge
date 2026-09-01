import json
from hermes_life_bridge.representation_verify_v0035 import verify

def test_verifier_rejects_any_runtime_repr(tmp_path):
    route=tmp_path/"last_route.json"
    route.write_text(json.dumps({"platform":"feishu","chat_id":"oc_OK","target":"feishu:oc_OK"}))
    route.chmod(0o600)
    trace=tmp_path/"trace.jsonl"
    trace.write_text("SessionSource(platform=<Platform.FEISHU: 'feishu'>)\n")
    try:
        verify(str(tmp_path),str(route))
        assert False
    except RuntimeError as exc:
        assert "forbidden_runtime_representation" in str(exc)

def test_verifier_accepts_canonical_operational_state(tmp_path):
    route=tmp_path/"last_route.json"
    route.write_text(json.dumps({"platform":"feishu","chat_id":"oc_OK","target":"feishu:oc_OK"}))
    route.chmod(0o600)
    (tmp_path/"trace.jsonl").write_text(json.dumps({"platform":"feishu","target_redacted":True})+"\n")
    assert verify(str(tmp_path),str(route))["ok"] is True
