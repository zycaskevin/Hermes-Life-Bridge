from __future__ import annotations

import json
from pathlib import Path

from hermes_life_bridge.affect_context import AffectContextProjector


def state(life_did: str = "did:example:life") -> dict:
    return {
        "schema": "life-runtime.affect-drive-state.v0.1",
        "runtime_id": "subject-life-runtime",
        "life_did": life_did,
        "revision": 4,
        "updated_at": "2026-09-18T00:00:00Z",
        "dimensions": {
            "valence": 0.12,
            "arousal": 0.72,
            "curiosity": 0.81,
            "approach": 0.68,
            "confidence": 0.31,
            "tension": 0.44,
        },
        "recent_appraisal_ids": [],
    }


def write_state(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "affect.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_affect_context_projects_six_dimensions_as_transient_expression_guidance(
    tmp_path: Path,
) -> None:
    path = write_state(tmp_path, state())
    context = AffectContextProjector(
        life_did="did:example:life",
        state_file=str(path),
    ).context()
    assert "transient Life Runtime regulatory state" in context
    for name in ("valence", "arousal", "curiosity", "approach", "confidence", "tension"):
        assert f"{name}=" in context
    assert "Curiosity is elevated" in context
    assert "Approach is elevated" in context
    assert "Confidence is low" in context
    assert "does not grant expertise" not in context
    assert "cannot create memories, personality traits" in context


def test_high_confidence_still_cannot_grant_dld_competence(tmp_path: Path) -> None:
    value = state()
    value["dimensions"]["confidence"] = 0.9
    path = write_state(tmp_path, value)
    context = AffectContextProjector(
        life_did="did:example:life",
        state_file=str(path),
    ).context()
    assert "does not grant expertise" in context
    assert "DLD remains the authority" in context


def test_cross_life_affect_state_is_silently_ignored(tmp_path: Path) -> None:
    path = write_state(tmp_path, state("did:example:other"))
    context = AffectContextProjector(
        life_did="did:example:life",
        state_file=str(path),
    ).context()
    assert context == ""


def test_non_owner_only_affect_state_is_silently_ignored(tmp_path: Path) -> None:
    path = write_state(tmp_path, state())
    path.chmod(0o640)
    context = AffectContextProjector(
        life_did="did:example:life",
        state_file=str(path),
    ).context()
    assert context == ""
