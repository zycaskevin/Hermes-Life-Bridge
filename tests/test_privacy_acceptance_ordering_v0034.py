import json
from hermes_life_bridge.privacy_migrate_v0033 import migrate
from hermes_life_bridge.privacy_verify_v0034 import verify

def test_quiesced_resanitize_closes_late_writer_race(tmp_path):
    state=tmp_path/"state"; state.mkdir()
    route=state/"last_route.json"; trace=state/"trace.jsonl"; att=state/"privacy-attestation-hlb0033.json"
    chat="oc_RACE_PRIVATE"
    route.write_text(json.dumps({"platform":"hlb-selftest","chat_id":"","target":"hlb-selftest"}))
    trace.write_text(json.dumps({"target":f"feishu:{chat}"})+"\n")
    migrate(str(state),str(route),str(trace),str(att))
    with trace.open("a",encoding="utf-8") as f:
        f.write(f"SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='{chat}')\n")
    try:
        verify(str(state),str(route),str(att))
        assert False
    except RuntimeError:
        pass
    migrate(str(state),str(route),str(trace),str(att))
    result=verify(str(state),str(route),str(att))
    assert result["ok"] is True
    text=trace.read_text()
    assert chat not in text and "SessionSource(" not in text
