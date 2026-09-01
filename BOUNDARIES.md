# Boundaries

1. Raw user content MUST NOT be written to bridge trace.
2. The bridge MUST NOT alter Hermes messages in HLB-001.
3. `pre_gateway_dispatch` MUST return `{"action": "allow"}`.
4. Gateway ingress uses message identity when available.
5. CLI ingress uses session/turn identity and is disabled for non-CLI platforms.
6. Duplicate bridge events must reuse the same idempotency key.
7. Life Runtime receipt failure is observable but MUST NOT break the Hermes turn.
8. No direct Vault, memory engine, personality, or identity store access.
