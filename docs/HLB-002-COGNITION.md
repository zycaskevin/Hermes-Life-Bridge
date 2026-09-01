# HLB-002 — Cognition Bridge

Life Runtime may submit a bounded `CognitiveTaskEnvelope` to the HLB cognition socket.
HLB invokes Hermes' OpenAI-compatible `/v1/chat/completions` API and returns a typed receipt.

Safety/architecture decisions:
- Hermes session is isolated per task by default.
- HLB does not send `X-Hermes-Session-Key`, so it does not opt into Hermes long-term-memory scoping.
- L2/L3 tasks are rejected from this autonomous bridge path.
- Receipt echoes the task's basis state hash and projection hash.
- HLB operational receipt cache is for retry/idempotency only; it is not canonical memory.
- Life Runtime stores the receipt as a candidate and decides freshness/application separately.
