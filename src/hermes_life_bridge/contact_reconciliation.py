from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Callable

from .contact_store import ContactStore
from .operation_store import OperationStateConflict, OperationStore
from .reliability_contract import BridgeOperation, OperationState, RetryClass


class ContactEvidenceOutcome(str, Enum):
    DELIVERED = "delivered"
    NOT_DELIVERED = "not_delivered"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContactEvidence:
    outcome: ContactEvidenceOutcome
    source: str
    provider_message_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ContactEvidenceOutcome):
            raise ValueError("contact_evidence_outcome_required")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.source or ""):
            raise ValueError("contact_evidence_source_must_be_safe_token")
        if len(self.provider_message_id) > 512:
            raise ValueError("provider_message_id_too_long")
        if self.outcome is ContactEvidenceOutcome.DELIVERED and not self.provider_message_id:
            # Hermes/providers may not always expose an external id, but decisive
            # external evidence must carry a stable reference for later dedupe.
            raise ValueError("delivered_evidence_requires_provider_message_id")
        if self.outcome is not ContactEvidenceOutcome.DELIVERED and self.provider_message_id:
            raise ValueError("provider_message_id_only_valid_for_delivered_evidence")


@dataclass(frozen=True)
class ContactReconciliationResult:
    operation: BridgeOperation
    evidence: ContactEvidence
    changed: bool


class ContactReconciler:
    """Resolve Contact DELIVERY_UNKNOWN only from authoritative evidence."""

    def __init__(
        self,
        operation_store: OperationStore,
        contact_store: ContactStore,
        evidence_probe: Callable[[BridgeOperation], ContactEvidence] | None = None,
    ):
        self.operation_store = operation_store
        self.contact_store = contact_store
        self.evidence_probe = evidence_probe

    def _local_evidence(self, operation: BridgeOperation) -> ContactEvidence | None:
        receipt = self.contact_store.get_receipt(operation.idempotency_key)
        if receipt is not None and receipt.status == "delivered":
            provider_id = receipt.provider_message_id or f"hlb-receipt:{receipt.receipt_id}"
            return ContactEvidence(
                ContactEvidenceOutcome.DELIVERED,
                "contact_store_receipt",
                provider_id,
            )

        stored = self.contact_store.get_reconciliation(operation.idempotency_key)
        if stored is None:
            return None
        return ContactEvidence(
            ContactEvidenceOutcome(stored["outcome"]),
            stored["source"],
            stored.get("provider_message_id", ""),
        )

    def collect_evidence(self, operation: BridgeOperation) -> ContactEvidence:
        if operation.kind is not RetryClass.CONTACT:
            raise ValueError("contact_reconciliation_is_contact_only")
        if operation.state is not OperationState.DELIVERY_UNKNOWN:
            raise OperationStateConflict("contact_reconciliation_requires_delivery_unknown")

        local = self._local_evidence(operation)
        if local is not None:
            return local
        if self.evidence_probe is None:
            return ContactEvidence(ContactEvidenceOutcome.UNKNOWN, "no_authoritative_probe")

        evidence = self.evidence_probe(operation)
        if not isinstance(evidence, ContactEvidence):
            raise TypeError("contact_evidence_probe_must_return_contact_evidence")
        if evidence.outcome is not ContactEvidenceOutcome.UNKNOWN:
            self.contact_store.save_reconciliation(
                idempotency_key=operation.idempotency_key,
                outcome=evidence.outcome.value,
                source=evidence.source,
                provider_message_id=evidence.provider_message_id,
            )
        return evidence

    def reconcile(
        self,
        operation_id: str,
        *,
        observed_at: str,
    ) -> ContactReconciliationResult:
        operation = self.operation_store.get(operation_id)
        if operation is None:
            raise KeyError("operation_not_found")
        evidence = self.collect_evidence(operation)

        if evidence.outcome is ContactEvidenceOutcome.UNKNOWN:
            return ContactReconciliationResult(operation, evidence, False)

        if evidence.outcome is ContactEvidenceOutcome.DELIVERED:
            resolved = self.operation_store.mark_completed(
                operation.operation_id,
                updated_at=observed_at,
            )
        else:
            resolved = self.operation_store.mark_failed_safe(
                operation.operation_id,
                updated_at=observed_at,
                error_code="reconciled_authoritative_non_delivery",
            )
        return ContactReconciliationResult(resolved, evidence, True)
