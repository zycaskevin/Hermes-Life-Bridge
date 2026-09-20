"""Candidate-only resident Digital Life skill proposal tool.

The conversational runtime may suggest a reusable procedure. It cannot install a
skill, grant a capability, choose another life, or attach raw conversation data.
Evidence is bound later by DLS after DLMF emits a normalized experience reference.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .deployment_context import _load as load_runtime_manifest

TOOLSET = "digital_life"
TOOL = "digital_life_skill_propose"
POLICY_SCHEMA = "hlb.digital-life-skill-proposals.v1"
PROPOSAL_SCHEMA = "digital-life.skill-proposal.v1"
RESIDENT_BINDING_SCHEMA = "digital-life-stack.resident-skill-binding.v1"
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CAPABILITY = re.compile(r"[a-z0-9]+(?:[._:-][a-z0-9]+)*\Z")
_BLOCKED_TEXT = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"(?:^|\s)/(?:home|Users|root)/\S+"),
    re.compile(r"https?://\S+", re.I),
)

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL,
        "description": (
            "Propose a reusable procedural skill candidate learned from this conversation. "
            "Use only for a general reusable procedure, never for a one-off instruction, user-specific "
            "fact, memory, credential, URL, path, or permission grant. The proposal is quarantined and "
            "inactive until DLMF evidence and Agent Factory governance are satisfied. This tool does "
            "not install or activate a skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$", "maxLength": 64,
                    "description": "Portable skill name, lowercase kebab-case.",
                },
                "description": {
                    "type": "string", "minLength": 1, "maxLength": 512,
                    "description": "Generic reusable purpose; no private facts.",
                },
                "instructions": {
                    "type": "string", "minLength": 1, "maxLength": 8192,
                    "description": "Reusable procedure only. No secrets, user facts, URLs, filesystem paths or tool permissions.",
                },
                "capability_id": {
                    "type": "string", "minLength": 1, "maxLength": 128,
                    "description": "Capability ID from the owner-approved proposal allowlist.",
                },
            },
            "required": ["name", "description", "instructions", "capability_id"],
            "additionalProperties": False,
        },
    },
}


class SkillProposalBoundaryError(ValueError):
    pass


def _private_json(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SkillProposalBoundaryError("skill_policy_untrusted")
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            raise SkillProposalBoundaryError("skill_policy_too_large")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise SkillProposalBoundaryError("skill_policy_invalid") from exc
    if not isinstance(value, dict):
        raise SkillProposalBoundaryError("skill_policy_invalid")
    return value


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SkillProposalBoundaryError(label + "_invalid")
    value = value.strip()
    if not value or "\x00" in value or len(value.encode("utf-8")) > limit:
        raise SkillProposalBoundaryError(label + "_invalid")
    return value


def _procedure_text(value: Any, label: str, limit: int) -> str:
    value = _text(value, label, limit)
    if any(pattern.search(value) for pattern in _BLOCKED_TEXT):
        raise SkillProposalBoundaryError(label + "_private_or_location_data")
    return value


def _safe_id(value: Any, pattern: re.Pattern[str], label: str, limit: int) -> str:
    value = _text(value, label, limit)
    if pattern.fullmatch(value) is None:
        raise SkillProposalBoundaryError(label + "_invalid")
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _absolute_regular(path: Path, label: str, *, private: bool) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise SkillProposalBoundaryError(label + "_invalid")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or (private and info.st_mode & 0o077)
        ):
            raise SkillProposalBoundaryError(label + "_untrusted")
    finally:
        os.close(fd)
    return path


def _load_resident_binding(config: BridgeConfig) -> tuple[dict[str, Any], Path]:
    path = Path(config.skill_proposal_binding_file)
    value = _private_json(path)
    expected = {
        "schema",
        "digitalLifeId",
        "lifeDid",
        "runtimeId",
        "bindingMode",
        "proposalToolPolicy",
        "hermes",
        "lifetimeHub",
        "identityRoot",
        "developmentEvidence",
        "manifestHash",
    }
    if set(value) != expected:
        raise SkillProposalBoundaryError("resident_binding_fields")
    body = {k: v for k, v in value.items() if k != "manifestHash"}
    if (
        value.get("schema") != RESIDENT_BINDING_SCHEMA
        or value.get("lifeDid") != config.life_did
        or value.get("runtimeId") != "hermes"
        or value.get("bindingMode") != "existing-resident"
        or value.get("proposalToolPolicy") != "candidate-only"
        or value.get("manifestHash") != _canonical_hash(body)
    ):
        raise SkillProposalBoundaryError("resident_binding_invalid")
    digital_life_id = _text(value.get("digitalLifeId"), "digital_life_id", 128)
    hermes = value.get("hermes")
    lifetime = value.get("lifetimeHub")
    identity = value.get("identityRoot")
    development = value.get("developmentEvidence")
    if not all(isinstance(item, dict) for item in (hermes, lifetime, identity, development)):
        raise SkillProposalBoundaryError("resident_binding_nested_invalid")
    if set(hermes) != {"home", "stateDb"}:
        raise SkillProposalBoundaryError("resident_hermes_fields")
    home = Path(_text(hermes.get("home"), "hermes_home", 2048))
    state_db = Path(_text(hermes.get("stateDb"), "hermes_state_db", 2048))
    if not home.is_absolute() or home.is_symlink() or state_db != home / "state.db":
        raise SkillProposalBoundaryError("resident_hermes_binding_invalid")
    home_info = home.stat()
    if (
        not home_info.st_uid == os.getuid()
        or not stat.S_ISDIR(home_info.st_mode)
        or home_info.st_mode & 0o022
    ):
        raise SkillProposalBoundaryError("resident_hermes_home_untrusted")
    _absolute_regular(state_db, "resident_hermes_state", private=True)
    if set(lifetime) != {
        "database", "companionId", "genesisHash", "schemaProfile"
    }:
        raise SkillProposalBoundaryError("resident_lifetime_fields")
    lifetime_db = Path(_text(lifetime.get("database"), "lifetime_db", 2048))
    _absolute_regular(lifetime_db, "resident_lifetime_db", private=False)
    if lifetime.get("schemaProfile") != "legacy-continuity-v1":
        raise SkillProposalBoundaryError("resident_lifetime_schema_profile")
    companion_id = _text(lifetime.get("companionId"), "companion_id", 128)
    genesis_hash = _text(lifetime.get("genesisHash"), "genesis_hash", 128)
    if not re.fullmatch(r"[0-9a-f]{64}", genesis_hash):
        raise SkillProposalBoundaryError("resident_genesis_hash_invalid")
    if set(identity) != {"rootHash", "subjectId", "displayName"}:
        raise SkillProposalBoundaryError("resident_identity_fields")
    root_hash = _text(identity.get("rootHash"), "identity_root_hash", 128)
    if not re.fullmatch(r"[0-9a-f]{64}", root_hash):
        raise SkillProposalBoundaryError("resident_identity_root_hash_invalid")
    _text(identity.get("subjectId"), "identity_subject_id", 128)
    _text(identity.get("displayName"), "identity_display_name", 256)
    if set(development) != {"status", "reason"}:
        raise SkillProposalBoundaryError("resident_development_fields")
    if development.get("status") != "BLOCKED":
        raise SkillProposalBoundaryError("resident_development_status")
    _text(development.get("reason"), "resident_development_reason", 256)
    root = path.parent.absolute()
    if root.is_symlink():
        raise SkillProposalBoundaryError("resident_binding_root_symlink")
    info = root.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise SkillProposalBoundaryError("resident_binding_root_untrusted")
    proposal_root = root / "skill-proposals"
    return {
        **value,
        "digitalLifeId": digital_life_id,
        "_proposalRoot": str(proposal_root),
        "_companionId": companion_id,
    }, root


class SkillProposalTool:
    def __init__(self, config: BridgeConfig):
        if not config.skill_proposals_enabled:
            raise SkillProposalBoundaryError("skill_proposals_disabled")
        self.life_did = config.life_did
        if config.skill_proposal_binding_file:
            self.manifest, self.root = _load_resident_binding(config)
            self.digital_life_id = self.manifest["digitalLifeId"]
        else:
            if not config.agent_tools_enabled:
                raise SkillProposalBoundaryError("skill_proposals_require_agent_tools_or_resident_binding")
            if not config.deployment_manifest_file:
                raise SkillProposalBoundaryError("deployment_manifest_missing")
            self.manifest = load_runtime_manifest(config.life_did, config.deployment_manifest_file)
            if self.manifest["hermes"].get("toolPolicy") != "governed-readonly":
                raise SkillProposalBoundaryError("tool_policy_not_granted")
            self.root = Path(config.deployment_manifest_file).parent.resolve()
            self.digital_life_id = self.manifest["digitalLifeId"]
        policy = _private_json(self.root / "skill-proposals-policy.json")
        if set(policy) != {
            "schema", "digitalLifeId", "lifeDid", "allowedCapabilityIds",
            "maxInstructionsBytes", "maxPending"
        }:
            raise SkillProposalBoundaryError("skill_policy_fields")
        if (policy.get("schema") != POLICY_SCHEMA
                or policy.get("digitalLifeId") != self.digital_life_id
                or policy.get("lifeDid") != self.life_did):
            raise SkillProposalBoundaryError("skill_policy_scope")
        allowed = policy.get("allowedCapabilityIds")
        if not isinstance(allowed, list) or not 1 <= len(allowed) <= 32:
            raise SkillProposalBoundaryError("skill_policy_capabilities")
        normalized = []
        for item in allowed:
            normalized.append(_safe_id(item, _CAPABILITY, "capability_id", 128))
        if len(set(normalized)) != len(normalized):
            raise SkillProposalBoundaryError("skill_policy_capabilities")
        if type(policy.get("maxInstructionsBytes")) is not int or not 256 <= policy["maxInstructionsBytes"] <= 8192:
            raise SkillProposalBoundaryError("skill_policy_instruction_limit")
        if type(policy.get("maxPending")) is not int or not 1 <= policy["maxPending"] <= 256:
            raise SkillProposalBoundaryError("skill_policy_pending_limit")
        self.allowed_capabilities = frozenset(normalized)
        self.max_instructions = policy["maxInstructionsBytes"]
        self.max_pending = policy["maxPending"]
        self.pending = self.root / "skill-proposals" / "pending"
        if self.pending.is_symlink():
            raise SkillProposalBoundaryError("skill_pending_symlink")
        self.pending.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.pending, 0o700)

    def propose(self, args: dict[str, Any], *, session_id: str, turn_id: str) -> dict[str, Any]:
        if not isinstance(args, dict) or set(args) != {"name", "description", "instructions", "capability_id"}:
            raise SkillProposalBoundaryError("arguments_invalid")
        session_id = _text(session_id, "session_id", 256)
        turn_id = _text(turn_id, "turn_id", 256)
        name = _safe_id(args["name"], _NAME, "name", 64)
        capability_id = _safe_id(args["capability_id"], _CAPABILITY, "capability_id", 128)
        if capability_id not in self.allowed_capabilities:
            raise SkillProposalBoundaryError("capability_not_owner_allowed")
        description = _procedure_text(args["description"], "description", 512)
        instructions = _procedure_text(args["instructions"], "instructions", self.max_instructions)
        bound_dir = self.root / "skill-proposals" / "bound"
        active_pending = [
            path for path in self.pending.glob("*.json")
            if not (bound_dir / path.name).is_file()
        ]
        if len(active_pending) >= self.max_pending:
            raise SkillProposalBoundaryError("pending_skill_limit")
        identity_material = "\0".join([
            self.life_did, session_id, turn_id, name, capability_id, description, instructions
        ])
        digest = _hash(identity_material)
        proposal_id = "skillprop:" + digest[:40]
        value = {
            "schema": PROPOSAL_SCHEMA,
            "proposalId": proposal_id,
            "digitalLifeId": self.digital_life_id,
            "lifeDid": self.life_did,
            "runtimeId": "hermes",
            "name": name,
            "version": "0.1.0",
            "description": description,
            "instructions": instructions,
            "capabilityId": capability_id,
            "sourceSessionId": session_id,
            "sourceSessionHash": "sha256:" + _hash(session_id),
            "sourceTurnHash": "sha256:" + _hash(turn_id),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "evidenceStatus": "PENDING_DLMF_REFERENCE",
            "canonical": False,
            "active": False,
        }
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        target = self.pending / (proposal_id.replace(":", "-") + ".json")
        identity_keys = (
            "schema", "proposalId", "digitalLifeId", "lifeDid", "runtimeId",
            "name", "version", "description", "instructions", "capabilityId",
            "sourceSessionId", "sourceSessionHash", "sourceTurnHash",
            "evidenceStatus", "canonical", "active",
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC
        temporary = self.pending / f".{target.name}.{secrets.token_hex(8)}.pending"
        try:
            fd = os.open(temporary, flags, 0o600)
            try:
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                existing = _private_json(target)
                if any(existing.get(key) != value.get(key) for key in identity_keys):
                    raise SkillProposalBoundaryError("proposal_idempotency_conflict")
                value = existing
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "status": "pending_evidence_sync",
            "lifeDid": self.life_did,
            "capabilityId": capability_id,
            "candidateActive": False,
            "skillInstalled": False,
            "notice": (
                "Candidate only. DLMF evidence, Agent Factory validation and capability authority "
                "are still required; do not claim this skill is learned or available yet."
            ),
        }


def enabled() -> bool:
    try:
        SkillProposalTool(BridgeConfig.from_env())
        return True
    except Exception:
        return False


def handler(args: dict, **kwargs) -> str:
    try:
        tool = SkillProposalTool(BridgeConfig.from_env())
        result = tool.propose(
            args,
            session_id=str(kwargs.get("session_id") or ""),
            # Hermes model-tool dispatch provides task_id + session_id to handlers.
            # turn_id is a post-tool-hook field; accept it only when a future host
            # supplies it, otherwise bind the proposal to the host task id.
            turn_id=str(kwargs.get("turn_id") or kwargs.get("task_id") or ""),
        )
    except SkillProposalBoundaryError as exc:
        result = {"ok": False, "error": str(exc), "executed": False}
    except Exception:
        result = {"ok": False, "error": "skill_proposal_unavailable", "executed": False}
    return json.dumps(result, ensure_ascii=False)
