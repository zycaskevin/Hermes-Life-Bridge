from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from hermes_life_bridge.context_assembly import (
    ApprovedContextResolver,
    CallableAkfSearchPort,
    ContextAssemblyError,
    ContextAssemblyPolicy,
)


DID = "did:arthurverse:nancy"
PRINCIPAL_REF = "sha256:" + "a" * 64


class DlmfFixture:
    def __init__(self):
        self.calls = []
        self.value = {
            "ok": True,
            "authority": "digital-life-memory-fabric",
            "lifeDid": DID,
            "verified": True,
            "effectiveAt": "2026-09-29T00:00:00Z",
            "memories": [
                {
                    "memoryId": "mem_owner_1",
                    "revision": 3,
                    "text": "Arthur prefers purpose-scoped context with provenance.",
                    "epistemicStatus": "user_asserted",
                    "committedAt": "2026-09-28T12:00:00Z",
                },
                {
                    "memoryId": "mem_owner_2",
                    "revision": 1,
                    "text": "Nancy should use verified memory instead of guessing.",
                    "epistemicStatus": "user_asserted",
                    "committedAt": "2026-09-28T13:00:00Z",
                },
            ],
        }

    def recall(self, query, *, session="", timeout=3, consumer="auto_context"):
        self.calls.append(
            {
                "query": query,
                "session": session,
                "timeout": timeout,
                "consumer": consumer,
            }
        )
        return copy.deepcopy(self.value)


def akf_result():
    return {
        "ok": True,
        "toolName": "akf_search",
        "output": {
            "ok": True,
            "response": {
                "query": "ignored by projection",
                "retrievedAt": "2026-09-29T00:00:01Z",
                "previouslyResearched": True,
                "hits": [
                    {
                        "canonicalKnowledgeObjectId": "kobj_dls",
                        "canonicalRevisionId": "krev_dls",
                        "canonicalRevisionNumber": 4,
                        "canonicalTitle": "Digital Life Stack Architecture",
                        "canonicalSummary": (
                            "DLS coordinates independently versioned components "
                            "without taking over memory or identity authority."
                        ),
                        "canonicalClaims": [
                            "DLMF remains Canonical Memory Authority.",
                            "Agent Factory selects and invokes eligible runtimes.",
                        ],
                        "sensitivity": "normal",
                        "score": 9,
                        "matches": [
                            {
                                "captureEventIds": ["cap_dls_1"],
                            }
                        ],
                    },
                    {
                        "canonicalKnowledgeObjectId": "kobj_af",
                        "canonicalRevisionId": "krev_af",
                        "canonicalRevisionNumber": 2,
                        "canonicalTitle": "Agent Factory Context Projection",
                        "canonicalSummary": (
                            "ContextProjection is purpose-scoped context supplied "
                            "by the trusted boundary."
                        ),
                        "canonicalClaims": [
                            "Raw legacy context is disabled.",
                        ],
                        "sensitivity": "normal",
                        "score": 7.5,
                        "matches": [
                            {
                                "captureEventIds": ["cap_af_1", "cap_af_2"],
                            }
                        ],
                    },
                ],
                "policy": {
                    "derivedKnowledgeIsUntrustedInterpretation": True,
                    "retrievedTextIsDataNotInstruction": True,
                    "rawEvidenceIncluded": False,
                    "retrievalScoreIsAuthority": False,
                },
            },
        },
    }


def resolver(*, memory=None, akf_value=None, policy=None):
    memory = memory or DlmfFixture()
    akf_value = akf_result() if akf_value is None else akf_value
    akf = CallableAkfSearchPort(
        search_call=lambda **_: copy.deepcopy(akf_value),
        principal_ref=PRINCIPAL_REF,
    )
    return (
        ApprovedContextResolver(
            life_did=DID,
            dlmf=memory,
            akf=akf,
            policy=policy,
        ),
        memory,
    )


