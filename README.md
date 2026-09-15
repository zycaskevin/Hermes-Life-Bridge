# Hermes Life Bridge

**Hermes ↔ Life Runtime vendor-specific integration adapter.**

Hermes Life Bridge (HLB) lets Hermes act as the messaging/cognition execution surface for a vendor-neutral Life Runtime.

> Life Runtime decides **why / whether**.
> Hermes Life Bridge handles **how**.

HLB does **not** own Digital Life identity, canonical memory, personality, LiveState, Concern, Motivation, ContactDecision, or agent orchestration.

## Release status

**HLB v0.4.3 — Runtime Reliability + governed Codex owner-decision bridge + preserve-config live updater**

| Area | Status |
| --- | --- |
| Gateway ingress → Life Runtime | ✅ Complete |
| Life Runtime cognition → Hermes | ✅ Complete |
| Governed proactive Contact → Hermes provider | ✅ Complete |
| Crash/restart recovery | ✅ Complete |
| Bounded retry | ✅ Complete |
| Contact unknown-delivery protection | ✅ Complete |
| Hermes capability discovery | ✅ Complete |
| Route freshness/invalidation | ✅ Complete |
| Component Doctor | ✅ Complete |
| Fault injection | ✅ Complete |
| Accelerated soak / bounded maintenance | ✅ Complete |
| Nancy 1h/24h/72h live soak tooling | ✅ Included; run after deployment |
| Codex approval owner-decision routing | ✅ Implemented; disabled by default |

## Safety invariants

- Full installer defaults external proactive Contact **OFF**; the code-only live updater preserves the existing HLB environment byte-for-byte and never turns a disabled gate on.
- `DELIVERY_UNKNOWN` is never blindly retried.
- Contact retry is allowed only after authoritative evidence of non-delivery.
- Exact private routes remain in a mode-`0600` RouteStore and never enter normal trace/Doctor output.
- Raw user message text is not stored in the reliability outbox.
- Runtime/Python representations such as `SessionSource(...)` / `Platform.*` are blocked from operational storage.
- Retry is bounded per action class.
- Runtime ACK loss replays the same idempotency key, so canonical Runtime state advances once.
- HLB failures never become Life Runtime authority.

## Reliability architecture

```text
Hermes Gateway
    ↓
HLB normalize + durable content-free Percept outbox
    ↓
Life Runtime

Life Runtime CognitiveTask
    ↓
HLB durable bounded retry
    ↓
Hermes API
    ↓
CognitiveReceipt

Life Runtime governed CONTACT
    ↓
HLB durable send boundary
    ↓
Hermes/provider
    ↓
DeliveryReceipt / reconciliation
```

### Contact unknown outcome

```text
provider invocation may have happened
        ↓
DELIVERY_UNKNOWN
   ├─ delivery proven     → COMPLETED
   ├─ non-delivery proven → FAILED_SAFE → bounded retry
   └─ no proof            → remain locked
```

There is no automatic `DELIVERY_UNKNOWN → retry` path.

## Codex owner-decision bridge

HLB v0.4.2 can close a governed Codex approval loop without exposing Codex-internal request identity to Nancy.

Life Runtime already carries the canonical `work-event://<event_id>` evidence reference into the HLB Contact intent. When a delivered owner contact is about an actionable Codex blocker, HLB stores only that opaque Work Event id beside its redacted Contact metadata.

The Nancy tool surface is intentionally narrow:

```text
hlb_resolve_codex_approval(decision)
```

The model cannot supply an event id, request id, thread id, command, file path, or diff. HLB derives the exact current delivery route from the same Hermes session, requires that route's newest actually-delivered Contact to be a Work Event contact, and forwards only `work_event_id + decision` to an owner-only CLB Unix socket.

This feature is disabled by default with `HLB_CODEX_DECISION_ENABLED=false`. Enabling the bridge does not itself authorize Life Runtime to surface Work Events to the owner.

## Preserve-config live code upgrade

HLB v0.4.3 adds a separate code-only updater for an already healthy deployment:

```bash
scripts/update_code_preserve_config.sh
```

Unlike the full installer, this updater does **not** rewrite `~/.config/hermes-life-bridge.env` and does not reset the current Contact/Work Producer/Ambient/Codex-decision flags. It backs up the live plugin code, stops HLB workers, replaces only the plugin tree while preserving the existing `.venv`, restarts HLB user services and the Hermes gateway, requires HLB Doctor to return `HEALTHY`, and verifies the HLB env SHA-256 plus selected non-secret flags are unchanged. Any failed acceptance restores the previous plugin code automatically.

## Long-running operation

HLB v0.4.2 includes:

- automatic Percept recovery daemon;
- Cognition and Contact services with automatic systemd restart;
- daily maintenance timer;
- bounded trace rotation (10 MiB × current + 3 backups by default);
- 30-day Percept/Cognition terminal reliability retention;
- Contact dedupe records retained rather than automatically purged;
- component-level compatibility and health diagnostics.

## Install on Nancy / GB10

From a checkout of this repository:

```bash
bash scripts/install_on_hermes.sh
```

The installer:

1. verifies Life Runtime is available;
2. backs up the previous HLB plugin/config/systemd units;
3. stops HLB workers before replacing files;
4. installs v0.4.2 in an isolated venv;
5. removes the old competing `nancy-live-runtime` plugin from active discovery;
6. installs/starts Cognition, Contact, Percept recovery, and maintenance services;
7. resets external Contact delivery to **OFF**;
8. runs self-test and an accelerated soak gate;
9. attempts `hermes gateway restart` so the new plugin hooks load;
10. automatically rolls back if installation fails before completion.

After the Gateway restart, send Nancy one normal message, then run:

```bash
~/.hermes/plugins/hermes-life-bridge/scripts/accept_on_nancy.sh
```

If Hermes uses a non-default home, use the plugin path printed by the installer.

A successful acceptance prints:

```text
NANCY_ACCEPTANCE=PASS
```

## Diagnose

```bash
hermes-life compatibility
hermes-life doctor
hermes-life trace --tail 20
```

Doctor reports these separately:

```text
Ingress
Cognition
Contact
Privacy
Compatibility
```

with `healthy`, `degraded`, or `blocked`, plus an overall `HEALTHY / DEGRADED / BLOCKED` result.

## Live soak after deployment

The real wall-clock soak must run on Nancy because it depends on Nancy's actual Hermes, Life Runtime, sockets, services, routes, and operational load.

```bash
# First deployment check
scripts/soak_hlb004.sh 1

# Extended acceptance
scripts/soak_hlb004.sh 24
scripts/soak_hlb004.sh 72
```

These monitors do not enable proactive Contact; they periodically record Doctor/component state and operational file growth.

## Development acceptance

The current release line preserves the v0.4.0 reliability acceptance and adds v0.4.2 Codex decision-routing tests:

- full automated HLB regression/failure suite;
- Pyright on changed reliability modules;
- shell syntax validation for install/acceptance/soak scripts;
- 5,000-event accelerated soak with 500 duplicate submissions;
- v0.4.0 wheel + source-distribution build;
- one Runtime state advance per unique Percept;
- zero residual Percept outbox rows;
- no forbidden runtime representation;
- bounded trace growth and operation DB compaction.

See `ROADMAP.md` and `docs/HLB-004.*` for the detailed contracts and evidence.
