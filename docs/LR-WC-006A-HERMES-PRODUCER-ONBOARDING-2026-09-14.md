# LR-WC-006A — Hermes / Nancy Real Producer Onboarding

Date: 2026-09-14
Status: COMPLETE
HLB branch: `feat/lr-wc006a-hermes-producer`
Implementation revision: `8c935ab`
HLB version: `0.4.1`
Life Runtime mode: Production Shadow / no-send

## Result

Hermes/Nancy is now the first real Canonical Work Event producer for Life Runtime.
HLB only produces and transports sanitized work facts; Life Runtime retains M3 ownership of whether and when work may later become a contact candidate.

The plugin registers three hooks plus one explicit report tool. The report tool supports only verified completion, owner blocker, and material failure. It requires matching terminal tool evidence from the same Hermes session and turn. Session end, message completion, raw tool output, prompts, or arbitrary activity are not treated as work completion.

Ordinary `post_tool_call` data is reduced at the plugin boundary to bounded metadata needed for opaque evidence identity. Raw args/results are not persisted as Work Event evidence.

## Context activity

Gateway input reports content-free context activity through `pre_gateway_dispatch`; CLI input uses `pre_llm_call`. The Runtime receives a hashed opaque work-context identity rather than a provider chat identity or delivery route.

## Production identity

Production principal:

`hermes:nancy:work`

Granted capabilities:

- `work_event`
- `context_activity`

Not granted:

- `owner_seen`
- `shadow_review`

Runtime stores a verifier only; HLB keeps the producer credential in owner-private local state. No producer credential is stored in Git or the HLB env file.

## Live deployment

Hermes Plugin Doctor passed for HLB v0.4.1 with one tool and three hooks. Hermes gateway was gracefully restarted. Life Runtime was restarted separately and remained active on its existing local endpoint with `workShadow=no-send`.

## Real Hermes E2E

A real Hermes one-shot session was used:

`20260914_075135_0ba105`

Sequence:

1. Hermes executed a read-only tool.
2. HLB observed terminal success metadata only.
3. Hermes explicitly invoked `hlb_report_work_event`.
4. HLB bound the declaration to same-turn evidence.
5. Life Runtime admitted the event into Production Shadow.

Observed durable Work Event:

- producer: `hermes:nancy:work`
- work: `lrwc006a-hermes-producer-e2e`
- type: `verified_completion`
- evidence verified: true
- evidence count: 1
- state: `pending_idle`
- reason: `awaiting_idle_deadline`

The Work Event did not include the source tool path or raw tool output.

## Zero-send evidence

After E2E, Life Runtime ordinary action/reflection counters did not advance because of the Work Event. Recent ordinary Runtime events remained system pulses. No Contact Intent, outward Self Gateway authorization, provider delivery, delivery retry, or canary authorization occurred.

## Synthetic cleanup

The controlled auth probe and Hermes E2E event were archived as UAT evidence and removed from the active Production Shadow ledger. The active ledger was verified at:

- work events: 0
- context epochs: 0
- shadow observations: 0
- shadow decisions: 0

Therefore the next eligible real Hermes/Nancy event is the true start of the Production Shadow observation window.

## Next gate

Production Shadow now collects real Hermes/Nancy work events. The existing gate remains:

- at least 7 days;
- at least 30 unique eligible real events;
- required lifecycle/scenario coverage;
- explicit owner review.

`canary_authorized` remains false. LR-WC-007 Governed Contact Intent Integration remains blocked until that gate and explicit owner approval are satisfied.