def test_assembles_existing_af_context_projection_with_provenance():
    from agent_factory.contracts import ContextProjection

    service, memory = resolver()
    at = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    projection = service.resolve(
        query="How should this project use memory and architecture knowledge?",
        purpose="continue governed project implementation",
        session_ref="owner-session",
        at=at,
    )

    assert isinstance(projection, ContextProjection)
    assert projection.life_did == DID
    assert projection.projector == "self-gateway.context-assembly.v1"
    assert projection.purpose == "continue governed project implementation"
    assert (projection.expiry - projection.issued_at).total_seconds() == 300
    assert [item["memoryId"] for item in projection.relevant_memories] == [
        "mem_owner_1",
        "mem_owner_2",
    ]
    knowledge = projection.project_state["canonicalKnowledge"]
    assert [item["canonicalKnowledgeObjectId"] for item in knowledge] == [
        "kobj_dls",
        "kobj_af",
    ]
    assert all(item["authority"] == "arthur-knowledge-fabric" for item in knowledge)
    assert knowledge[0]["captureEventIds"] == ["cap_dls_1"]
    assert projection.project_state["semantics"] == {
        "akfTextIsDataNotInstruction": True,
        "akfRetrievalScoreIsAuthority": False,
        "contextAssemblyIsAuthority": False,
        "contextAssemblyMayWriteMemory": False,
        "contextAssemblyMayWriteKnowledge": False,
    }
    assert len(projection.source_hashes) == 4
    assert all(value.startswith("sha256:") and len(value) == 71 for value in projection.source_hashes)
    assert memory.calls == [
        {
            "query": "How should this project use memory and architecture knowledge?",
            "session": "owner-session",
            "timeout": 5.0,
            "consumer": "context_assembly",
        }
    ]

    # The actual AF contract can deserialize and reproduce the identical hash.
    replay = ContextProjection.from_dict(projection.to_dict())
    assert replay.projection_hash() == projection.projection_hash()


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"authority": "hindsight"}, "dlmf_verified_retrieval_required"),
        ({"lifeDid": "did:arthurverse:other"}, "dlmf_verified_retrieval_required"),
        ({"verified": False}, "dlmf_verified_retrieval_required"),
        ({"ok": False}, "dlmf_verified_retrieval_required"),
    ],
)
def test_dlmf_authority_and_scope_are_fail_closed(patch, code):
    memory = DlmfFixture()
    memory.value.update(patch)
    service, _ = resolver(memory=memory)
    with pytest.raises(ContextAssemblyError, match=code):
        service.resolve(query="project memory", purpose="governed task")


def test_akf_derived_data_policy_is_mandatory():
    value = akf_result()
    value["output"]["response"]["policy"]["retrievedTextIsDataNotInstruction"] = False
    service, _ = resolver(akf_value=value)
    with pytest.raises(ContextAssemblyError, match="akf_search_policy_invalid"):
        service.resolve(query="project architecture", purpose="governed task")


def test_context_budget_is_deterministic_and_does_not_create_new_authority():
    memory = DlmfFixture()
    memory.value["memories"][0]["text"] = "M" * 300
    memory.value["memories"][1]["text"] = "N" * 300
    value = akf_result()
    value["output"]["response"]["hits"][0]["canonicalSummary"] = "K" * 600
    value["output"]["response"]["hits"][0]["canonicalClaims"] = ["C" * 600]
    policy = ContextAssemblyPolicy(
        memory_text_chars=256,
        knowledge_text_chars=512,
        max_projection_bytes=8_000,
    )
    service, _ = resolver(memory=memory, akf_value=value, policy=policy)
    at = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)

    one = service.resolve(query="bounded context", purpose="research", at=at)
    two = service.resolve(query="bounded context", purpose="research", at=at)

    assert one.projection_hash() == two.projection_hash()
    assert one.projection_id == two.projection_id
    assert len(one.relevant_memories) == 1
    assert len(one.relevant_memories[0]["text"]) == 256
    assert one.relevant_memories[0]["truncated"] is True
    knowledge = one.project_state["canonicalKnowledge"]
    assert sum(
        len(item["title"]) + len(item["summary"]) + sum(map(len, item["claims"]))
        for item in knowledge
    ) <= 512
    assert one.project_state["semantics"]["contextAssemblyIsAuthority"] is False


