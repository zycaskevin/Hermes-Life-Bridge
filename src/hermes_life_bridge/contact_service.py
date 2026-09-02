from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import NoReturn

from .config import BridgeConfig
from .contact_delivery import (
    HermesSendClient,
    HermesSendFailedSafe,
    HermesSendOutcomeUnknown,
)
from .contact_model import ContactDecisionEnvelope, ContactIntentEnvelope, DeliveryReceipt
from .contact_reconciliation import (
    ContactEvidence,
    ContactEvidenceOutcome,
    ContactReconciler,
)
from .contact_store import ContactStore
from .correlation import stable_id
from .operation_store import OperationStateConflict, OperationStore
from .routing import RouteStore, route_status
from .reliability_contract import BridgeOperation, DeliveryOutcome, OperationState, RetryClass, RouteStatus
from .retry_engine import RetryDisposition, RetryEngine
from .trace import BridgeTracer
from .representation import canonical_platform


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _target_platform(target: str) -> str:
    return canonical_platform(target) or "unknown"


def _error_fingerprint(exc: Exception) -> str:
    return hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:16]


def _request_hash(intent: ContactIntentEnvelope, decision: ContactDecisionEnvelope, execution_target: str | None = None) -> str:
    scrub = {k: v for k, v in intent.to_dict().items() if k != "message_text"}
    return hashlib.sha256(
        json.dumps(
            {
                "intent": scrub,
                "decision": decision.to_dict(),
                "execution_target_hash": hashlib.sha256((execution_target or intent.target).encode()).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class ContactReliabilityError(RuntimeError):
    code = "contact_reliability_error"


class ContactDeliveryUnknown(ContactReliabilityError):
    code = "contact_delivery_unknown"


class ContactRetryDeferred(ContactReliabilityError):
    code = "contact_retry_deferred"

    def __init__(self, next_attempt_at: str):
        self.next_attempt_at = next_attempt_at
        super().__init__(self.code)


class ContactDeliveryExhausted(ContactReliabilityError):
    code = "contact_delivery_exhausted"


class ContactRouteUnavailable(ContactReliabilityError):
    code = "contact_route_unavailable"


class ContactOperationInFlight(ContactReliabilityError):
    code = "contact_operation_in_flight"


class ContactCompletedReceiptUnavailable(ContactReliabilityError):
    code = "contact_completed_receipt_unavailable"


class ContactService:
    def __init__(
        self,
        config: BridgeConfig | None = None,
        sender=None,
        *,
        operation_store: OperationStore | None = None,
        evidence_probe=None,
    ):
        self.config = config or BridgeConfig.from_env()
        self.sender = sender or HermesSendClient(self.config)
        self.store = ContactStore(self.config.contact_db)
        self.trace = BridgeTracer(self.config.trace_path)
        self.routes = RouteStore(self.config.route_path)

        operation_path = self.config.operation_db
        if not operation_path:
            if self.config.contact_db:
                operation_path = str(
                    Path(self.config.contact_db).with_name("operations.sqlite3")
                )
            else:
                operation_path = str(
                    Path(self.config.trace_path).with_name("operations.sqlite3")
                )
        self.operations = operation_store or OperationStore(operation_path)
        self.retry = RetryEngine(self.operations)
        self.reconciler = ContactReconciler(
            self.operations,
            self.store,
            evidence_probe=evidence_probe,
        )
        self._startup_recovered = False

    def _validate(
        self,
        intent: ContactIntentEnvelope,
        decision: ContactDecisionEnvelope,
    ) -> None:
        if intent.life_did != self.config.life_did:
            raise ValueError("life_did_mismatch")
        if decision.intent_id != intent.intent_id:
            raise ValueError("intent_decision_mismatch")
        if decision.outcome != "contact":
            raise ValueError("decision_not_contact")
        if _parse(intent.expires_at) <= datetime.now(timezone.utc):
            raise ValueError("intent_expired")
        if hashlib.sha256(intent.message_text.encode()).hexdigest() != intent.message_hash:
            raise ValueError("message_hash_mismatch")
        if not intent.evidence_refs:
            raise ValueError("missing_evidence")

    def _execution_target(self, intent: ContactIntentEnvelope) -> tuple[str, RouteStatus, str]:
        learned = self.routes.load()
        status = route_status(
            learned,
            max_age_seconds=self.config.route_max_age_seconds,
        )
        if status is RouteStatus.FRESH:
            target = str((learned or {}).get("target") or "")
            source = "learned"
        elif status in (RouteStatus.STALE, RouteStatus.INVALID):
            raise ContactRouteUnavailable(f"contact_route_{status.value}")
        else:
            target = self.config.contact_target or intent.target
            source = "configured" if self.config.contact_target else "intent"

        if not target or target == "auto":
            raise ContactRouteUnavailable("contact_route_unknown")
        requested = str(intent.target or "").strip()
        target_platform = _target_platform(target)
        if requested not in {"", "auto", target, target_platform}:
            raise ValueError("target_not_allowlisted")
        if self.config.contact_target:
            configured = self.config.contact_target
            configured_platform = _target_platform(configured)
            # A learned exact route may refine a configured platform-only allowlist.
            if target not in {configured} and _target_platform(target) != configured_platform:
                raise ValueError("target_not_allowlisted")
        return target, status, source

    def _initial_operation(
        self,
        intent: ContactIntentEnvelope,
        request_hash: str,
        now: str,
    ) -> BridgeOperation:
        return BridgeOperation(
            operation_id=stable_id(
                "contact-operation",
                intent.idempotency_key,
                request_hash,
            ),
            kind=RetryClass.CONTACT,
            idempotency_key=intent.idempotency_key,
            request_hash=request_hash,
            state=OperationState.PREPARED,
            attempt=0,
            created_at=now,
            updated_at=now,
            delivery_outcome=DeliveryOutcome.NOT_ATTEMPTED,
        )

    def _reconciled_receipt(
        self,
        intent: ContactIntentEnvelope,
        evidence: ContactEvidence,
        *,
        target: str,
        duplicate: bool = False,
    ) -> DeliveryReceipt:
        if evidence.outcome is not ContactEvidenceOutcome.DELIVERED:
            raise ValueError("reconciled_receipt_requires_delivered_evidence")
        receipt = DeliveryReceipt(
            receipt_id=stable_id(
                "delivery-receipt-reconciled",
                intent.intent_id,
                intent.message_hash,
                evidence.provider_message_id,
            ),
            intent_id=intent.intent_id,
            idempotency_key=intent.idempotency_key,
            life_did=intent.life_did,
            target=target,
            status="delivered",
            message_hash=intent.message_hash,
            provider_message_id=evidence.provider_message_id,
            delivered_at=_now(),
            duplicate=duplicate,
        )
        self.store.save_receipt(receipt)
        return receipt

    def _defer_or_exhaust(self, operation: BridgeOperation, *, now: str) -> NoReturn:
        if operation.state is not OperationState.FAILED_SAFE:
            raise OperationStateConflict("defer_requires_failed_safe")
        result = self.retry.schedule_failed_safe(operation.operation_id, now=now)
        if result.disposition is RetryDisposition.EXHAUSTED:
            raise ContactDeliveryExhausted(ContactDeliveryExhausted.code)
        retry_at = result.operation.next_attempt_at or ""
        raise ContactRetryDeferred(retry_at)

    def _handle_unknown(
        self,
        operation: BridgeOperation,
        intent: ContactIntentEnvelope,
        *,
        now: str,
        target: str,
    ) -> DeliveryReceipt:
        result = self.reconciler.reconcile(operation.operation_id, observed_at=now)
        if result.evidence.outcome is ContactEvidenceOutcome.DELIVERED:
            return self._reconciled_receipt(intent, result.evidence, target=target, duplicate=True)
        if result.evidence.outcome is ContactEvidenceOutcome.NOT_DELIVERED:
            self._defer_or_exhaust(result.operation, now=now)
        raise ContactDeliveryUnknown(ContactDeliveryUnknown.code)

    def _prepare_existing(
        self,
        operation: BridgeOperation,
        intent: ContactIntentEnvelope,
        *,
        now: str,
        target: str,
    ) -> DeliveryReceipt | BridgeOperation:
        if operation.state is OperationState.COMPLETED:
            stored = self.store.get_reconciliation(operation.idempotency_key)
            if stored and stored.get("outcome") == "delivered":
                evidence = ContactEvidence(
                    ContactEvidenceOutcome.DELIVERED,
                    stored["source"],
                    stored["provider_message_id"],
                )
                return self._reconciled_receipt(intent, evidence, target=target, duplicate=True)
            raise ContactCompletedReceiptUnavailable(
                ContactCompletedReceiptUnavailable.code
            )

        if operation.state is OperationState.DELIVERY_UNKNOWN:
            return self._handle_unknown(operation, intent, now=now, target=target)

        if operation.state is OperationState.IN_FLIGHT:
            raise ContactOperationInFlight(ContactOperationInFlight.code)

        if operation.state is OperationState.EXHAUSTED:
            raise ContactDeliveryExhausted(ContactDeliveryExhausted.code)

        if operation.state is OperationState.FAILED_SAFE:
            self._defer_or_exhaust(operation, now=now)

        if operation.state is OperationState.RETRY_WAIT:
            if not operation.next_attempt_at or _parse(operation.next_attempt_at) > _parse(now):
                raise ContactRetryDeferred(operation.next_attempt_at or "")
            operation = self.operations.make_retry_ready(
                operation.operation_id,
                updated_at=now,
            )

        if operation.state is not OperationState.PREPARED:
            raise OperationStateConflict("contact_operation_not_prepared")
        return operation

    def process(
        self,
        intent: ContactIntentEnvelope,
        decision: ContactDecisionEnvelope,
    ) -> DeliveryReceipt:
        trace_id = stable_id("contact-trace", intent.intent_id)
        execution_target, learned_route_status, route_source = self._execution_target(intent)
        target_platform = _target_platform(execution_target)
        self.trace.emit(
            trace_id=trace_id,
            stage="CONTACT_REQUEST_RECEIVED",
            intent_id=intent.intent_id,
            target_platform=target_platform,
            target_redacted=True,
            route_status=learned_route_status.value,
            route_source=route_source,
        )
        self._validate(intent, decision)
        request_hash = _request_hash(intent, decision, execution_target)

        cached = self.store.get_receipt(intent.idempotency_key)
        if cached:
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_DEDUPE_HIT",
                intent_id=intent.intent_id,
                receipt_id=cached.receipt_id,
                target_platform=target_platform,
                target_redacted=True,
            )
            return replace(cached, target=execution_target, duplicate=True)

        self.store.reserve(
            idempotency_key=intent.idempotency_key,
            request_hash=request_hash,
            metadata={
                "intent_id": intent.intent_id,
                "target": execution_target,
                "message_hash": intent.message_hash,
            },
            created_at=intent.created_at,
        )

        if not self.config.contact_delivery_enabled:
            receipt = DeliveryReceipt(
                receipt_id=stable_id("delivery-receipt", intent.intent_id, "dry_run"),
                intent_id=intent.intent_id,
                idempotency_key=intent.idempotency_key,
                life_did=intent.life_did,
                target=execution_target,
                status="dry_run",
                message_hash=intent.message_hash,
                provider_message_id="",
                delivered_at=_now(),
            )
            self.store.save_receipt(receipt)
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_DRY_RUN",
                intent_id=intent.intent_id,
                receipt_id=receipt.receipt_id,
            )
            return receipt

        now = _now()
        operation, _created = self.operations.reserve(
            self._initial_operation(intent, request_hash, now)
        )
        prepared = self._prepare_existing(operation, intent, now=now, target=execution_target)
        if isinstance(prepared, DeliveryReceipt):
            return prepared

        started = self.retry.begin_attempt(prepared.operation_id, now=now).operation
        self.trace.emit(
            trace_id=trace_id,
            stage="HERMES_SEND_START",
            intent_id=intent.intent_id,
            operation_id=started.operation_id,
            attempt=started.attempt,
            target_platform=target_platform,
            target_redacted=True,
        )

        try:
            provider_id = self.sender.send(
                target=execution_target,
                message=intent.message_text,
            )
        except HermesSendFailedSafe as exc:
            failed = self.operations.mark_failed_safe(
                started.operation_id,
                updated_at=_now(),
                error_code="hermes_send_failed_safe",
            )
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_FAILED_SAFE",
                status="fail",
                intent_id=intent.intent_id,
                operation_id=failed.operation_id,
                attempt=failed.attempt,
                error_fingerprint=_error_fingerprint(exc),
                target_platform=target_platform,
                target_redacted=True,
            )
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_FAILED",
                status="fail",
                intent_id=intent.intent_id,
                error="HermesSendFailedSafe",
                error_fingerprint=_error_fingerprint(exc),
                target_platform=target_platform,
                target_redacted=True,
            )
            self._defer_or_exhaust(failed, now=_now())
        except HermesSendOutcomeUnknown as exc:
            unknown = self.operations.mark_delivery_unknown(
                started.operation_id,
                updated_at=_now(),
                error_code="hermes_send_outcome_unknown",
            )
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_DELIVERY_UNKNOWN",
                status="fail",
                intent_id=intent.intent_id,
                operation_id=unknown.operation_id,
                attempt=unknown.attempt,
                error_fingerprint=_error_fingerprint(exc),
                target_platform=target_platform,
                target_redacted=True,
            )
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_FAILED",
                status="fail",
                intent_id=intent.intent_id,
                error="HermesSendOutcomeUnknown",
                error_fingerprint=_error_fingerprint(exc),
                target_platform=target_platform,
                target_redacted=True,
            )
            return self._handle_unknown(unknown, intent, now=_now(), target=execution_target)
        except Exception as exc:
            # Custom/future sender exceptions are conservative: after begin_attempt,
            # HLB cannot prove that an external side effect did not occur.
            unknown = self.operations.mark_delivery_unknown(
                started.operation_id,
                updated_at=_now(),
                error_code="sender_exception_outcome_unknown",
            )
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_DELIVERY_UNKNOWN",
                status="fail",
                intent_id=intent.intent_id,
                operation_id=unknown.operation_id,
                attempt=unknown.attempt,
                error=type(exc).__name__,
                error_fingerprint=_error_fingerprint(exc),
                target_platform=target_platform,
                target_redacted=True,
            )
            self.trace.emit(
                trace_id=trace_id,
                stage="CONTACT_FAILED",
                status="fail",
                intent_id=intent.intent_id,
                error=type(exc).__name__,
                error_fingerprint=_error_fingerprint(exc),
                target_platform=target_platform,
                target_redacted=True,
            )
            return self._handle_unknown(unknown, intent, now=_now(), target=execution_target)

        receipt = DeliveryReceipt(
            receipt_id=stable_id(
                "delivery-receipt",
                intent.intent_id,
                intent.message_hash,
                provider_id,
            ),
            intent_id=intent.intent_id,
            idempotency_key=intent.idempotency_key,
            life_did=intent.life_did,
            target=execution_target,
            status="delivered",
            message_hash=intent.message_hash,
            provider_message_id=provider_id,
            delivered_at=_now(),
        )
        # Persist provider evidence before marking the operation complete. If the
        # process crashes between these two commits, startup reconciliation can prove
        # delivery from ContactStore and still prevent a duplicate send.
        self.store.save_receipt(receipt)
        try:
            self.operations.mark_completed(
                started.operation_id,
                updated_at=_now(),
            )
        except OperationStateConflict:
            # The durable receipt is authoritative evidence and will reconcile the
            # operation on owner restart. Never resend just because this bookkeeping
            # transition raced or failed.
            pass

        self.trace.emit(
            trace_id=trace_id,
            stage="HERMES_SEND_SUCCESS",
            intent_id=intent.intent_id,
            receipt_id=receipt.receipt_id,
            provider_message_id=provider_id,
            operation_id=started.operation_id,
            attempt=started.attempt,
            target_platform=target_platform,
            target_redacted=True,
        )
        self.trace.emit(
            trace_id=trace_id,
            stage="DELIVERY_RECEIPT_SENT",
            intent_id=intent.intent_id,
            receipt_id=receipt.receipt_id,
            status=receipt.status,
            target_platform=target_platform,
            target_redacted=True,
        )
        return receipt

    def recover_startup(self) -> dict[str, int]:
        if self._startup_recovered:
            return {"recovered_in_flight": 0, "resolved": 0, "unknown": 0, "retry": 0}
        recovered_at = _now()
        interrupted = self.operations.recover_interrupted_contact_operations(
            recovered_at=recovered_at
        )
        resolved = 0
        unknown = 0
        retry = 0
        for operation in interrupted:
            try:
                result = self.reconciler.reconcile(
                    operation.operation_id,
                    observed_at=_now(),
                )
                if result.evidence.outcome is ContactEvidenceOutcome.UNKNOWN:
                    unknown += 1
                elif result.evidence.outcome is ContactEvidenceOutcome.DELIVERED:
                    resolved += 1
                else:
                    resolved += 1
                    outcome = self.retry.schedule_failed_safe(
                        result.operation.operation_id,
                        now=_now(),
                    )
                    if outcome.disposition is RetryDisposition.SCHEDULED:
                        retry += 1
            except Exception as exc:
                # Fail closed. Operation remains durable and will be visible in Doctor.
                unknown += 1
                self.trace.emit(
                    trace_id=stable_id("contact-recovery", operation.operation_id),
                    stage="CONTACT_RECONCILIATION_FAILED",
                    status="fail",
                    operation_id=operation.operation_id,
                    error=type(exc).__name__,
                    error_fingerprint=_error_fingerprint(exc),
                )
        self._startup_recovered = True
        return {
            "recovered_in_flight": len(interrupted),
            "resolved": resolved,
            "unknown": unknown,
            "retry": retry,
        }

    async def handle_client(self, reader, writer) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw:
                return
            data = json.loads(raw.decode())
            intent = ContactIntentEnvelope(**data["intent"])
            decision = ContactDecisionEnvelope(**data["decision"])
            receipt = await asyncio.to_thread(self.process, intent, decision)
            writer.write(
                (
                    json.dumps(
                        {"ok": True, "receipt": receipt.to_dict()},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
        except ContactRetryDeferred as exc:
            try:
                writer.write(
                    (
                        json.dumps(
                            {
                                "ok": False,
                                "error": exc.code,
                                "retry_at": exc.next_attempt_at,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
            except Exception:
                pass
        except ContactReliabilityError as exc:
            try:
                writer.write(
                    (json.dumps({"ok": False, "error": exc.code}) + "\n").encode()
                )
                await writer.drain()
            except Exception:
                pass
        except Exception as exc:
            try:
                writer.write(
                    (json.dumps({"ok": False, "error": str(exc)[:500]}) + "\n").encode()
                )
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
        self.recover_startup()
        sock = Path(self.config.contact_socket)
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self.handle_client, path=str(sock))
        os.chmod(sock, 0o660)
        async with server:
            await server.serve_forever()
