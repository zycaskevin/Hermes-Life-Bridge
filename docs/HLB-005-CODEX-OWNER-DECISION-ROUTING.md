# HLB-005 — Codex Owner Decision Routing

Date: 2026-09-15
Status: DEVELOPMENT COMPLETE / PRODUCTION FEATURE DISABLED BY DEFAULT
HLB version: 0.4.2
Companion: Codex Life Bridge 0.3.0 / CLB-003

## 1. Ownership

The integration preserves the existing boundaries:

```text
Codex App Server
    -> Codex Life Bridge
    -> Canonical Work Event
    -> Life Runtime Work Contact governance
    -> HLB Contact delivery
    -> owner

owner reply
    -> Hermes/Nancy tool
    -> HLB route + Work Event correlation
    -> CLB owner-decision Unix socket
    -> original live Codex request
```

Life Runtime continues to decide why / whether / when a Work Event is surfaced.
HLB only correlates the already-governed delivered Contact with the exact local CLB approval request.

## 2. Evidence correlation

Life Runtime's existing Project Work observer emits:

```text
work-event://<canonical event_id>
```

in observation evidence refs. That value is already preserved through the Ambient surface path into the HLB Contact intent.

HLB extracts a Work Event id only when exactly one bounded `work-event://...` ref is present. It stores the opaque id beside its normal redacted Contact metadata. It does not persist the evidence URL, Codex command, diff, thread id, request id, or raw owner message.

## 3. Exact-route fail-closed rules

The Nancy decision tool can act only when:

1. the current Hermes session has a fresh in-memory delivery route observed within 5 minutes;
2. the newest actually-delivered Contact for that exact route is itself associated with a Work Event id;
3. CLB still recognizes that Work Event as an actionable owner request;
4. the decision is one of the exact CLB command/file approval decisions.

If a newer unrelated delivered Contact exists on that route, HLB returns no actionable Work Event rather than reaching backward to an older approval.

Dry-run Contacts are not actionable.

## 4. Nancy tool contract

Tool:

```text
hlb_resolve_codex_approval
```

Arguments:

```json
{
  "decision": "accept | acceptForSession | decline | cancel"
}
```

No correlation identifiers are accepted from the model.

The tool description explicitly requires a clear owner answer to the Codex approval request in the same current session. `acceptForSession` is reserved for explicit session-wide permission.

## 5. CLB decision transport

HLB sends an exact local request to the owner-only CLB Unix socket:

```json
{
  "schema_version": "clb-owner-decision.v0.1",
  "work_event_id": "codexevt:...",
  "decision": "accept"
}
```

Before connecting, HLB requires the socket to:

- exist as a Unix socket;
- be owned by the HLB process user;
- grant no group/other permissions.

The socket client uses a bounded timeout and bounded response size.

## 6. Cross-repository UAT

A CLB fake App Server was held on a live file-change approval. The CLB owner request was bound to its blocker Work Event id.

HLB then:

1. delivered an isolated Contact whose evidence contained that Work Event id;
2. observed a Telegram gateway turn on the same session/route;
3. invoked the Nancy decision tool with only `decision=accept`;
4. resolved the exact latest delivered Work Event for that route;
5. sent the event-id decision to CLB.

Observed:

```text
HLB_CONTACT_DELIVERED=PASS
HLB_TARGET_MATCH=PASS
HLB_GATEWAY_ALLOW=PASS
HLB_DECISION_TOOL=PASS
```

The waiting CLB worker immediately continued and completed:

```text
agent_event=agent.completed
work_event_type=milestone_reached
```

CLB replay tests additionally verified same-decision idempotency and conflicting stale-decision rejection.

## 7. Automated acceptance

Focused HLB tests cover:

- Contact Work Event extraction and exact-route lookup;
- newer unrelated Contact hiding an older approval;
- dry-run non-actionability;
- router success and stale-event failure;
- same-session route correlation;
- rejection of model-supplied event ids / extra tool fields;
- tool disabled-by-default registration;
- release metadata consistency.

The full AEB test run has five pre-existing environmental failures where pytest's AEB sandbox temp pathname exceeds the Linux AF_UNIX pathname limit. All other tests, including the release gate, pass when those five socket-path-only cases are excluded.

## 8. Production gate

`HLB_CODEX_DECISION_ENABLED` defaults to false.

Deploying HLB 0.4.2 code does not authorize Work Contact. The feature must not be enabled for real owner decisions until:

- a production CLB decision socket/service is installed;
- a production Codex Work Event principal is onboarded;
- existing Life Runtime Work Contact governance has reached its owner-approved rollout gate;
- an explicit bounded canary is approved.
