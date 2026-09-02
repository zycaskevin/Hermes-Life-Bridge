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

### HLB-004.1 — Reliability Contract ✅ CONTRACT DEFINED

- `BridgeOperation` privacy-minimized durable operation contract
- `OperationState`: prepared / in-flight / retry-wait / completed / failed-safe / delivery-unknown / exhausted
- `RetryClass`: Percept / Cognition / Contact
- bounded `RetryPolicy` contract with Contact unknown-outcome retry prohibition
- `DeliveryOutcome` with normative `FAILED_SAFE` vs `DELIVERY_UNKNOWN` semantics
- `RouteStatus`: unknown / fresh / stale / invalid
- `HermesCompatibilityReport` contract
- HLB-003.5 canonical representation boundary reused for reliability serialization
- LR-M4 / Concern authority explicitly excluded

Next: **HLB-004.2 — Durable Operation Store**.

### HLB-004.2 — Durable Operation Store ✅ COMPLETE

- SQLite `OperationStore` with WAL + `synchronous=FULL`
- DB/WAL/SHM mode `0600`
- privacy-minimized operation-only schema; no message/prompt/route payload columns
- idempotent reservation + request-hash conflict detection
- atomic state transitions with `BEGIN IMMEDIATE`
- attempt increment only at durable `prepared → in_flight` boundary
- durable `FAILED_SAFE → RETRY_WAIT` persistence
- `DELIVERY_UNKNOWN` durable across restart and forbidden from retry scheduling
- Contact-owner interrupted `in_flight → delivery_unknown` recovery primitive
- Percept/Cognition interrupted operations intentionally untouched
- concurrent double-start prevention
- HLB-003.5 representation boundary enforced for durable identity
- exact-route-shaped operation identity rejection
- future SQLite schema version fails closed
- `HLB_OPERATION_DB` configuration/default installer path

Next: **HLB-004.3 — Retry Engine**.

### HLB-004.3 — Retry Engine ✅ COMPLETE

- separate Percept / Cognition / Contact retry policies
- total attempts bounded at 5 / 3 / 2 respectively
- deterministic bounded exponential backoff + jitter
- durable `FAILED_SAFE → RETRY_WAIT` scheduling
- stranded `FAILED_SAFE` restart sweep
- bounded due-operation selection
- due release returns to `PREPARED` without executing
- concurrent scheduler double-release protection
- durable `begin_attempt()` boundary increments attempt only when starting
- Percept interrupted `in_flight` replay-safe recovery
- Cognition interrupted recovery requires accepted-receipt reconciliation
- Cognition reconciliation before schedule, due release, and retry start
- late CognitiveReceipt cancels pending retry and completes operation
- Cognition-only compatibility amendment for late completion from retry states
- Contact `DELIVERY_UNKNOWN` remains outside retry path
- Retry Engine performs no Hermes / Life Runtime / provider execution

Next: **HLB-004.4 — Contact Delivery Reconciliation**.

### HLB-004.4 — Contact Delivery Reconciliation ✅ COMPLETE

- explicit FAILED_SAFE vs DELIVERY_UNKNOWN Hermes send errors
- timeout/nonzero-after-invocation classified conservatively as unknown
- authoritative Contact reconciliation evidence model
- durable local DeliveryReceipt reconciliation
- optional provider evidence probe contract
- conflict-safe reconciliation persistence without raw payload/route
- Contact operation store integrated into enabled delivery path
- durable send ordering: IN_FLIGHT → provider → receipt → COMPLETED
- crash-after-receipt recovery without duplicate send
- unresolved unknown outcome permanently blocks blind resend
- proven non-delivery is the only path back into bounded retry
- startup Contact recovery and reconciliation before socket serve
- backward-compatible redacted Contact failure trace

Next: **HLB-004.5 — Hermes Compatibility Discovery**.

### HLB-004.5 — Hermes Compatibility Discovery ✅ COMPLETE

- observed plugin registration capability evidence
- observed gateway hook / SessionSource evidence
- Hermes CLI version and send capability probes
- Hermes API health capability probe
- privacy-minimized compatibility evidence/report files mode 0600
- capability-based compatibility instead of hard version allowlist
- explicit blocking issues vs warnings
- route/platform discovery without copying exact private route
- hermes-life compatibility CLI command

Next: **HLB-004.6 — Doctor vNext**.

### HLB-004.6 — Doctor vNext & Route Lifecycle ✅ COMPLETE

- component health: Ingress / Cognition / Contact / Privacy / Compatibility
- human-readable healthy / degraded / blocked messages
- overall HEALTHY / DEGRADED / BLOCKED
- unresolved delivery_unknown surfaced as Contact blocker
- route lifecycle: unknown / fresh / stale / invalid
- 7-day default learned-route freshness policy
- stale/invalid learned route blocks Contact instead of silent fallback
- explicit route invalidation and relearning
- resolved route hash bound into Contact idempotency
- operational representation/privacy mode checks
- Doctor never echoes exact private route

Next: **HLB-004.7 — Failure Injection Suite**.
