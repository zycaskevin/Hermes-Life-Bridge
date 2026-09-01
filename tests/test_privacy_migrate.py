import json
from hermes_life_bridge.privacy_migrate import sanitize_trace


def test_historical_trace_is_sanitized_without_raw_backup(tmp_path):
    route = tmp_path / "route.json"
    trace = tmp_path / "trace.jsonl"
    route.write_text(json.dumps({
        "platform":"feishu",
        "chat_id":"oc_SECRET",
        "thread_id":"",
        "target":"feishu:oc_SECRET",
    }))
    trace.write_text(
        json.dumps({
            "stage":"HERMES_SEND_START",
            "target":"feishu:oc_SECRET",
            "detail":"provider route feishu:oc_SECRET",
            "target_platform":"feishu",
        }) + "\n"
    )
    result = sanitize_trace(str(route), str(trace))
    assert result["changed"] is True
    assert len(result["original_sha256"]) == 64
    text = trace.read_text()
    assert "oc_SECRET" not in text
    assert "feishu:oc_SECRET" not in text
    row = json.loads(text)
    assert "target" not in row
    assert row["detail"] == "provider route [REDACTED]"
    assert row["target_platform"] == "feishu"
    assert not list(tmp_path.glob("*backup*"))
