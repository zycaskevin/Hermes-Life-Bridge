from __future__ import annotations
from pathlib import Path
import json,re,sys

SESSION_SOURCE_RE=re.compile(r"SessionSource\(",re.I)
CANONICAL_TARGET_RE=re.compile(r"(?:feishu|telegram|discord|slack|signal|sms):[A-Za-z0-9_.@-]+(?::[A-Za-z0-9_.@-]+)?",re.I)
FEISHU_CHAT_RE=re.compile(r"\boc_[A-Za-z0-9_-]+\b")

def verify(state_dir:str,route_path:str,attestation_path:str)->dict:
    state=Path(state_dir); routep=Path(route_path); att=Path(attestation_path)
    if not routep.exists(): raise RuntimeError("route_store_missing")
    route=json.loads(routep.read_text(encoding="utf-8"))
    platform=str(route.get("platform") or "")
    chat=str(route.get("chat_id") or "")
    target=str(route.get("target") or "")
    if platform!="feishu": raise RuntimeError("production_route_not_feishu")
    if not chat or not target: raise RuntimeError("production_route_incomplete")
    if (routep.stat().st_mode & 0o777)!=0o600: raise RuntimeError("route_store_mode_not_0600")
    if not att.exists(): raise RuntimeError("hash_attestation_missing")
    at=att.read_text(encoding="utf-8")
    if chat in at or target in at: raise RuntimeError("attestation_contains_raw_route")

    needles=[x.encode() for x in (chat,target) if x]
    scanned=0
    for p in state.iterdir():
        if not p.is_file() or p in {routep,att}: continue
        scanned+=1
        raw=p.read_bytes()
        for n in needles:
            if n in raw: raise RuntimeError(f"route_identity_outside_private_store:{p.name}")
        text=raw.decode("utf-8",errors="replace")
        if SESSION_SOURCE_RE.search(text): raise RuntimeError(f"sessionsource_repr_remains:{p.name}")
        if CANONICAL_TARGET_RE.search(text): raise RuntimeError(f"canonical_target_remains:{p.name}")
        if FEISHU_CHAT_RE.search(text): raise RuntimeError(f"raw_feishu_chat_id_remains:{p.name}")

    backups=[p.name for p in state.iterdir() if p.is_file() and "trace" in p.name.lower() and p.name!="trace.jsonl" and "attestation" not in p.name.lower() and not p.name.lower().endswith(".sha256")]
    if backups: raise RuntimeError("historical_trace_backup_remains:"+",".join(sorted(backups)))
    return {"ok":True,"platform":platform,"chat_id_present":True,"route_store_mode":"0600","files_scanned":scanned,"raw_trace_backup_retained":False}

def main():
    if len(sys.argv)!=4: raise SystemExit("usage: privacy_verify_v0034 STATE_DIR ROUTE_FILE ATTESTATION")
    print(json.dumps(verify(sys.argv[1],sys.argv[2],sys.argv[3]),sort_keys=True))

if __name__=="__main__": main()
