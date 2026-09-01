from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.doctor import run_doctor

def test_doctor_reports_missing_socket(tmp_path):
    cfg = BridgeConfig("did:example:life", str(tmp_path/"missing.sock"), str(tmp_path/"trace.jsonl"))
    report = run_doctor(cfg)
    assert report["runtime_socket"]["exists"] is False
    assert report["overall"] == "DEGRADED"
