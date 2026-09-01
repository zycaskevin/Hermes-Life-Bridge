import json, socket, threading
from enum import Enum
from hermes_life_bridge.bridge import HermesLifeBridge
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.routing import RouteStore

class Platform(Enum):
    FEISHU="feishu"

class Source:
    platform=Platform.FEISHU
    chat_id="oc_SECRET"
    thread_id=None
    message_id="om_1"
    def to_dict(self):
        return {"platform":"feishu","chat_id":self.chat_id,"thread_id":None,"message_id":self.message_id}

class Event:
    text="PRIVATE CONTENT"
    source=Source()
    message_id=""

def test_trace_redacts_chat_id_private_store_keeps_route(tmp_path):
    sock=str(tmp_path/"r.sock")
    trace=str(tmp_path/"trace.jsonl")
    route=str(tmp_path/"route.json")
    srv=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); srv.bind(sock); srv.listen(1)
    def worker():
        c,_=srv.accept(); c.recv(8192)
        c.sendall((json.dumps({
            "ok":True,"duplicate":False,"persisted":True,
            "state_sequence":5,"state_hash":"h5","decision":{"outcome":"defer"},
        })+"\n").encode())
        c.close(); srv.close()
    th=threading.Thread(target=worker); th.start()
    cfg=BridgeConfig(life_did="did:x",runtime_socket=sock,trace_path=trace,route_path=route)
    receipt=HermesLifeBridge(cfg).gateway_message(Event(),session_ref="s")
    th.join()
    assert receipt.ok is True
    text=open(trace,encoding="utf-8").read()
    assert "oc_SECRET" not in text
    assert "SessionSource" not in text
    assert RouteStore(route).load()["target"]=="feishu:oc_SECRET"
