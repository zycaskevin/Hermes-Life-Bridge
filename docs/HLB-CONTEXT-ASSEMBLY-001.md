# HLB-CONTEXT-ASSEMBLY-001 — DLMF + AKF → AF ContextProjection

**Date:** 2026-09-29
**Status:** Implemented; live read path verified; production Runtime effect intentionally not activated here.

## Goal

Fill the missing ArthurVerse context path without creating a new authority, database,
service, memory system, or runtime capability:

```text
DLMF verified retrieval ─┐
                         ├──> trusted host / Self Gateway context resolver
AKF canonical search ────┘                 │
                                           ▼
                                 AF ContextProjection
                                           │
                                           ▼
                                      Agent Factory
                                           │
                                           ▼
                                    Selected Runtime
```

The implementation reuses Agent Factory's existing `ContextProjection` contract.
There is no second "Context Pack" contract.

## Why this shape

The reviewed open-source patterns converge on the same boundary:

- **Lethe** reconstructs reviewed/versioned memory into an explicit context projection
  and can pin a manifest for reproducibility.
- **Hindsight** uses a lifecycle context provider: recall before an agent run, inject
  bounded memory context, retain after the run.
- **LangGraph** passes long-term stores and static dependencies through Runtime instead
  of letting graph nodes secretly own those authorities.
- **Graphiti** returns retrieval results with episode/provenance and temporal identity.
- **Cognee** combines retrieval surfaces into agent context while keeping the knowledge
  engine separate from the consuming agent.

ArthurVerse already had the destination contract: AF
`ContextProjection` is explicitly "a Self Gateway output containing only
purpose-scoped context." Therefore the smallest correct implementation is an approved
resolver, not another context service.

No external project is introduced as a dependency by this packet.

## Authority boundary

### DLMF

DLMF remains Canonical Memory Authority.

The resolver accepts a DLMF source only when the existing recall port returns:

- `ok=true`
- `authority=digital-life-memory-fabric`
- the exact bound `lifeDid`
- `verified=true`

Memory IDs, revisions, text, epistemic status and commit time may enter the runtime
projection. Operational receipts contain only IDs/hashes; the live verification proof
does not copy memory text.

A retrieval outage fails closed for governed assembly. It is not interpreted as an
empty memory store.

### AKF

AKF remains Knowledge / Provenance Authority for this boundary.

The resolver accepts AKF search only when its existing response policy says:

- `derivedKnowledgeIsUntrustedInterpretation=true`
- `retrievedTextIsDataNotInstruction=true`
- `rawEvidenceIncluded=false`
- `retrievalScoreIsAuthority=false`

Only active canonical search output supplied by AKF is projected. The resolver does
not access AKF PostgreSQL or raw evidence.

The AKF authenticated principal is represented only by a SHA-256 reference supplied
by the already-authenticated host adapter. No Bearer credential enters the projection.

### Context assembly

Context assembly is **not an authority**.

It may:

- read admitted DLMF and AKF ports;
- normalize their results;
- enforce deterministic limits;
- preserve source provenance;
- create AF's existing immutable `ContextProjection`.

It may not:

- write DLMF;
- write AKF;
- select a Runtime;
- issue an `ExecutionAuthorization`;
- expand token/cost/tool/network permission;
- infer a new canonical fact from source material;
- treat retrieval score as truth;
- turn retrieved text into instructions.

### Agent Factory / Self Gateway

AF remains the governed invocation / runtime-selection layer.

For governed native execution the host supplies the existing:

- AF `CapabilityIntent`;
- DLS exact `dlsBinding`.

`ApprovedContextResolver.resolve_governed()` validates their life binding and puts
both into the same `project_state` as AKF/DLMF context. AF's
`projection_hash()` therefore binds:

- DLMF memory context;
- AKF canonical knowledge context;
- AF capability intent;
- DLS request/selection/context binding.

Changing any of those changes the projection hash.

Execution authority is still issued and verified outside this resolver.

## Context mapping

### DLMF → `relevant_memories`

Each projected memory contains:

- authority;
- `memoryId`;
- revision;
- bounded text;
- truncation marker;
- epistemic status;
- commit time;
- SHA-256 source hash.

DLMF retrieval order is preserved.

### AKF → `project_state.canonicalKnowledge`

Each projected knowledge item contains:

- authority;
- canonical KnowledgeObject ID;
- canonical revision ID / revision number;
- bounded title / summary / claims;
- capture-event provenance refs;
- sensitivity;
- retrieval score with `retrievalScoreIsAuthority=false`;
- truncation marker;
- SHA-256 source hash.

