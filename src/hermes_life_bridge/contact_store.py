from __future__ import annotations

from pathlib import Path
import hashlib
import json
import sqlite3
import threading

from .contact_model import DeliveryReceipt
from .representation import canonical_platform, canonicalize_operational_value


def _target_platform(target: str) -> str:
    return canonical_platform(target) or "unknown"


def _target_hash(target: str) -> str:
    return hashlib.sha256((target or "").encode("utf-8")).hexdigest()


def _redact_request_metadata(metadata: dict) -> dict:
    data = canonicalize_operational_value(dict(metadata))
    target = str(data.pop("target", "") or "")
    if target:
        data["target_platform"] = _target_platform(target)
        data["target_hash"] = _target_hash(target)
        data["target_redacted"] = True
    data.pop("chat_id", None)
    data.pop("thread_id", None)
    return data


def _redact_receipt_dict(data: dict) -> dict:
    out = canonicalize_operational_value(dict(data))
    target = str(out.get("target") or "")
    if target:
        out["target"] = _target_platform(target)
        out["target_hash"] = _target_hash(target)
    out.pop("chat_id", None)
    out.pop("thread_id", None)
    return out


class ContactStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.path.chmod(0o600)
        except Exception:
            pass

        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA secure_delete=ON")
            self.conn.executescript("""
              CREATE TABLE IF NOT EXISTS requests(
                idempotency_key TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS receipts(
                idempotency_key TEXT PRIMARY KEY,
                receipt_json TEXT NOT NULL,
                delivered_at TEXT NOT NULL
              );
            """)
            changed = self._migrate_redact_private_targets_locked()
            self.conn.commit()

            if changed:
                # UPDATE removes logical references, but old SQLite pages/WAL frames can
                # still contain the previous target bytes. Rewrite + truncate them.
                self.conn.execute("VACUUM")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.commit()

    def _migrate_redact_private_targets_locked(self) -> bool:
        changed = False

        for row in self.conn.execute(
            "SELECT idempotency_key, metadata_json FROM requests"
        ).fetchall():
            try:
                data = json.loads(row["metadata_json"])
            except Exception:
                continue
            redacted = canonicalize_operational_value(_redact_request_metadata(data))
            if redacted != data:
                self.conn.execute(
                    "UPDATE requests SET metadata_json=? WHERE idempotency_key=?",
                    (json.dumps(redacted, sort_keys=True), row["idempotency_key"]),
                )
                changed = True

        for row in self.conn.execute(
            "SELECT idempotency_key, receipt_json FROM receipts"
        ).fetchall():
            try:
                data = json.loads(row["receipt_json"])
            except Exception:
                continue
            redacted = canonicalize_operational_value(_redact_receipt_dict(data))
            if redacted != data:
                self.conn.execute(
                    "UPDATE receipts SET receipt_json=? WHERE idempotency_key=?",
                    (json.dumps(redacted, sort_keys=True), row["idempotency_key"]),
                )
                changed = True

        return changed

    def reserve(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        metadata: dict,
        created_at: str,
    ):
        safe_metadata = _redact_request_metadata(metadata)
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO requests(idempotency_key,request_hash,metadata_json,created_at) VALUES(?,?,?,?)",
                (
                    idempotency_key,
                    request_hash,
                    json.dumps(safe_metadata, sort_keys=True),
                    created_at,
                ),
            )
            row = self.conn.execute(
                "SELECT request_hash FROM requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row and row["request_hash"] != request_hash:
                self.conn.rollback()
                raise ValueError("contact_idempotency_conflict")
            self.conn.commit()

    def get_receipt(self, idempotency_key: str):
        with self._lock:
            row = self.conn.execute(
                "SELECT receipt_json FROM receipts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row["receipt_json"])
        data.pop("target_hash", None)
        data["duplicate"] = True
        return DeliveryReceipt(**data)

    def save_receipt(self, receipt: DeliveryReceipt):
        safe = _redact_receipt_dict(receipt.to_dict())
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO receipts(idempotency_key,receipt_json,delivered_at) VALUES(?,?,?)",
                (
                    receipt.idempotency_key,
                    json.dumps(safe, sort_keys=True),
                    receipt.delivered_at,
                ),
            )
            self.conn.commit()
