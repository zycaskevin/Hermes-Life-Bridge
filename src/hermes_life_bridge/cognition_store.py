from __future__ import annotations
from pathlib import Path
import json, sqlite3, threading
from .cognition_model import CognitiveReceipt, CognitiveTaskEnvelope


class CognitionStore:
    """Small operational idempotency/receipt cache.

    The cognition service may execute work in asyncio worker threads, so this store uses a
    cross-thread SQLite connection guarded by a process-local lock. It is not canonical memory.
    """
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        try:
            Path(path).chmod(0o600)
        except Exception:
            pass
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cognition_tasks(
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    request_hash TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cognition_receipts(
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    receipt_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def get_receipt(self, idempotency_key: str) -> CognitiveReceipt | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT receipt_json FROM cognition_receipts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row["receipt_json"])
        data["duplicate"] = True
        return CognitiveReceipt(**data)

    def reserve_task(self, task: CognitiveTaskEnvelope) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO cognition_tasks(task_id,idempotency_key,request_hash,task_json,created_at) VALUES(?,?,?,?,?)",
                (task.task_id, task.idempotency_key, task.request_hash(), json.dumps({k:v for k,v in task.to_dict().items() if k != "instruction"}, sort_keys=True), task.created_at),
            )
            row = self.conn.execute(
                "SELECT request_hash FROM cognition_tasks WHERE idempotency_key=?",
                (task.idempotency_key,),
            ).fetchone()
            if row and row["request_hash"] != task.request_hash():
                self.conn.rollback()
                raise ValueError("idempotency_key_reused_with_different_request")
            self.conn.commit()

    def save_receipt(self, receipt: CognitiveReceipt) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cognition_receipts(task_id,idempotency_key,receipt_json,completed_at) VALUES(?,?,?,?)",
                (receipt.task_id, receipt.idempotency_key, json.dumps(receipt.to_dict(), sort_keys=True), receipt.completed_at),
            )
            self.conn.commit()
