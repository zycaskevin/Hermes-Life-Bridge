"""Governed proactive Contact is independent of native research availability."""
from __future__ import annotations

from .config import BridgeConfig
from .contact_model import ContactDecisionEnvelope, ContactIntentEnvelope, DeliveryReceipt
from .contact_service import ContactService


class HermesContactBridge:
    """Thin gate around the existing durable sender; no new contact policy."""
    def __init__(self, config: BridgeConfig):
        self.config = config

    def deliver(self, *, selected_runtime: str, intent: ContactIntentEnvelope,
                decision: ContactDecisionEnvelope, service: ContactService) -> DeliveryReceipt:
        # Import only the error type; this module does not load DLS contracts,
        # instantiate an invocation host, or turn a research outage into silence.
        from .runtime_adapter import RuntimeBridgeError
        if selected_runtime != "hermes":
            raise RuntimeBridgeError("hermes_runtime_mismatch")
        if (not isinstance(service, ContactService)
                or not isinstance(intent, ContactIntentEnvelope)
                or not isinstance(decision, ContactDecisionEnvelope)
                or service.config.life_did != self.config.life_did
                or intent.life_did != self.config.life_did):
            raise RuntimeBridgeError("contact_life_scope_mismatch")
        return service.process(intent, decision)
