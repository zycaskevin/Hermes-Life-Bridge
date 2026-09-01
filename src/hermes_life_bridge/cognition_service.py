from __future__ import annotations
import asyncio, json, os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from .cognition_model import CognitiveReceipt, CognitiveTaskEnvelope, sha256_text
from .cognition_store import CognitionStore
from .config import BridgeConfig
from .correlation import stable_id
from .hermes_api import HermesApiClient
from .trace import BridgeTracer


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class CognitionService:
    def __init__(self, config: BridgeConfig | None = None, api_client=None):
        self.config = config or BridgeConfig.from_env()
        self.api = api_client or HermesApiClient(self.config)
        self.trace = BridgeTracer(self.config.trace_path)
        self.store = CognitionStore(self.config.cognition_db)

    def _validate(self, task: CognitiveTaskEnvelope) -> None:
        if task.life_did != self.config.life_did:
            raise ValueError("life_did_mismatch")
        if task.session_policy != "task_isolated":
            raise ValueError("unsupported_session_policy")
        if _parse_time(task.expires_at) <= datetime.now(timezone.utc):
            raise ValueError("task_expired")
        if task.risk_level not in {"L0", "L1", "L2", "L3"}:
            raise ValueError("invalid_risk_level")
        if task.risk_level in {"L2", "L3"}:
            raise ValueError("risk_requires_human_or_governed_path")

    def process(self, task: CognitiveTaskEnvelope) -> CognitiveReceipt:
        trace_id = stable_id("cognition-trace", task.task_id)
        self.trace.emit(trace_id=trace_id, stage="COGNITION_TASK_RECEIVED", task_id=task.task_id,
                        basis_state_sequence=task.basis_state_sequence, projection_hash=task.projection_hash)
        self._validate(task)
        cached = self.store.get_receipt(task.idempotency_key)
        if cached:
            self.trace.emit(trace_id=trace_id, stage="COGNITION_DEDUPE_HIT", task_id=task.task_id,
                            receipt_id=cached.receipt_id)
            return cached
        self.store.reserve_task(task)
        started = _now()
        self.trace.emit(trace_id=trace_id, stage="HERMES_API_CONNECT", task_id=task.task_id,
                        endpoint=self.config.hermes_api_base_url)
        try:
            output, session_id = self.api.cognize(instruction=task.instruction, task_id=task.task_id)
            completed = _now()
            receipt = CognitiveReceipt(
                receipt_id=stable_id("cognitive-receipt", task.task_id, task.request_hash(), sha256_text(output)),
                task_id=task.task_id,
                idempotency_key=task.idempotency_key,
                life_did=task.life_did,
                status="completed",
                basis_state_sequence=task.basis_state_sequence,
                basis_state_hash=task.basis_state_hash,
                projection_hash=task.projection_hash,
                output_text=output,
                output_hash=sha256_text(output),
                hermes_session_id=session_id,
                request_hash=task.request_hash(),
                started_at=started,
                completed_at=completed,
            )
            self.store.save_receipt(receipt)
            self.trace.emit(trace_id=trace_id, stage="HERMES_API_RESPONSE", task_id=task.task_id,
                            receipt_id=receipt.receipt_id, output_hash=receipt.output_hash,
                            hermes_session_id=session_id)
            return receipt
        except Exception as exc:
            self.trace.emit(trace_id=trace_id, stage="COGNITION_FAILED", status="fail",
                            task_id=task.task_id, error=type(exc).__name__, detail=str(exc)[:240])
            raise

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw:
                return
            data = json.loads(raw.decode("utf-8"))
            task = CognitiveTaskEnvelope(**data)
            receipt = await asyncio.to_thread(self.process, task)
            trace_id = stable_id("cognition-trace", task.task_id)
            self.trace.emit(trace_id=trace_id, stage="COGNITIVE_RECEIPT_SENT", task_id=task.task_id,
                            receipt_id=receipt.receipt_id, duplicate=receipt.duplicate)
            writer.write((json.dumps({"ok": True, "receipt": receipt.to_dict()}, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except Exception as exc:
            try:
                writer.write((json.dumps({"ok": False, "error": str(exc)[:500]}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def serve(self) -> None:
        sock = Path(self.config.cognition_socket)
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self.handle_client, path=str(sock))
        os.chmod(sock, 0o660)
        async with server:
            await server.serve_forever()
