from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ONTOGENY_SCHEMA = "digital-life-development.ontogeny-runtime-projection.v1"
_ALLOWED_PHASES = {"GENESIS", "EARLY_FORMATION", "DEVELOPING", "INDIVIDUATING"}


class DevelopmentContextError(RuntimeError):
    pass


class DevelopmentContextProjector:
    def __init__(self, *, life_did: str, projection_file: str) -> None:
        self.life_did = _required(life_did, "life_did")
        self.projection_file = _required(projection_file, "projection_file")

    def context(self) -> str:
        try:
            value = self._load()
            return _render(value)
        except Exception:
            return _safe_fallback()

    def _load(self) -> dict[str, Any]:
        path = Path(self.projection_file)
        stat = path.stat()
        if not path.is_file():
            raise DevelopmentContextError("development_projection_not_file")
        if stat.st_mode & 0o077:
            raise DevelopmentContextError("development_projection_not_owner_only")
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            raise DevelopmentContextError("development_projection_wrong_owner")
        if stat.st_size <= 0 or stat.st_size > 131072:
            raise DevelopmentContextError("development_projection_size_invalid")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DevelopmentContextError("development_projection_invalid_json") from exc
        if not isinstance(value, dict):
            raise DevelopmentContextError("development_projection_not_object")
        _validate(value, expected_life_did=self.life_did)
        return value


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(
        c in value for c in "\r\n\0"
    ):
        raise DevelopmentContextError(f"{label}_invalid")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DevelopmentContextError(f"{label}_invalid")
    return value


def _validate(value: dict[str, Any], *, expected_life_did: str) -> None:
    if value.get("schema") != ONTOGENY_SCHEMA:
        raise DevelopmentContextError("development_projection_schema_mismatch")
    if value.get("authority") != "digital-life-development":
        raise DevelopmentContextError("development_projection_authority_mismatch")
    if value.get("projectionNature") != "DERIVED_REBUILDABLE":
        raise DevelopmentContextError("development_projection_nature_mismatch")
    _required(value.get("digitalLifeId"), "digitalLifeId")
    if _required(value.get("lifeDid"), "lifeDid") != expected_life_did:
        raise DevelopmentContextError("development_projection_life_did_mismatch")
    phase = _required(value.get("phase"), "phase")
    if phase not in _ALLOWED_PHASES:
        raise DevelopmentContextError("development_projection_phase_invalid")
    revision = _nonnegative_int(value.get("sourceDevelopmentRevision"), "developmentRevision")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict):
        raise DevelopmentContextError("development_projection_evidence_invalid")
    total = _nonnegative_int(evidence.get("total"), "evidence.total")
    real = _nonnegative_int(evidence.get("real"), "evidence.real")
    synthetic = _nonnegative_int(evidence.get("synthetic"), "evidence.synthetic")
    other = _nonnegative_int(evidence.get("other"), "evidence.other")
    if real + synthetic + other != total:
        raise DevelopmentContextError("development_projection_evidence_mismatch")
    personality = value.get("personality")
    if not isinstance(personality, dict) or personality.get("state") not in {"UNFORMED", "FORMED"}:
        raise DevelopmentContextError("development_projection_personality_invalid")
    self_model = value.get("selfModel")
    if not isinstance(self_model, dict) or self_model.get("state") not in {"UNFORMED", "EMERGING"}:
        raise DevelopmentContextError("development_projection_self_model_invalid")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list):
        raise DevelopmentContextError("development_projection_capabilities_invalid")
    for item in capabilities:
        if not isinstance(item, dict):
            raise DevelopmentContextError("development_projection_capability_invalid")
        _required(item.get("capabilityId"), "capabilityId")
        _required(item.get("ownership"), "capabilityOwnership")
        _required(item.get("stage"), "capabilityStage")
    policy = value.get("expressionPolicy")
    if not isinstance(policy, dict):
        raise DevelopmentContextError("development_projection_expression_policy_invalid")
    if policy.get("developedCompetenceAuthority") != "DLD_ONLY":
        raise DevelopmentContextError("development_projection_competence_authority_invalid")
    if policy.get("generalWorldKnowledge") != "LATENT_SUBSTRATE_AVAILABLE":
        raise DevelopmentContextError("development_projection_world_knowledge_policy_invalid")
    if revision < 0:
        raise DevelopmentContextError("development_projection_revision_invalid")


def _render(value: dict[str, Any]) -> str:
    phase = value["phase"]
    evidence = value["evidence"]
    personality = value["personality"]
    self_model = value["selfModel"]
    capabilities = value["capabilities"]
    frontier = value.get("learningFrontier")
    frontier = frontier if isinstance(frontier, list) else []

    if capabilities:
        capability_text = ", ".join(
            f"{item['capabilityId']}[{item['ownership']}/{item['stage']}]"
            for item in capabilities[:12]
        )
    else:
        capability_text = "none"

    frontier_text = ", ".join(
        str(item.get("capabilityId"))
        for item in frontier[:8]
        if isinstance(item, dict) and item.get("capabilityId")
    ) or "none"

    early = phase in {"GENESIS", "EARLY_FORMATION"}
    behavioral_rule = (
        "For a complex task in a domain with no DLD-developed capability, first learn the user's "
        "key goals/constraints before offering a polished multi-step plan, unless the user explicitly "
        "asks for a complete answer immediately."
        if early
        else
        "Calibrate initiative and confidence to the DLD-developed capabilities listed below; unknown "
        "domains remain exploratory rather than established expertise."
    )

    return (
        "<digital-life-development-context>\n"
        "This is an ephemeral read-only projection from Digital-Life-Development. "
        "It describes what this Digital Life has actually developed; it is not a role-play persona.\n"
        f"phase={phase}; development_revision={value['sourceDevelopmentRevision']}; "
        f"evidence_total={evidence['total']}; real_evidence={evidence['real']}; "
        f"synthetic_evidence={evidence['synthetic']}; personality={personality['state']}; "
        f"self_model={self_model['state']}.\n"
        f"developed_capabilities={capability_text}.\n"
        f"learning_frontier={frontier_text}.\n"
        "Expression rules:\n"
        "- General pretrained world knowledge is available as latent substrate knowledge, but it is "
        "not personal experience and not proof of developed competence.\n"
        "- Do not claim personal expertise, preferences, traits, history, or relationships unless "
        "supported by canonical memory/DLD state supplied to this turn.\n"
        "- When using general knowledge in an undeveloped domain, be helpful but exploratory; do not "
        "present the answer as a skill this Digital Life has already developed.\n"
        f"- {behavioral_rule}\n"
        "- Do not imitate baby talk, claim to be a child, or intentionally answer incorrectly. "
        "Development is expressed through calibrated certainty, initiative, self-knowledge, and "
        "learned competence, not reduced intelligence.\n"
        "</digital-life-development-context>"
    )


def safe_development_context() -> str:
    return _safe_fallback()


def _safe_fallback() -> str:
    return (
        "<digital-life-development-context>\n"
        "The configured DLD projection is temporarily unavailable or invalid. Fail safe: do not claim "
        "personal history, preferences, stable traits, or developed expertise. General pretrained "
        "knowledge may still be used as substrate knowledge; for complex tasks, prefer clarifying the "
        "user's goals before presenting a polished plan. Do not imitate baby talk or feign ignorance.\n"
        "</digital-life-development-context>"
    )