### Governance metadata → `project_state`

For governed runtime invocation:

- `capability_intent`
- `dlsBinding`

are supplied by the trusted host and become part of the same projection hash.

The v1 resolver accepts no other trusted-project-state keys.

## Deterministic limits

Default bounds:

- projection TTL: 300 seconds;
- DLMF results: 3;
- AKF results: 4;
- DLMF text budget: 6,000 characters;
- AKF text budget: 9,000 characters;
- maximum encoded projection: 32,000 bytes;
- DLMF retrieval timeout: 5 seconds by default, configurable from 1–15 seconds.

The production Nancy HLB configuration currently supplies its already-existing
`memory_recall_timeout_seconds=15`; the resolver does not create a separate network
timeout authority.

Text is clipped deterministically in source ranking order. No model is called to
summarize context during assembly.

## Live read verification

On 2026-09-29 the same owner/project query was sent to both existing read surfaces.

AKF:

- production MCP endpoint remained on loopback;
- 4 canonical KnowledgeObject hits returned;
- canonical revisions and capture-event provenance were preserved;
- AKF policy marked text as data, not instructions.

DLMF:

- production verified retrieval returned 3 allowed memories;
- scope remained `tenant-arthur / did:arthurverse:nancy / life`;
- DLMF remained canonical authority.

The resolver produced one real AF `ContextProjection`:

- DLMF memories: 3;
- AKF knowledge objects: 4;
- source hashes: 7;
- encoded size: about 11.8 KiB;
- TTL: 5 minutes;
- writes: 0;
- Runtime invocation: 0 for this read proof.

Owner-local content-minimized evidence:

`~/.local/share/digital-life/nancy-resident/memory-runtime/activation-evidence/context-assembly-20260929/live-read-context-projection.json`

Raw DLMF memory text and AKF raw evidence are not copied into that proof.

A second content-minimized proof used those same live sources, added an AF
`CapabilityIntent` plus DLS exact-binding metadata, and validated the resulting
projection with AF's real `ExecutionBoundaryValidator`. An AF `GovernedInvocation`
was then constructed with the identical projection hash.

This second proof deliberately used a synthetic contract-only
`ExecutionAuthorization`; it does **not** claim a production Self Gateway signature
or Runtime effect. Results:

- DLMF memories: 3;
- AKF canonical knowledge objects: 4;
- source hashes: 7;
- AF execution-boundary validation: PASS;
- AF governed-invocation construction: PASS;
- Runtime invocation: 0;
- memory/knowledge writes: 0.

Evidence:

`~/.local/share/digital-life/nancy-resident/memory-runtime/activation-evidence/context-assembly-20260929/af-contract-proof.json`

## Executable acceptance

`tests/test_context_assembly.py` proves:

1. the resolver returns AF's real `ContextProjection`, not a parallel schema;
2. AF `ContextProjection.from_dict()` round-trips to the same hash;
3. wrong DLMF authority, life or verification state fails closed;
4. invalid AKF data/instruction policy fails closed;
5. source ordering and bounded truncation are deterministic;
6. transport credentials and AKF response query metadata do not enter the projection;
7. duplicate DLMF identity is rejected;
8. the AKF principal binding must be a SHA-256 ref;
9. AF intent and DLS binding belong to the same life;
10. governance metadata changes the same projection hash;
11. partial or extra trusted host metadata is rejected;
12. AF `ExecutionBoundaryValidator` accepts an authorization bound to the assembled projection;
13. AF `GovernedInvocation` carries the exact assembled projection hash.

## Production runtime stop line

This packet deliberately does **not** claim a production public-research execution.

The existing HLB native-runtime contract still reports:

- `liveBudgetEnforcementVerified=false`
- `liveExecutionVerified=false`

and the current production composition has no admitted native
`public_research` bridge service. Existing Self Gateway policy also keeps that
capability fail-closed until its separate authorization/budget activation work is
completed.

That pre-existing effect/authorization gate is independent of Context Assembly.
This packet must not bypass it merely to produce an end-to-end demo.

When that gate is activated, the trusted host wiring is:

1. DLS prepares the exact intent/selection.
2. HLB obtains `dlsBinding` from the existing native request.
3. Existing host constructs AF `CapabilityIntent`.
4. This resolver retrieves DLMF + AKF and returns
   `resolve_governed(...)` projection.
5. Existing Self Gateway issuer binds `ExecutionAuthorization` to that
   `projection_hash`.
6. AF creates `GovernedInvocation` and selects Runtime.
7. HLB submits only the already-authorized projection to the selected Runtime.

No authority boundary changes at activation time.
