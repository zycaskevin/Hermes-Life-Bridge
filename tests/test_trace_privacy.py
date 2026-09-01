from hermes_life_bridge.trace import BridgeTracer

def test_trace_strips_raw_content_keys(tmp_path):
    path = tmp_path / "trace.jsonl"
    t = BridgeTracer(str(path))
    t.emit(trace_id="x", stage="HOOK_RECEIVED", message="secret", user_message="secret2", safe="ok")
    text = path.read_text()
    assert "secret" not in text
    assert "secret2" not in text
    assert '"safe": "ok"' in text


def test_trace_strips_route_identity_keys(tmp_path):
    path = tmp_path / "trace.jsonl"
    t = BridgeTracer(str(path))
    t.emit(
        trace_id="route",
        stage="CONTACT_REQUEST_RECEIVED",
        target="feishu:oc_SECRET",
        canonical_target="feishu:oc_SECRET",
        chat_id="oc_SECRET",
        thread_id="th_SECRET",
        target_platform="feishu",
        target_redacted=True,
    )
    text = path.read_text()
    assert "oc_SECRET" not in text
    assert "th_SECRET" not in text
    assert '"target_platform": "feishu"' in text
    assert '"target_redacted": true' in text
