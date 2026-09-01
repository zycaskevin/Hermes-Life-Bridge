import json, sqlite3
from enum import Enum
from hermes_life_bridge.representation import (
    canonical_platform, canonicalize_operational_value, contains_forbidden_representation_bytes
)
from hermes_life_bridge.trace import BridgeTracer
from hermes_life_bridge.representation_migrate_v0035 import migrate_db,migrate_trace

class Platform(Enum):
    FEISHU="feishu"

class Source:
    platform=Platform.FEISHU
    chat_id="oc_SECRET"
    def to_dict(self):
        return {"platform":self.platform,"chat_id":self.chat_id}

def test_canonical_platform_handles_all_repr_forms():
    assert canonical_platform(Platform.FEISHU)=="feishu"
    assert canonical_platform("Platform.FEISHU")=="feishu"
    assert canonical_platform("<Platform.FEISHU: 'feishu'>")=="feishu"
    assert canonical_platform("SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='oc_X')")=="feishu"
    assert canonical_platform("feishu:oc_X")=="feishu"

def test_arbitrary_runtime_object_never_becomes_repr():
    value=canonicalize_operational_value(Source())
    text=json.dumps(value)
    assert "SessionSource(" not in text
    assert "Platform.FEISHU" not in text
    assert "object at 0x" not in text

def test_trace_boundary_sanitizes_nested_repr(tmp_path):
    p=tmp_path/"trace.jsonl"
    t=BridgeTracer(str(p))
    t.emit(
        trace_id="x",stage="HOOK_RECEIVED",
        platform="SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='oc_SECRET')",
        nested={"enum":"Platform.FEISHU","obj":"<Thing object at 0x1234>"},
    )
    raw=p.read_bytes()
    assert not contains_forbidden_representation_bytes(raw)
    text=raw.decode()
    assert "oc_SECRET" not in text
    assert '"platform": "feishu"' in text

def test_historical_trace_migration(tmp_path):
    p=tmp_path/"trace.jsonl"
    p.write_text(
        json.dumps({"platform":"SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='oc_OLD')"})+"\n"
    )
    result=migrate_trace(p)
    assert result["exists"] is True
    raw=p.read_bytes()
    assert not contains_forbidden_representation_bytes(raw)
    assert b"oc_OLD" not in raw

def test_contact_db_representation_migration(tmp_path):
    p=tmp_path/"contact.sqlite3"
    conn=sqlite3.connect(p)
    conn.executescript("""
      CREATE TABLE requests(idempotency_key TEXT PRIMARY KEY,request_hash TEXT,metadata_json TEXT,created_at TEXT);
      CREATE TABLE receipts(idempotency_key TEXT PRIMARY KEY,receipt_json TEXT,delivered_at TEXT);
    """)
    bad="SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='oc_OLD')"
    conn.execute("INSERT INTO requests VALUES(?,?,?,?)",("i","h",json.dumps({"target_platform":bad}),"t"))
    conn.execute("INSERT INTO receipts VALUES(?,?,?)",("i",json.dumps({"target":bad}),"t"))
    conn.commit(); conn.close()
    migrate_db(p)
    for candidate in (p,):
        assert not contains_forbidden_representation_bytes(candidate.read_bytes())
        assert b"oc_OLD" not in candidate.read_bytes()
    c=sqlite3.connect(p)
    req=json.loads(c.execute("SELECT metadata_json FROM requests").fetchone()[0])
    rec=json.loads(c.execute("SELECT receipt_json FROM receipts").fetchone()[0])
    assert req["target_platform"]=="feishu"
    assert rec["target"]=="feishu"
    c.close()
