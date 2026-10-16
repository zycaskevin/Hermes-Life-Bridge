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

## Nancy existing-Feishu verified recall (2026-10-16)

The original Nancy Hermes Gateway remains the resident conversation host. Its
identity is **did:arthurverse:nancy**; Lily/Luna's independent native-tool
bundle is **not** granted to Nancy. An explicitly configured
`HLB_RESIDENT_RECALL_TOOL_ENABLED=true` exposes **only** `digital_life_recall`
through the existing DLMF memory-view binding, even with
`HLB_AGENT_TOOLS_ENABLED=false`. Automatic memory context injection remains
independent. `HLB_RESIDENT_RECALL_TIMEOUT_SECONDS` defaults to 30 seconds and
may be set between 2 and 40 seconds for the manual query path. The DLMF live
verified retrieval view permits up to 20 seconds for the primary and historical
providers; a shorter HLB deadline previously caused intermittent failed recalls.
The automatically injected memory context has its **separate** timeout and
is not raised by this change.

The handler validates Nancy's immutable existing-resident skill/memory
manifests, the owner-bound original Feishu direct-message session from the
resident Hermes `state.db`, the hash-bound source allowlist and a real user
message **before** any private DLMF retrieval. Missing, other-life, other-user,
non-Feishu or synthetic sessions fail closed without returning memory. Successful
retrieval returns verified, same-life DLMF records; a temporary retrieval
failure returns an explicit error plus a bounded, non-sensitive `failureReason`
(and possibly a safe HTTP status). No exception text, URL, query or memory
content is placed in failure diagnostics. An unavailable retrieval must never
be interpreted as zero matching memories or fabricated success. For the
owner-initiated manual tool only, one extra **read-only** attempt is allowed
within the same 30-second deadline if DLMF returns HTTP 400 with the exact
public provider-failure code `digital_life_stack_request_failed`. The retry
keeps the same authenticated life scope, never retriggers canonical writes, and
returns `attempts=1` or `attempts=2` for traceability. Validation failures,
wrong/unauthorized sessions, authorization errors, automatic context lookups
and exhausted deadlines are never retried.

For an already upgraded resident, `python3
scripts/upgrade_nancy_resident_recall_retry.py --check` / `--apply`
installs only the bounded retry implementation after matching the exact
previous source and preserving the configured 30-second budget. It backs up
source/config, restarts only the original Hermes Gateway and requires both
plugin admission and full HLB Doctor readiness; any failure restores source.

**Live Nancy GB10 evidence, 2026-10-16 (Asia/Taipei):** The original
owner-approved Feishu DM session was verified against Hermes `state.db`.
Before bounded retry, the same topic query once failed at approximately 10s
with DLMF's generic provider error while the next call returned three verified
DLMF memories. After the reviewed bounded retry code was installed,
`NANCY_BOUNDED_READ_RETRY_UPGRADE_PASS` and HLB Doctor `HEALTHY` were
observed. Three consecutive real owner-session → HLB → DLMF calls for the
same exact query succeeded on their first attempts, each returning three
verified memories (4.77s, 4.68s, 4.59s). The matching keyword metadata
included Digital Life Stack, Agent Factory, OpenClaw, Hermes, and Nancy;
a forged session was rejected before retrieval. These results do not prove
all future network calls will succeed: the live successes did not naturally
exercise attempt two, which is covered by controlled positive and negative
regression tests. The model voluntarily calling the tool during a *new*
Feishu user turn is also a separate end-to-end acceptance gate. AF native
research still requires its own approved owner context/model-auth/budget gates.

A scoped activation entry is `python3 scripts/activate_nancy_resident_recall.py
--check` followed by `--apply` only after review. For existing live Nancy
installations previously activated with the 15-second manual timeout, use
`python3 scripts/upgrade_nancy_resident_recall_timeout.py --check` and then
`--apply`; that upgrade checks the exact accepted prior HLB modules, replaces
only the two changed modules and manual-timeout setting, and retains a private
rollback snapshot with the original Gateway configuration. It verifies an exact installed
baseline, backs up the three source modules and HLB env privately, changes only
those modules plus one opt-in setting, checks the registered tool and the real
HLB Doctor, waits for transient Gateway API startup, and restores previous
source/config on a failed acceptance. It does not change native research,
Self Gateway, model providers, memory authority, canonical state or another
Digital Life. Use its timestamped backup directory for rollback/reconciliation;
never blindly rerun an interrupted update.

**Evidence scope:** HLB regression, independent static review, installed
plugin registration, real allowed Feishu session + DLMF memory retrieval, and
forged session rejection. A new Feishu chat turn with model-selected tool
invocation and response is still a separate human-visible E2E gate.
Hermes/OpenClaw/Prime native research remains governed separately by the
Owner-context signature, runtime hard budget and model-auth readiness.

## Nancy original-Feishu read-only Runtime status (2026-10-17)

The original Nancy Feishu owner direct-message session can optionally expose
`digital_life_runtime_status` through **the existing Hermes Life Bridge**. This
is a small **observability tool**, not a second Router/Runtime or a research
capability. It remains hidden unless
`HLB_RESIDENT_RUNTIME_STATUS_ENABLED=true` is set explicitly; it also requires
an exact Nancy DID, the existing owner-private resident binding and a real
owner-allowlisted Feishu DM with a user message.

The handler reads *only* the existing loopback Life Runtime health (port 8791)
and governed research-preview health (port 8896), without model-supplied URLs,
queries or credentials. Results are reduced to a safe schema containing Nancy's
DID, continuity flag, the preview connectivity booleans for Hermes/OpenClaw/
Prime, and **explicitly not verified** production authorization. In particular,
`directRuntimeConnectivityObserved=true`, `previewLiveExecution=true`, or
`previewResearchAuthorized=true` **never** implies AF admission or runtime
switching. `productionRuntimeSwitchReady` and
`productionExecutionPermittedByThisTool` stay false, and no task, canonical
memory write, model invocation, grant, or Runtime selection is attempted.

The one-time, exact-baseline, rollback-capable deployment is:

```bash
python3 scripts/activate_nancy_resident_runtime_status.py --check
python3 scripts/activate_nancy_resident_runtime_status.py --apply
```

The rollout updates only the reviewed plugin registration/config and one new
read-only module, appends the single opt-in setting, restarts only the existing
Hermes Gateway, and requires Doctor `HEALTHY`, an isolated owner-scoped
status-handler probe and forged-session rejection. The deployment explicitly
reports `feishuProviderTurnVerified=false`: model-selected tool execution in a
**new** Feishu provider turn remains a separate E2E acceptance, not inferred
from plugin registration. The loopback health reader explicitly disables
HTTP environment proxies and follows no redirects. It preserves existing
DLMF/HLB, Feishu, Contact,
Lifetime Hub, Identity and self-gateway authority. Lack of signed Owner
reconciliation, verified model auth and hard budget remains a separate blocker
for actual Agent Factory multi-Runtime execution.

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
