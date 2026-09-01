# Roadmap

## HLB-001 — Gateway Ingress Bridge ✅ COMPLETE

- Gateway `pre_gateway_dispatch` authoritative ingress ✅
- CLI-only fallback `pre_llm_call` ✅
- normalization ✅
- dedupe / correlation ✅
- Unix socket transport ✅
- authoritative persisted ACK consistency ✅
- trace / doctor / self-test ✅
- real Gateway → Life Runtime E2E ✅

## HLB-002 — Cognition Bridge ✅ COMPLETE

- local cognition service/socket ✅
- Life Runtime `CognitiveTask` contract ✅
- task-isolated Hermes API session ✅
- no long-term-memory Session-Key by default ✅
- CognitiveReceipt + idempotency ✅
- basis-state / projection integrity ✅
- stale/fresh candidate classification in Life Runtime ✅
- real GB10 Hermes cognition E2E ✅

## HLB-003 — Contact Bridge ✅ COMPLETE

- Life Runtime governed `ContactIntent` / `ContactDecision` ingress ✅
- contact service/socket ✅
- delivery disabled by default ✅
- Hermes one-shot provider delivery ✅
- real Feishu provider delivery E2E ✅
- `DeliveryReceipt` ✅
- exactly-once/idempotent intent replay behavior ✅
- structured `SessionSource` → canonical route normalization ✅
- production RouteStore isolation, mode `0600` ✅
- ordinary trace route redaction ✅
- ContactStore/WAL route redaction ✅
- historical privacy migration ✅
- deterministic acceptance ordering ✅
- canonical representation boundary (`SessionSource`, `Platform.*`, object repr blocked) ✅
- installer/service-state recovery ✅
- HLB-003.5 independent final acceptance ✅

### HLB-003 closure evidence

- provider delivery: PASS
- DeliveryReceipt: delivered
- exactly one authorized acceptance message delivered
- duplicate external send: NO
- delivery restored to `false`
- SafeStop: NO
- Doctor: HEALTHY
- forbidden runtime representation byte counts: 0

## HLB-004 — Runtime Reliability & Compatibility ⏭️ NEXT

Goal: move from functionally proven E2E paths to a bridge that can operate continuously across process restarts, temporary dependency failures, and Hermes upgrades.

Planned scope:

- reconnect after Hermes / Life Runtime restart
- bounded retry with explicit retry classes
- cognition timeout/recovery and durable correlation
- ambiguous contact-send timeout handling without duplicate provider messages
- durable/outbox semantics where required
- Hermes plugin hook/API compatibility detection
- route invalidation and safe relearning
- service crash recovery/readiness
- compatibility matrix across supported Hermes versions
- accelerated fault-injection tests
- 24h / 72h soak tests

Hard boundary: HLB-004 must not absorb Life Runtime LiveState, Concern, Memory, Personality, Motivation, Contact Governance, or Agent orchestration responsibilities.
