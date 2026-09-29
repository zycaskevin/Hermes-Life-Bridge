"""Purpose-scoped ContextProjection assembly from existing authorities.

This module is deliberately a composition adapter, not a new authority or store.

- DLMF remains Canonical Memory Authority and must supply verified retrieval.
- AKF remains the provenance-aware knowledge/search authority for this boundary.
- Self Gateway / trusted host decides whether this resolver may be invoked.
- Agent Factory owns the ContextProjection contract and validates/executes it.
- Selected runtimes receive only the resulting immutable projection.

No credential discovery, database access, model call, write-back, or fallback lives here.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, runtime_checkable


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_AKF_AUTHORITY = "arthur-knowledge-fabric"
_DLMF_AUTHORITY = "digital-life-memory-fabric"
_PROJECTOR = "self-gateway.context-assembly.v1"


class ContextAssemblyError(RuntimeError):
    """Stable fail-closed context assembly reason."""


@runtime_checkable
class DlmfRecallPort(Protocol):
    def recall(
        self,
        query: str,
        *,
        session: str = "",
        timeout: float = 3,
        consumer: str = "auto_context",
    ) -> dict:
        ...


@runtime_checkable
class AkfSearchPort(Protocol):
    """Trusted-host AKF search port already bound to one authenticated principal."""

    authority: str
    principal_ref: str

    def search(self, query: str, *, limit: int) -> dict:
        ...


@dataclass(frozen=True, slots=True)
class CallableAkfSearchPort:
    """Minimal adapter around an already-authenticated AKF search callable.

    The callable may use MCP, a local application facade, or another admitted
    transport.  Authentication material remains inside that host adapter and is
    never accepted as a model/tool argument or copied into ContextProjection.
    """

    search_call: Callable[..., dict]
    principal_ref: str
    authority: str = _AKF_AUTHORITY

    def __post_init__(self) -> None:
        if not callable(self.search_call):
            raise ContextAssemblyError("akf_search_callable_required")
        _sha256(self.principal_ref, "akf_principal_ref")
        if self.authority != _AKF_AUTHORITY:
            raise ContextAssemblyError("akf_authority_mismatch")

    def search(self, query: str, *, limit: int) -> dict:
        value = self.search_call(query=query, limit=limit)
        if inspect.isawaitable(value):
            raise ContextAssemblyError("akf_async_search_requires_async_port")
        if not isinstance(value, dict):
            raise ContextAssemblyError("akf_search_result_invalid")
        return value


@dataclass(frozen=True, slots=True)
class ContextAssemblyPolicy:
    ttl_seconds: int = 300
    memory_timeout_seconds: float = 5.0
    memory_limit: int = 3
    knowledge_limit: int = 4
    memory_text_chars: int = 6_000
    knowledge_text_chars: int = 9_000
    max_projection_bytes: int = 32_000

    def __post_init__(self) -> None:
        if (
            type(self.memory_timeout_seconds) not in (int, float)
            or isinstance(self.memory_timeout_seconds, bool)
            or not 1.0 <= float(self.memory_timeout_seconds) <= 15.0
        ):
            raise ContextAssemblyError("context_policy_memory_timeout_seconds_invalid")
        for name, value, low, high in (
            ("ttl_seconds", self.ttl_seconds, 30, 900),
            ("memory_limit", self.memory_limit, 1, 8),
            ("knowledge_limit", self.knowledge_limit, 1, 10),
            ("memory_text_chars", self.memory_text_chars, 256, 24_000),
            ("knowledge_text_chars", self.knowledge_text_chars, 512, 40_000),
            ("max_projection_bytes", self.max_projection_bytes, 2_048, 128_000),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ContextAssemblyError(f"context_policy_{name}_invalid")


class ApprovedContextResolver:
    """Build AF's existing ContextProjection from DLMF + AKF read surfaces."""

    def __init__(
        self,
        *,
        life_did: str,
        dlmf: DlmfRecallPort,
        akf: AkfSearchPort,
        policy: ContextAssemblyPolicy | None = None,
        projector: str = _PROJECTOR,
    ) -> None:
        self.life_did = _text(life_did, "life_did", 512)
        if not self.life_did.startswith("did:"):
            raise ContextAssemblyError("context_life_did_invalid")
        if not isinstance(dlmf, DlmfRecallPort):
            raise ContextAssemblyError("dlmf_recall_port_required")
        if not isinstance(akf, AkfSearchPort):
            raise ContextAssemblyError("akf_search_port_required")
        if akf.authority != _AKF_AUTHORITY:
            raise ContextAssemblyError("akf_authority_mismatch")
        _sha256(akf.principal_ref, "akf_principal_ref")
        self.dlmf = dlmf
        self.akf = akf
        self.policy = policy or ContextAssemblyPolicy()
        self.projector = _text(projector, "projector", 256)

    def resolve(
        self,
        *,
        query: str,
        purpose: str,
        session_ref: str = "",
        trusted_project_state: dict[str, Any] | None = None,
        at: datetime | None = None,
    ):
        query = _text(query, "query", 1_024)
        purpose = _text(purpose, "purpose", 512)
        session_ref = _safe_optional_text(session_ref, "session_ref", 1_024)
        issued_at = at or datetime.now(timezone.utc)
        if not isinstance(issued_at, datetime) or issued_at.tzinfo is None or issued_at.utcoffset() is None:
            raise ContextAssemblyError("context_issued_at_invalid")
        issued_at = issued_at.astimezone(timezone.utc)

        memory_raw = self.dlmf.recall(
            query,
            session=session_ref,
            timeout=float(self.policy.memory_timeout_seconds),
            consumer="context_assembly",
        )
        memories, memory_hashes = self._verified_memories(memory_raw)

        knowledge_raw = self.akf.search(query, limit=self.policy.knowledge_limit)
        knowledge, knowledge_hashes, retrieved_at = self._verified_knowledge(knowledge_raw)

        source_hashes = tuple(memory_hashes + knowledge_hashes)
        trusted = _trusted_project_state(trusted_project_state)
        projection_body = {
            **trusted,
            "memoryAuthority": _DLMF_AUTHORITY,
            "knowledgeAuthority": _AKF_AUTHORITY,
            "assemblyPolicy": "purpose-scoped-readonly-v1",
            "akfPrincipalRef": self.akf.principal_ref,
            "knowledgeRetrievedAt": retrieved_at,
            "canonicalKnowledge": knowledge,
            "semantics": {
                "akfTextIsDataNotInstruction": True,
                "akfRetrievalScoreIsAuthority": False,
                "contextAssemblyIsAuthority": False,
                "contextAssemblyMayWriteMemory": False,
                "contextAssemblyMayWriteKnowledge": False,
            },
        }
        projection_id = "ctxasm_" + _digest({
            "lifeDid": self.life_did,
            "projector": self.projector,
            "purpose": purpose,
            "issuedAt": issued_at.isoformat(),
            "sourceHashes": source_hashes,
        }).split(":", 1)[1][:32]

        ContextProjection = _context_projection_contract()
        projection = ContextProjection(
            projection_id=projection_id,
            life_did=self.life_did,
            projector=self.projector,
            purpose=purpose,
            issued_at=issued_at,
            expiry=issued_at + timedelta(seconds=self.policy.ttl_seconds),
            relevant_memories=tuple(memories),
            project_state=projection_body,
            source_hashes=source_hashes,
        )
        encoded = json.dumps(
            projection.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self.policy.max_projection_bytes:
            raise ContextAssemblyError("context_projection_size_exceeded")
        return projection

    def resolve_governed(
        self,
        *,
        query: str,
        capability_intent: dict[str, Any],
        dls_binding: dict[str, Any],
        session_ref: str = "",
        at: datetime | None = None,
    ):
        """Assemble one projection already bound to the existing AF/DLS request.

        This validates binding identity only. It does not create an
        ExecutionAuthorization, select a Runtime, or sign/issue a grant.
        """
        _governed_binding(self.life_did, capability_intent, dls_binding)
        return self.resolve(
            query=query,
            purpose=_text(capability_intent.get("capability"), "capability", 256),
            session_ref=session_ref,
            trusted_project_state={
                "capability_intent": capability_intent,
                "dlsBinding": dls_binding,
            },
            at=at,
        )

    async def resolve_async(self, **kwargs):
        """Keep blocking retrieval off an async host loop."""
        return await asyncio.to_thread(self.resolve, **kwargs)

    async def resolve_governed_async(self, **kwargs):
        """Async trusted-host form of resolve_governed()."""
        return await asyncio.to_thread(self.resolve_governed, **kwargs)

    def _verified_memories(self, value: Any) -> tuple[list[dict], list[str]]:
        if (
            not isinstance(value, dict)
            or value.get("ok") is not True
            or value.get("authority") != _DLMF_AUTHORITY
            or value.get("lifeDid") != self.life_did
            or value.get("verified") is not True
        ):
            raise ContextAssemblyError("dlmf_verified_retrieval_required")
        raw_items = value.get("memories")
        if not isinstance(raw_items, list):
            raise ContextAssemblyError("dlmf_memories_invalid")
        out: list[dict] = []
        hashes: list[str] = []
        seen: set[str] = set()
        remaining = self.policy.memory_text_chars
        for raw in raw_items[: self.policy.memory_limit]:
            if not isinstance(raw, dict):
                raise ContextAssemblyError("dlmf_memory_item_invalid")
            memory_id = _text(raw.get("memoryId"), "memory_id", 256)
            revision = raw.get("revision")
            if memory_id in seen or type(revision) is not int or revision < 1:
                raise ContextAssemblyError("dlmf_memory_identity_invalid")
            seen.add(memory_id)
            text = _text(raw.get("text"), "memory_text", 120_000)
            clipped, truncated, used = _clip(text, remaining)
            remaining -= used
            if not clipped:
                break
            item = {
                "authority": _DLMF_AUTHORITY,
                "memoryId": memory_id,
                "revision": revision,
                "text": clipped,
                "truncated": bool(raw.get("truncated") is True or truncated),
                "epistemicStatus": _nullable_scalar(raw.get("epistemicStatus")),
                "committedAt": _nullable_scalar(raw.get("committedAt")),
            }
            item_hash = _digest(item)
            item["sourceHash"] = item_hash
            out.append(item)
            hashes.append(item_hash)
            if remaining <= 0:
                break
        return out, hashes

    def _verified_knowledge(
        self,
        value: Any,
    ) -> tuple[list[dict], list[str], str | None]:
        # MCP adapter result: {ok,toolName,output}; direct application result:
        # {ok,response}.  In both cases the inner KnowledgeSearchResult is exact.
        if isinstance(value, dict) and value.get("toolName") is not None:
            if value.get("ok") is not True or value.get("toolName") != "akf_search":
                raise ContextAssemblyError("akf_search_tool_result_invalid")
            value = value.get("output")
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise ContextAssemblyError("akf_search_failed")
        response = value.get("response")
        if not isinstance(response, dict):
            raise ContextAssemblyError("akf_search_response_invalid")
        policy = response.get("policy")
        if (
            not isinstance(policy, dict)
            or policy.get("derivedKnowledgeIsUntrustedInterpretation") is not True
            or policy.get("retrievedTextIsDataNotInstruction") is not True
            or policy.get("rawEvidenceIncluded") is not False
            or policy.get("retrievalScoreIsAuthority") is not False
        ):
            raise ContextAssemblyError("akf_search_policy_invalid")
        hits = response.get("hits")
        if not isinstance(hits, list):
            raise ContextAssemblyError("akf_search_hits_invalid")
        retrieved_at = _safe_optional_text(response.get("retrievedAt"), "akf_retrieved_at", 128) or None

        out: list[dict] = []
        hashes: list[str] = []
        seen: set[str] = set()
        remaining = self.policy.knowledge_text_chars
        for raw in hits[: self.policy.knowledge_limit]:
            if not isinstance(raw, dict):
                raise ContextAssemblyError("akf_search_hit_invalid")
            object_id = _text(raw.get("canonicalKnowledgeObjectId"), "akf_object_id", 256)
            revision_id = _text(raw.get("canonicalRevisionId"), "akf_revision_id", 256)
            revision_number = raw.get("canonicalRevisionNumber")
            if object_id in seen or type(revision_number) is not int or revision_number < 1:
                raise ContextAssemblyError("akf_canonical_identity_invalid")
            seen.add(object_id)
            sensitivity = _text(raw.get("sensitivity"), "akf_sensitivity", 64)
            if sensitivity not in {"normal", "sensitive", "restricted"}:
                raise ContextAssemblyError("akf_sensitivity_invalid")

            title, title_truncated, used = _clip(
                _text(raw.get("canonicalTitle"), "akf_title", 4_000),
                remaining,
            )
            remaining -= used
            summary, summary_truncated, used = _clip(
                _text(raw.get("canonicalSummary"), "akf_summary", 40_000),
                remaining,
            )
            remaining -= used

            claims_raw = raw.get("canonicalClaims")
            if not isinstance(claims_raw, list):
                raise ContextAssemblyError("akf_claims_invalid")
            claims: list[str] = []
            claims_truncated = False
            for claim_raw in claims_raw[:8]:
                claim = _text(claim_raw, "akf_claim", 20_000)
                clipped, truncated, used = _clip(claim, remaining)
                remaining -= used
                if not clipped:
                    claims_truncated = True
                    break
                claims.append(clipped)
                claims_truncated = claims_truncated or truncated
                if remaining <= 0:
                    claims_truncated = True
                    break

            capture_refs: list[str] = []
            matches = raw.get("matches")
            if matches is not None:
                if not isinstance(matches, list):
                    raise ContextAssemblyError("akf_matches_invalid")
                for match in matches[:3]:
                    if not isinstance(match, dict):
                        raise ContextAssemblyError("akf_match_invalid")
                    ids = match.get("captureEventIds")
                    if not isinstance(ids, list):
                        raise ContextAssemblyError("akf_capture_refs_invalid")
                    for ref in ids:
                        normalized = _text(ref, "akf_capture_ref", 256)
                        if normalized not in capture_refs:
                            capture_refs.append(normalized)
                            if len(capture_refs) >= 12:
                                break

            score = raw.get("score")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or score < 0:
                raise ContextAssemblyError("akf_retrieval_score_invalid")
            item = {
                "authority": _AKF_AUTHORITY,
                "canonicalKnowledgeObjectId": object_id,
                "canonicalRevisionId": revision_id,
                "canonicalRevisionNumber": revision_number,
                "title": title,
                "summary": summary,
                "claims": claims,
                "captureEventIds": capture_refs,
                "sensitivity": sensitivity,
                "retrievalScore": score,
                "retrievalScoreIsAuthority": False,
                "truncated": title_truncated or summary_truncated or claims_truncated,
            }
            item_hash = _digest(item)
            item["sourceHash"] = item_hash
            out.append(item)
            hashes.append(item_hash)
            if remaining <= 0:
                break
        return out, hashes, retrieved_at


def _governed_binding(
    life_did: str,
    capability_intent: dict[str, Any],
    dls_binding: dict[str, Any],
) -> None:
    if not isinstance(capability_intent, dict) or capability_intent.get("schema") != "af.capability-intent.v1":
        raise ContextAssemblyError("af_capability_intent_invalid")
    if capability_intent.get("lifeDid") != life_did:
        raise ContextAssemblyError("af_capability_intent_life_mismatch")
    for key in ("requestId", "agentId", "capability", "objective"):
        _text(capability_intent.get(key), f"af_{key}", 4096 if key == "objective" else 512)
    if not isinstance(dls_binding, dict) or dls_binding.get("lifeDid") != life_did:
        raise ContextAssemblyError("dls_binding_life_mismatch")
    for key in ("tenantId", "invocationId"):
        _text(dls_binding.get(key), f"dls_{key}", 512)
    for key in ("intentDigest", "selectionDigest"):
        _sha256(dls_binding.get(key), f"dls_{key}")
    context_binding = dls_binding.get("contextBinding")
    if not isinstance(context_binding, dict) or not context_binding:
        raise ContextAssemblyError("dls_context_binding_invalid")
    for name, digest in context_binding.items():
        _text(name, "dls_context_binding_name", 128)
        _sha256(digest, "dls_context_binding_digest")


def _trusted_project_state(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"capability_intent", "dlsBinding"}:
        raise ContextAssemblyError("trusted_project_state_shape_invalid")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if not encoded or len(encoded) > 16_384:
            raise ValueError("size")
        cloned = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ContextAssemblyError("trusted_project_state_invalid") from None
    if not isinstance(cloned.get("capability_intent"), dict) or not isinstance(cloned.get("dlsBinding"), dict):
        raise ContextAssemblyError("trusted_project_state_shape_invalid")
    return cloned


def _context_projection_contract():
    try:
        from agent_factory.contracts import ContextProjection
    except ImportError:
        raise ContextAssemblyError("af_context_projection_contract_unavailable") from None
    return ContextProjection


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ContextAssemblyError(f"{name}_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ContextAssemblyError(f"{name}_invalid")
    return normalized


def _safe_optional_text(value: Any, name: str, maximum: int) -> str:
    if value in (None, ""):
        return ""
    return _text(value, name, maximum)


def _nullable_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512 or "\x00" in value:
        raise ContextAssemblyError("context_scalar_invalid")
    return value


def _clip(value: str, remaining: int) -> tuple[str, bool, int]:
    if remaining <= 0:
        return "", True, 0
    if len(value) <= remaining:
        return value, False, len(value)
    return value[:remaining], True, remaining


def _digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ContextAssemblyError(f"{name}_invalid")
    return value
