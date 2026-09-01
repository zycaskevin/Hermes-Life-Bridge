from __future__ import annotations
import asyncio, hashlib, json, os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from .config import BridgeConfig
from .contact_delivery import HermesSendClient
from .contact_model import ContactDecisionEnvelope, ContactIntentEnvelope, DeliveryReceipt
from .contact_store import ContactStore
from .correlation import stable_id
from .trace import BridgeTracer
from .representation import canonical_platform

def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def _parse(value):
    return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(timezone.utc)

def _target_platform(target: str) -> str:
    return canonical_platform(target) or "unknown"

def _error_fingerprint(exc: Exception) -> str:
    return hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:16]

def _request_hash(intent,decision):
    scrub={k:v for k,v in intent.to_dict().items() if k!="message_text"}
    return hashlib.sha256(json.dumps({"intent":scrub,"decision":decision.to_dict()},sort_keys=True,separators=(",",":")).encode()).hexdigest()

class ContactService:
    def __init__(self, config:BridgeConfig|None=None, sender=None):
        self.config=config or BridgeConfig.from_env()
        self.sender=sender or HermesSendClient(self.config)
        self.store=ContactStore(self.config.contact_db)
        self.trace=BridgeTracer(self.config.trace_path)

    def _validate(self,intent,decision):
        if intent.life_did != self.config.life_did: raise ValueError("life_did_mismatch")
        if decision.intent_id != intent.intent_id: raise ValueError("intent_decision_mismatch")
        if decision.outcome != "contact": raise ValueError("decision_not_contact")
        if _parse(intent.expires_at) <= datetime.now(timezone.utc): raise ValueError("intent_expired")
        if hashlib.sha256(intent.message_text.encode()).hexdigest() != intent.message_hash:
            raise ValueError("message_hash_mismatch")
        if not intent.evidence_refs: raise ValueError("missing_evidence")
        if self.config.contact_target and intent.target != self.config.contact_target:
            raise ValueError("target_not_allowlisted")

    def process(self,intent,decision):
        trace_id=stable_id("contact-trace",intent.intent_id)
        target_platform=_target_platform(intent.target)
        self.trace.emit(trace_id=trace_id,stage="CONTACT_REQUEST_RECEIVED",intent_id=intent.intent_id,target_platform=target_platform,target_redacted=True)
        self._validate(intent,decision)
        rh=_request_hash(intent,decision)
        cached=self.store.get_receipt(intent.idempotency_key)
        if cached:
            self.trace.emit(trace_id=trace_id,stage="CONTACT_DEDUPE_HIT",intent_id=intent.intent_id,receipt_id=cached.receipt_id,target_platform=target_platform,target_redacted=True)
            return replace(cached,target=intent.target,duplicate=True)
        self.store.reserve(
            idempotency_key=intent.idempotency_key,request_hash=rh,
            metadata={"intent_id":intent.intent_id,"target":intent.target,"message_hash":intent.message_hash},
            created_at=intent.created_at
        )
        if not self.config.contact_delivery_enabled:
            receipt=DeliveryReceipt(
                receipt_id=stable_id("delivery-receipt",intent.intent_id,"dry_run"),
                intent_id=intent.intent_id,idempotency_key=intent.idempotency_key,life_did=intent.life_did,
                target=intent.target,status="dry_run",message_hash=intent.message_hash,
                provider_message_id="",delivered_at=_now()
            )
            self.store.save_receipt(receipt)
            self.trace.emit(trace_id=trace_id,stage="CONTACT_DRY_RUN",intent_id=intent.intent_id,receipt_id=receipt.receipt_id)
            return receipt
        self.trace.emit(trace_id=trace_id,stage="HERMES_SEND_START",intent_id=intent.intent_id,target_platform=target_platform,target_redacted=True)
        try:
            provider_id=self.sender.send(target=intent.target,message=intent.message_text)
            receipt=DeliveryReceipt(
                receipt_id=stable_id("delivery-receipt",intent.intent_id,intent.message_hash,provider_id),
                intent_id=intent.intent_id,idempotency_key=intent.idempotency_key,life_did=intent.life_did,
                target=intent.target,status="delivered",message_hash=intent.message_hash,
                provider_message_id=provider_id,delivered_at=_now()
            )
            self.store.save_receipt(receipt)
            self.trace.emit(trace_id=trace_id,stage="HERMES_SEND_SUCCESS",intent_id=intent.intent_id,
                            receipt_id=receipt.receipt_id,provider_message_id=provider_id,
                            target_platform=target_platform,target_redacted=True)
            self.trace.emit(trace_id=trace_id,stage="DELIVERY_RECEIPT_SENT",intent_id=intent.intent_id,
                            receipt_id=receipt.receipt_id,status=receipt.status,
                            target_platform=target_platform,target_redacted=True)
            return receipt
        except Exception as exc:
            self.trace.emit(trace_id=trace_id,stage="CONTACT_FAILED",status="fail",intent_id=intent.intent_id,
                            error=type(exc).__name__,error_fingerprint=_error_fingerprint(exc),
                            target_platform=target_platform,target_redacted=True)
            raise

    async def handle_client(self,reader,writer):
        try:
            raw=await asyncio.wait_for(reader.readline(),timeout=5)
            if not raw: return
            data=json.loads(raw.decode())
            intent=ContactIntentEnvelope(**data["intent"])
            decision=ContactDecisionEnvelope(**data["decision"])
            receipt=await asyncio.to_thread(self.process,intent,decision)
            writer.write((json.dumps({"ok":True,"receipt":receipt.to_dict()},ensure_ascii=False)+"\n").encode())
            await writer.drain()
        except Exception as exc:
            try:
                writer.write((json.dumps({"ok":False,"error":str(exc)[:500]})+"\n").encode()); await writer.drain()
            except Exception: pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass

    async def serve(self):
        sock=Path(self.config.contact_socket)
        sock.parent.mkdir(parents=True,exist_ok=True)
        if sock.exists(): sock.unlink()
        server=await asyncio.start_unix_server(self.handle_client,path=str(sock))
        os.chmod(sock,0o660)
        async with server: await server.serve_forever()