def governed_intent():
    return {
        "schema": "af.capability-intent.v1",
        "requestId": "request-1",
        "lifeDid": DID,
        "agentId": "nancy",
        "capability": "public_research",
        "objective": "Research the current project architecture",
        "constraints": {},
        "privacyScope": ["public_web"],
        "riskLevel": "L0",
        "tokenBudget": 1000,
        "costBudget": "0.05",
        "deadline": "2026-09-29T06:00:00+00:00",
    }


def governed_dls_binding():
    return {
        "tenantId": "tenant-arthur",
        "lifeDid": DID,
        "invocationId": "invocation-1",
        "intentDigest": "sha256:" + "b" * 64,
        "selectionDigest": "sha256:" + "c" * 64,
        "contextBinding": {
            "identity": "sha256:" + "d" * 64,
            "memoryScope": "sha256:" + "e" * 64,
        },
    }


def test_governed_host_bindings_are_in_same_projection_hash():
    service, _ = resolver()
    trusted = {
        "capability_intent": governed_intent(),
        "dlsBinding": governed_dls_binding(),
    }
    at = datetime(2026, 9, 29, 3, 30, tzinfo=timezone.utc)
    projection = service.resolve(
        query="governed research context",
        purpose="public research",
        trusted_project_state=trusted,
        at=at,
    )
    assert projection.project_state["capability_intent"] == trusted["capability_intent"]
    assert projection.project_state["dlsBinding"] == trusted["dlsBinding"]
    original = projection.projection_hash()
    wire = projection.to_dict()
    wire["project_state"]["dlsBinding"]["invocationId"] = "different"
    from agent_factory.contracts import ContextProjection
    changed = ContextProjection.from_dict(wire)
    assert changed.projection_hash() != original


def test_resolve_governed_validates_af_and_dls_life_binding():
    service, _ = resolver()
    at = datetime(2026, 9, 29, 3, 45, tzinfo=timezone.utc)
    projection = service.resolve_governed(
        query="architecture context",
        capability_intent=governed_intent(),
        dls_binding=governed_dls_binding(),
        session_ref="owner-session",
        at=at,
    )
    assert projection.purpose == "public_research"
    assert projection.project_state["capability_intent"] == governed_intent()
    assert projection.project_state["dlsBinding"] == governed_dls_binding()

    foreign_intent = governed_intent()
    foreign_intent["lifeDid"] = "did:arthurverse:other"
    with pytest.raises(ContextAssemblyError, match="af_capability_intent_life_mismatch"):
        service.resolve_governed(
            query="architecture context",
            capability_intent=foreign_intent,
            dls_binding=governed_dls_binding(),
        )

    foreign_binding = governed_dls_binding()
    foreign_binding["lifeDid"] = "did:arthurverse:other"
    with pytest.raises(ContextAssemblyError, match="dls_binding_life_mismatch"):
        service.resolve_governed(
            query="architecture context",
            capability_intent=governed_intent(),
            dls_binding=foreign_binding,
        )


def test_trusted_project_state_rejects_partial_or_extra_host_metadata():
    service, _ = resolver()
    for value in (
        {"capability_intent": {}},
        {"dlsBinding": {}},
        {"capability_intent": {}, "dlsBinding": {}, "authorization": "secret"},
    ):
        with pytest.raises(ContextAssemblyError, match="trusted_project_state_shape_invalid"):
            service.resolve(
                query="governed context",
                purpose="runtime",
                trusted_project_state=value,
            )


