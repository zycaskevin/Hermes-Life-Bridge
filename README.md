# Hermes Life Bridge

**Hermes ↔ Life Runtime vendor adapter.**

Hermes Life Bridge (HLB) isolates Hermes-specific lifecycle, hook, session, route, cognition, and delivery behavior from the vendor-neutral Life Runtime.

HLB does **not** own identity, canonical memory, personality, LiveState, concerns, motivation, contact policy, or agent orchestration. Life Runtime decides **why** something matters and whether cognition/contact is permitted; HLB provides the Hermes-specific **how**.

## Milestone status

| Milestone | Capability | Status |
| --- | --- | --- |
| HLB-001 | Hermes Gateway ingress → Life Runtime | ✅ COMPLETE |
| HLB-002 | Life Runtime cognition task → Hermes → CognitiveReceipt | ✅ COMPLETE |
| HLB-003 | Governed ContactIntent → Hermes delivery → DeliveryReceipt | ✅ COMPLETE |
| HLB-004 | Runtime reliability & compatibility | ⏭️ NEXT |

HLB-003 reached final closure in **HLB-003.5 — Canonical Representation Boundary** after real Feishu delivery and independent privacy/readback acceptance.

## Architecture

```text
Hermes Gateway
      │
      │ HLB-001
      ▼
Hermes Life Bridge ───────────────► Life Runtime
      ▲                                 │
      │                                 │ governed cognition/contact
      │ HLB-002 / HLB-003               │
      └─────────────────────────────────┘
```

### HLB-001 — Gateway Ingress

```text
Hermes Gateway MessageEvent
        ↓
pre_gateway_dispatch
        ↓
normalize + correlation + dedupe
        ↓
Unix socket
        ↓
Life Runtime PerceptEvent
        ↓
authoritative receipt + trace
```

Capabilities:

- `pre_gateway_dispatch` authoritative Gateway ingress
- `pre_llm_call` CLI-only fallback
- deterministic idempotency keys
- privacy-preserving content fingerprints
- raw user text excluded from ordinary operational trace
- Unix-socket Life Runtime transport
- doctor/self-test diagnostics
- fail-open observer behavior for Hermes message handling

### HLB-002 — Cognition Bridge

```text
Life Runtime CognitiveTask
        ↓
HLB cognition service
        ↓
Hermes /v1/chat/completions
        ↓
CognitiveReceipt
        ↓
Life Runtime
```

Properties:

- local cognition Unix socket
- task-isolated `X-Hermes-Session-Id`
- no `X-Hermes-Session-Key` by default
- idempotent cognition receipt cache
- L0/L1 automatic path only; L2/L3 rejected
- receipt bound to basis state / projection hashes
- real GB10 Hermes cognition E2E verified

### HLB-003 — Contact Bridge

```text
Life Runtime ContactDecision(outcome=contact)
        ↓
HLB contact service
        ↓
Hermes one-shot delivery
        ↓
provider
        ↓
DeliveryReceipt
```

Properties:

- delivery defaults **OFF**
- real Feishu provider delivery verified
- exactly one authorized test message delivered during acceptance
- idempotent receipt handling prevents duplicate provider sends for the same intent
- SafeStop/contact governance remain Life Runtime authority
- exact route stored only in private RouteStore (`0600`)
- ordinary trace and ContactStore do not persist raw chat IDs or canonical targets
- canonical representation boundary prevents Python/runtime repr leakage such as `SessionSource(...)` or `Platform.FEISHU`
- historical privacy migration uses secure delete / VACUUM / WAL truncate
- self-test cannot overwrite the production delivery route

Final HLB-003.5 acceptance: **PASS**.

## Why this repo is separate from Life Runtime

Life Runtime must remain vendor-neutral. Hermes-specific concerns belong here, including:

- Hermes hook contracts and version compatibility
- Gateway `SessionSource` normalization
- provider/channel addressing
- Hermes session correlation
- Hermes API transport
- `hermes send` delivery behavior
- adapter-specific retry/idempotency diagnostics

A future OpenClaw or other cognition runtime should use its own bridge while the Life Runtime contracts remain unchanged.

## Install as a Hermes plugin

Install/copy this repository into Hermes' plugin directory using the plugin workflow supported by the deployed Hermes version. The repository-level `__init__.py` is the plugin entry point.

Key configuration examples:

```bash
export LIFE_RUNTIME_SOCKET="${XDG_RUNTIME_DIR}/nancy-live-runtime.sock"
export LIFE_RUNTIME_LIFE_DID="did:example:life"
export HLB_TRACE_PATH="${XDG_STATE_HOME:-$HOME/.local/state}/hermes-life-bridge/trace.jsonl"
```

Contact delivery is intentionally disabled by default and should only be enabled through the governed deployment/E2E procedure.

## Diagnose

```bash
hermes-life doctor
hermes-life trace --tail 20
```

A healthy Gateway ingress should show stages such as:

```text
HOOK_RECEIVED
NORMALIZED
DEDUPE_CHECK
SOCKET_CONNECT
EVENT_SENT
RUNTIME_ACK
STATE_ADVANCED
```

## Next milestone — HLB-004

HLB-004 will harden the bridge for long-running operation:

- restart/reconnect behavior
- bounded retry and failure classification
- cognition timeout/recovery
- ambiguous contact-send timeout without duplicate external delivery
- Hermes hook/API compatibility detection
- route invalidation/relearning
- service recovery
- 24h / 72h soak testing

See [ROADMAP.md](ROADMAP.md).
