import json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.hermes_api import HermesApiClient

class Handler(BaseHTTPRequestHandler):
    seen = {}
    def do_POST(self):
        length=int(self.headers.get("Content-Length","0")); body=self.rfile.read(length)
        Handler.seen={"path":self.path,"headers":dict(self.headers),"body":json.loads(body)}
        payload={"choices":[{"message":{"content":"ok"}}]}
        raw=json.dumps(payload).encode(); self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def log_message(self,*a): pass

def test_hermes_api_uses_isolated_session_without_session_key(tmp_path):
    server=HTTPServer(("127.0.0.1",0),Handler); th=threading.Thread(target=server.serve_forever); th.start()
    try:
        cfg=BridgeConfig("did:test:life","/tmp/r",str(tmp_path/"t"), hermes_api_base_url=f"http://127.0.0.1:{server.server_port}", hermes_api_key="secret")
        text, sid=HermesApiClient(cfg).cognize(instruction="test",task_id="t1")
        assert text == "ok" and sid == "hlb-cognition-t1"
        headers={k.lower():v for k,v in Handler.seen["headers"].items()}
        assert headers["x-hermes-session-id"] == "hlb-cognition-t1"
        assert "x-hermes-session-key" not in headers
        assert headers["authorization"] == "Bearer secret"
    finally:
        server.shutdown(); th.join()