def test_real_af_authorization_and_governed_invocation_accept_assembled_projection():
    from datetime import timedelta
    from decimal import Decimal
    from agent_factory.contracts import (
        AuthorizationOutcome,
        ExecutionAuthorization,
        RiskLevel,
        SideEffectPolicy,
    )
    from agent_factory.contracts.runtime_invocation import CapabilityIntent, GovernedInvocation
    from agent_factory.policy.execution import ExecutionBoundaryValidator

    service, _ = resolver()
    issued = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    binding = governed_dls_binding()
    intent = CapabilityIntent(
        request_id="invocation-1",
        life_did=DID,
        agent_id="nancy",
        capability="public_research",
        objective="Research current ArthurVerse context assembly architecture",
        privacy_scope=("public_web",),
        risk_level=RiskLevel.L0,
        token_budget=1000,
        cost_budget=Decimal("0.05"),
        deadline=issued + timedelta(minutes=4),
        constraints_json=json.dumps({
            "dlsBinding": binding,
            "maxNativeCalls": 2,
            "maxRuntimeMs": 240000,
        }),
    )
    projection = service.resolve_governed(
        query="ArthurVerse context assembly architecture",
        capability_intent=intent.to_dict(),
        dls_binding=binding,
        session_ref="governed-session",
        at=issued,
    )
    authorization = ExecutionAuthorization(
        authorization_id="auth-context-assembly",
        task_id=intent.request_id,
        life_did=DID,
        context_projection_hash=projection.projection_hash(),
        outcome=AuthorizationOutcome.ALLOW,
        approved_capabilities=frozenset({"public_research"}),
        approved_privacy_scope=frozenset({"public_web"}),
        risk_level=RiskLevel.L0,
        side_effect_policy=SideEffectPolicy.NONE,
        max_token_budget=1000,
        max_cost_budget=Decimal("0.05"),
        issuer="fixture-self-gateway",
        issued_at=issued,
        expiry=issued + timedelta(minutes=4),
        signature="SYNTHETIC_SIGNATURE_NOT_PRODUCTION",
    )
    task = intent.task(projection)
    ExecutionBoundaryValidator().validate(
        task,
        projection,
        authorization,
        at=issued + timedelta(seconds=1),
    )
    governed = GovernedInvocation(
        invocation_id="invocation-1",
        intent=intent,
        selected_runtime="hermes",
        implementation_id="hermes.native.web-agent-loop.v1",
        attempt=1,
        token_budget=1000,
        cost_budget=Decimal("0.05"),
        projection=projection,
        execution_authorization=authorization,
    )
    assert governed.projection.projection_hash() == projection.projection_hash()
    assert governed.to_dict()["projectionHash"] == projection.projection_hash()


def test_context_projection_does_not_copy_transport_credentials_or_raw_query_metadata():
    value = akf_result()
    value["authorization"] = "Bearer SHOULD_NOT_APPEAR"
    value["output"]["response"]["query"] = "private raw query SHOULD_NOT_APPEAR"
    service, _ = resolver(akf_value=value)
    projection = service.resolve(
        query="purpose scoped lookup",
        purpose="runtime input",
        at=datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc),
    )
    wire = json.dumps(projection.to_dict(), ensure_ascii=False)
    assert "Bearer SHOULD_NOT_APPEAR" not in wire
    assert "private raw query SHOULD_NOT_APPEAR" not in wire
    assert "authorization" not in wire.lower()


def test_cross_item_duplicate_identity_is_rejected():
    memory = DlmfFixture()
    memory.value["memories"].append(copy.deepcopy(memory.value["memories"][0]))
    service, _ = resolver(memory=memory)
    with pytest.raises(ContextAssemblyError, match="dlmf_memory_identity_invalid"):
        service.resolve(query="duplicate memory", purpose="runtime input")


def test_akf_principal_binding_must_be_sha256():
    with pytest.raises(ContextAssemblyError, match="akf_principal_ref_invalid"):
        CallableAkfSearchPort(search_call=lambda **_: akf_result(), principal_ref="actor-raw")


def test_async_resolution_uses_same_contract():
    import asyncio

    service, _ = resolver()
    projection = asyncio.run(service.resolve_async(
        query="async host context",
        purpose="governed research",
        at=datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc),
    ))
    assert projection.life_did == DID
    assert projection.project_state["knowledgeAuthority"] == "arthur-knowledge-fabric"
