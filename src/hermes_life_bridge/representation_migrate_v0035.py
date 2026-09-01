from __future__ import annotations
from pathlib import Path
import hashlib,json,os,sqlite3,sys

from .representation import canonicalize_operational_value, contains_forbidden_representation_bytes


def _sha(raw:bytes)->str:
    return hashlib.sha256(raw).hexdigest()


def migrate_trace(path:Path)->dict:
    if not path.exists():
        return {"exists":False,"changed":False,"pre_sha256":"","post_sha256":""}
    raw=path.read_bytes()
    rows=[]
    for line in raw.decode("utf-8",errors="replace").splitlines():
        try:
            value=json.loads(line)
        except Exception:
            value=line
        safe=canonicalize_operational_value(value)
        if isinstance(safe,(dict,list)):
            rows.append(json.dumps(safe,sort_keys=True,ensure_ascii=False))
        else:
            rows.append(str(safe))
    clean=("\n".join(rows)+("\n" if rows else "")).encode()
    tmp=path.with_suffix(path.suffix+".0035")
    tmp.write_bytes(clean); os.chmod(tmp,0o600); tmp.replace(path); os.chmod(path,0o600)
    if contains_forbidden_representation_bytes(path.read_bytes()):
        raise RuntimeError("trace_representation_remains")
    return {"exists":True,"changed":clean!=raw,"pre_sha256":_sha(raw),"post_sha256":_sha(clean)}


def migrate_db(path:Path)->dict:
    if not path.exists():
        return {"exists":False,"changed":False}
    conn=sqlite3.connect(path)
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA secure_delete=ON")
    changed=False
    for table,col,key in (
        ("requests","metadata_json","idempotency_key"),
        ("receipts","receipt_json","idempotency_key"),
    ):
        try:
            rows=conn.execute(f"SELECT {key},{col} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            try:
                data=json.loads(row[col])
            except Exception:
                continue
            safe=canonicalize_operational_value(data)
            # Normalize platform-bearing fields specifically.
            if isinstance(safe,dict):
                from .representation import canonical_platform
                if "target_platform" in safe:
                    safe["target_platform"]=canonical_platform(safe["target_platform"]) or "unknown"
                if "target" in safe:
                    safe["target"]=canonical_platform(safe["target"]) or "unknown"
            if safe!=data:
                conn.execute(
                    f"UPDATE {table} SET {col}=? WHERE {key}=?",
                    (json.dumps(safe,sort_keys=True),row[key]),
                )
                changed=True
    conn.commit()
    if changed:
        conn.execute("VACUUM")
        conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    for candidate in (path,Path(str(path)+"-wal"),Path(str(path)+"-shm")):
        if candidate.exists() and contains_forbidden_representation_bytes(candidate.read_bytes()):
            raise RuntimeError(f"db_representation_remains:{candidate.name}")
    return {"exists":True,"changed":changed}


def main():
    if len(sys.argv)!=3:
        raise SystemExit("usage: representation_migrate_v0035 TRACE CONTACT_DB")
    trace=Path(sys.argv[1]); db=Path(sys.argv[2])
    result={"trace":migrate_trace(trace),"contact_db":migrate_db(db)}
    print(json.dumps(result,sort_keys=True))


if __name__=="__main__": main()
