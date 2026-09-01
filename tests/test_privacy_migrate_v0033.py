import json
from hermes_life_bridge.privacy_migrate_v0033 import migrate
from hermes_life_bridge.routing import RouteStore

def test_recovers_real_route_after_selftest_overwrite_and_cleans_history(tmp_path):
    state = tmp_path/"state"; state.mkdir()
    route = state/"last_route.json"
    trace = state/"trace.jsonl"
    backup = state/"trace.jsonl.raw-backup"
    attest = state/"privacy-attestation-0033.json"

    route.write_text(json.dumps({"platform":"hlb-selftest","chat_id":"","target":"hlb-selftest"}))
    chat="oc_PRIVATE_123"
    trace.write_text(
        json.dumps({"stage":"HOOK_RECEIVED","platform":f"SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='{chat}')"})+"\n"+
        json.dumps({"stage":"HERMES_SEND_START","target":f"feishu:{chat}","detail":f"send feishu:{chat}"})+"\n"
    )
    backup.write_text(f"old SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='{chat}')\n")

    result=migrate(str(state),str(route),str(trace),str(attest))
    assert result["ok"] is True
    assert result["platform"]=="feishu"
    assert result["raw_backup_retained"] is False
    assert not backup.exists()

    learned=RouteStore(str(route)).load()
    assert learned["platform"]=="feishu"
    assert learned["chat_id"]==chat
    assert learned["target"]==f"feishu:{chat}"
    assert (route.stat().st_mode & 0o777)==0o600

    clean=trace.read_text()
    assert chat not in clean
    assert "SessionSource(" not in clean
    assert f"feishu:{chat}" not in clean

    at=attest.read_text()
    assert chat not in at
    assert f"feishu:{chat}" not in at
    data=json.loads(at)
    assert data["raw_backup_retained"] is False
    assert len(data["recovered_chat_id_sha256"])==64
    assert len(data["trace_files"][0]["pre_redaction_sha256"])==64

def test_fails_closed_without_recoverable_real_route(tmp_path):
    state=tmp_path/"state"; state.mkdir()
    route=state/"last_route.json"; trace=state/"trace.jsonl"; att=state/"att.json"
    route.write_text(json.dumps({"platform":"hlb-selftest","target":"hlb-selftest"}))
    trace.write_text(json.dumps({"platform":"hlb-selftest"})+"\n")
    try:
        migrate(str(state),str(route),str(trace),str(att))
        assert False
    except RuntimeError as exc:
        assert "cannot_recover_real_gateway_route" in str(exc)
    assert not att.exists()
