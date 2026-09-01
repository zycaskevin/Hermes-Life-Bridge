from __future__ import annotations
from pathlib import Path
import json,sys
from .representation import contains_forbidden_representation_bytes

def verify(state_dir:str,route_path:str)->dict:
    state=Path(state_dir); routep=Path(route_path)
    if not routep.exists(): raise RuntimeError("route_store_missing")
    route=json.loads(routep.read_text(encoding="utf-8"))
    if route.get("platform")!="feishu" or not route.get("chat_id"):
        raise RuntimeError("production_route_invalid")
    if (routep.stat().st_mode & 0o777)!=0o600:
        raise RuntimeError("route_store_mode_not_0600")

    scanned=[]
    for p in state.iterdir():
        if not p.is_file(): continue
        # Exact route is allowed only in RouteStore, but implementation repr is not allowed even there.
        raw=p.read_bytes()
        if contains_forbidden_representation_bytes(raw):
            raise RuntimeError(f"forbidden_runtime_representation:{p.name}")
        scanned.append(p.name)
    return {"ok":True,"files_scanned":len(scanned),"route_platform":"feishu","chat_id_present":True}

def main():
    if len(sys.argv)!=3: raise SystemExit("usage: representation_verify_v0035 STATE_DIR ROUTE_FILE")
    print(json.dumps(verify(sys.argv[1],sys.argv[2]),sort_keys=True))

if __name__=="__main__": main()
