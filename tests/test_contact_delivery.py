from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_delivery import HermesSendClient

def test_send_uses_argv_not_shell(monkeypatch):
    seen={}
    class P:
        returncode=0; stdout='{"message_id":"m1"}'; stderr=''
    def fake_run(cmd,**kwargs):
        seen["cmd"]=cmd; seen["kwargs"]=kwargs; return P()
    monkeypatch.setattr("subprocess.run",fake_run)
    cfg=BridgeConfig("did:x","/tmp/r","/tmp/t",hermes_cli_path="hermes")
    c=HermesSendClient(cfg)
    mid=c.send(target="telegram",message='hello; rm -rf /')
    assert mid=="m1"
    assert seen["cmd"]==["hermes","send","--to","telegram","--json",'hello; rm -rf /']
    assert "shell" not in seen["kwargs"]
