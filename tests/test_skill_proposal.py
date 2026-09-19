from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from unittest import mock

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge import skill_proposal as m

DID = "did:arthurverse:test-life"


def private(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def config_for(root: Path) -> BridgeConfig:
    body = {
        "schema": "digital-life-stack.conversation-runtime-instance.v1",
        "digitalLifeId": "dl_test",
        "dlmfScope": {"tenantId": "tenant-test", "lifeDid": DID, "memoryNamespace": "life"},
        "hermes": {"home": str(root / "hermes"), "toolPolicy": "governed-readonly"},
        "dlmf": {"developmentExperienceJournal": str(root / "dlmf/development-experiences.jsonl")},
        "development": {"stateDir": str(root / "dld")},
    }
    body["manifestHash"] = "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    private(root / "runtime-instance.json", body)
    private(root / "skill-proposals-policy.json", {
        "schema": m.POLICY_SCHEMA,
        "digitalLifeId": "dl_test",
        "lifeDid": DID,
        "allowedCapabilityIds": ["procedural.assistance", "research.project"],
        "maxInstructionsBytes": 2048,
        "maxPending": 3,
    })
    return BridgeConfig(
        life_did=DID,
        runtime_socket="unused",
        trace_path="unused",
        deployment_manifest_file=str(root / "runtime-instance.json"),
        agent_tools_enabled=True,
        skill_proposals_enabled=True,
    )


def proposal():
    return {
        "name": "evidence-review",
        "description": "Review execution evidence before drawing a conclusion.",
        "instructions": "Check completion first. Then check verification. State uncertainty if either is missing.",
        "capability_id": "procedural.assistance",
    }


def test_candidate_only_proposal_is_private_and_idempotent(tmp_path, monkeypatch):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    cfg = config_for(root)
    tool = m.SkillProposalTool(cfg)
    first = tool.propose(proposal(), session_id="session-a", turn_id="turn-a")
    second = tool.propose(proposal(), session_id="session-a", turn_id="turn-a")
    assert first == second
    assert first["candidateActive"] is False and first["skillInstalled"] is False
    files = list((root / "skill-proposals/pending").glob("*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o077 == 0
    value = json.loads(files[0].read_text())
    assert value["lifeDid"] == DID
    assert value["runtimeId"] == "hermes"
    assert value["evidenceStatus"] == "PENDING_DLMF_REFERENCE"
    assert value["canonical"] is False and value["active"] is False
    assert value["sourceSessionId"] == "session-a"
    assert "skill" not in {p.name for p in root.iterdir() if p.is_dir()}


def test_model_cannot_choose_life_runtime_permission_or_unknown_fields(tmp_path):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    tool = m.SkillProposalTool(config_for(root))
    for key, value in (
        ("lifeDid", "did:arthurverse:other"),
        ("runtimeId", "openclaw"),
        ("allowedTools", ["bash"]),
        ("path", "/tmp/x"),
        ("active", True),
    ):
        with pytest.raises(m.SkillProposalBoundaryError, match="arguments_invalid"):
            tool.propose({**proposal(), key: value}, session_id="s", turn_id="t")


def test_owner_capability_allowlist_is_enforced(tmp_path):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    tool = m.SkillProposalTool(config_for(root))
    with pytest.raises(m.SkillProposalBoundaryError, match="capability_not_owner_allowed"):
        tool.propose({**proposal(), "capability_id": "filesystem.write"}, session_id="s", turn_id="t")


@pytest.mark.parametrize("value", [
    "Use Bearer SECRET-TOKEN to call it.",
    "Read /home/alice/private.txt before deciding.",
    "Contact alice@example.com for approval.",
    "Visit https://example.com/private.",
    "Use sk-1234567890abcdefghijkl.",
])
def test_clear_private_location_and_credential_material_is_rejected(tmp_path, value):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    tool = m.SkillProposalTool(config_for(root))
    with pytest.raises(m.SkillProposalBoundaryError, match="private_or_location_data"):
        tool.propose({**proposal(), "instructions": value}, session_id="s", turn_id="t")


def test_session_and_turn_are_host_supplied_and_required(tmp_path):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    tool = m.SkillProposalTool(config_for(root))
    for session, turn in (("", "t"), ("s", ""), ("\n", "t")):
        with pytest.raises(m.SkillProposalBoundaryError):
            tool.propose(proposal(), session_id=session, turn_id=turn)


def test_interrupted_proposal_write_never_publishes_partial_final(tmp_path):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    tool = m.SkillProposalTool(config_for(root))
    with mock.patch("hermes_life_bridge.skill_proposal.os.write", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            tool.propose(proposal(), session_id="s", turn_id="t")
    pending = root / "skill-proposals/pending"
    assert list(pending.glob("*.json")) == []
    assert list(pending.glob("*.pending")) == []
    assert tool.propose(proposal(), session_id="s", turn_id="t")["ok"] is True


def test_bound_marker_frees_pending_budget_without_deleting_proposal_history(tmp_path):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    cfg = config_for(root)
    tool = m.SkillProposalTool(cfg)
    for index in range(3):
        result = tool.propose({**proposal(), "name": f"skill-{index}"}, session_id="s", turn_id=f"t{index}")
        self_name = result["proposal_id"].replace(":", "-") + ".json"
        if index == 0:
            bound = root / "skill-proposals/bound"; bound.mkdir(parents=True, mode=0o700)
            (bound / self_name).write_text("{}"); (bound / self_name).chmod(0o600)
    result = tool.propose({**proposal(), "name": "skill-new"}, session_id="s", turn_id="new")
    assert result["ok"] is True
    assert len(list((root / "skill-proposals/pending").glob("*.json"))) == 4


def test_pending_limit_and_policy_scope_fail_closed(tmp_path):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    cfg = config_for(root)
    tool = m.SkillProposalTool(cfg)
    for index in range(3):
        tool.propose({**proposal(), "name": f"skill-{index}"}, session_id="s", turn_id=f"t{index}")
    with pytest.raises(m.SkillProposalBoundaryError, match="pending_skill_limit"):
        tool.propose({**proposal(), "name": "skill-over"}, session_id="s", turn_id="over")

    policy = json.loads((root / "skill-proposals-policy.json").read_text())
    policy["lifeDid"] = "did:arthurverse:other"
    private(root / "skill-proposals-policy.json", policy)
    with pytest.raises(m.SkillProposalBoundaryError, match="skill_policy_scope"):
        m.SkillProposalTool(cfg)


def test_pending_symlink_and_world_readable_policy_rejected(tmp_path):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    cfg = config_for(root)
    policy = root / "skill-proposals-policy.json"
    policy.chmod(0o644)
    with pytest.raises(m.SkillProposalBoundaryError, match="skill_policy_untrusted"):
        m.SkillProposalTool(cfg)
    policy.chmod(0o600)
    target = root / "elsewhere"; target.mkdir()
    pending = root / "skill-proposals/pending"
    pending.parent.mkdir(mode=0o700)
    pending.symlink_to(target, target_is_directory=True)
    with pytest.raises(m.SkillProposalBoundaryError, match="skill_pending_symlink"):
        m.SkillProposalTool(cfg)


def test_handler_uses_host_task_id_when_model_dispatch_has_no_turn_id(tmp_path, monkeypatch):
    root = tmp_path / "runtime"; root.mkdir(mode=0o700)
    cfg = config_for(root)
    monkeypatch.setattr(m.BridgeConfig, "from_env", staticmethod(lambda: cfg))
    value = json.loads(m.handler(proposal(), session_id="session-host", task_id="task-host"))
    assert value["ok"] is True
    stored = json.loads(next((root / "skill-proposals/pending").glob("*.json")).read_text())
    assert stored["sourceSessionId"] == "session-host"
    assert stored["sourceTurnHash"] == "sha256:" + __import__("hashlib").sha256(b"task-host").hexdigest()


def test_handler_never_leaks_exception_or_writes_when_disabled(tmp_path, monkeypatch):
    cfg = BridgeConfig(life_did=DID, runtime_socket="x", trace_path="x")
    monkeypatch.setattr(m.BridgeConfig, "from_env", staticmethod(lambda: cfg))
    value = json.loads(m.handler(proposal(), session_id="s", turn_id="t"))
    assert value == {"ok": False, "error": "skill_proposals_disabled", "executed": False}
