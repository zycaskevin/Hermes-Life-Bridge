# HLB-006 — Preserve-Config Live Code Upgrade

Date: 2026-09-15
Status: COMPLETE / LIVE UPGRADE PASS / CONFIG INVARIANT PASS
Release: Hermes Life Bridge v0.4.3

## Goal

Upgrade an already healthy live HLB deployment without re-running the full installer's safety reset behavior.

The existing full installer intentionally rewrites the HLB environment and defaults external Contact delivery to OFF. That is correct for a fresh install, but it is not appropriate for a live code-only upgrade where owner-approved settings such as Contact, Work Producer, and Ambient are already active.

HLB-006 therefore adds a separate bounded updater:

```text
scripts/update_code_preserve_config.sh
```

## Preserved production state

Before the live upgrade the owner config had:

```text
HLB_CONTACT_DELIVERY_ENABLED=true
HLB_WORK_PRODUCER_ENABLED=true
HLB_AMBIENT_INTEREST_ENABLED=true
HLB_CODEX_DECISION_ENABLED=unset
```

The updater records the HLB env SHA-256 and selected non-secret settings before mutation. It refuses acceptance if either changes.

It does not modify:

- `~/.config/hermes-life-bridge.env`;
- Contact enablement;
- Work Producer enablement;
- Ambient Interest enablement;
- Codex owner-decision enablement;
- Life Runtime system service/configuration.

## Upgrade transaction

The code-only updater:

1. verifies the live plugin/venv/Runtime socket;
2. records environment/config invariants;
3. creates a private timestamped backup of the existing plugin code;
4. stops the HLB worker services;
5. replaces only the plugin tree with rsync while preserving `.venv`;
6. refreshes editable package metadata with `uv pip install --offline --no-deps` when `uv` is available;
7. restarts HLB workers, maintenance timer, and Hermes gateway;
8. waits for sockets/services;
9. verifies source import version and package metadata version;
10. retries HLB Doctor for a bounded readiness window and requires final `HEALTHY`;
11. verifies environment hash and selected feature flags are unchanged.

Any failed gate restores the backed-up code and restarts the prior live deployment.

## First live attempt — readiness race and rollback

The first live 0.4.3 attempt replaced the code successfully, but Doctor was checked immediately after Hermes gateway restart and had not yet reached its stable healthy state.

Result:

```text
doctor_not_healthy
HLB_CODE_ROLLBACK=PASS
```

Post-rollback checks confirmed:

- live version restored to the previous deployment;
- gateway and HLB services active;
- HLB Doctor `HEALTHY`;
- Contact/Work Producer/Ambient settings unchanged.

Running the 0.4.3 repo code directly against the same live config while the previous deployment was active returned Doctor `HEALTHY`, proving this was a readiness timing race rather than a compatibility defect.

The updater was corrected to retry Doctor for up to 60 × 0.5 seconds while still requiring a final `HEALTHY` result.

## Second live attempt — upgrade pass

The bounded retry version was executed live and passed:

```text
HLB_CODE_UPDATE=PASS
version=0.4.3
env_hash_unchanged=true
HLB_AMBIENT_INTEREST_ENABLED=true
HLB_CODEX_DECISION_ENABLED=unset
HLB_CONTACT_DELIVERY_ENABLED=true
HLB_WORK_PRODUCER_ENABLED=true
```

Post-upgrade:

```text
import version = 0.4.3
plugin manifest = 0.4.3
Doctor = HEALTHY
ingress = healthy
cognition = healthy
contact = healthy
privacy = healthy
compatibility = healthy
```

The live tool manifest contains:

```text
hlb_report_work_event
hlb_resolve_codex_approval
```

`HLB_CODEX_DECISION_ENABLED` remained unset, so deploying the tool did not authorize owner decision routing.

## Package metadata alignment

After the source upgrade, the old preserved venv still reported stale package metadata even though HLB loaded the new source through `PYTHONPATH`.

The host already had `uv`, so metadata was refreshed offline:

```text
uv pip install --offline --python <live-venv-python> --no-deps -e <live-plugin>
```

This aligned:

```text
source/import version = 0.4.3
package metadata version = 0.4.3
plugin manifest version = 0.4.3
```

HLB-006 now includes this offline metadata refresh in both the forward transaction and best-effort rollback path.

## Third live attempt — idempotent upgrade pass

The final updater was run again from live 0.4.3 to live 0.4.3 to prove idempotence.

Observed:

```text
HLB_CODE_UPDATE=PASS
version=0.4.3
metadata_version=0.4.3
env_hash_unchanged=true
HLB_AMBIENT_INTEREST_ENABLED=true
HLB_CODEX_DECISION_ENABLED=unset
HLB_CONTACT_DELIVERY_ENABLED=true
HLB_WORK_PRODUCER_ENABLED=true
```

This proves repeated code-only upgrades do not rotate or reset owner-approved configuration.

## Test evidence

The complete HLB automated suite passed under ForgeRelay after HLB-006 changes. Additional updater tests cover:

- shell syntax;
- no sudo usage;
- no environment rewrite / Contact reset;
- backup and rollback contract;
- live venv preservation;
- bounded Doctor readiness retry;
- offline `uv` metadata refresh;
- source/metadata version invariant.

## Remaining production boundary

HLB is now live at 0.4.3 and healthy. CLB Work Control and Owner Decision services are separately live as owner-only user services.

The remaining Codex Production Shadow gate is not an HLB upgrade issue: the system `life-runtime.service` still needs one host/operator restart to load the already-staged `codex:dlmf:work` verifier. HLB Codex decision routing remains disabled until the later Work Contact governance gate.
