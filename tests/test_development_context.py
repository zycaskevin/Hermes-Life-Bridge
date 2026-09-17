from __future__ import annotations

import json
from pathlib import Path

from hermes_life_bridge.development_context import DevelopmentContextProjector


def projection(life_did: str = "did:example:life") -> dict:
    return {
        "schema": "digital-life-development.ontogeny-runtime-projection.v1",
        "authority": "digital-life-development",
        "projectionNature": "DERIVED_REBUILDABLE",
        "digitalLifeId": "dl_subject_a",
        "lifeDid": life_did,
        "generatedAt": "2026-09-18T00:00:00Z",
        "sourceStateHash": "sha256:" + "a" * 64,
        "sourceDevelopmentRevision": 0,
        "evidence": {"total": 2, "real": 2, "synthetic": 0, "other": 0},
        "milestones": {"count": 0},
        "growthTimeline": {"count": 0},
        "personality": {"state": "UNFORMED", "revision": None},
        "selfModel": {"state": "UNFORMED"},
        "capabilities": [],
        "learningFrontier": [],
        "phase": "EARLY_FORMATION",
        "expressionPolicy": {
            "generalWorldKnowledge": "LATENT_SUBSTRATE_AVAILABLE",
            "developedCompetenceAuthority": "DLD_ONLY",
            "inventedPersonalHistory": "FORBIDDEN",
            "inventedPreferenceOrTrait": "FORBIDDEN",
            "undevelopedComplexTaskMode": "CLARIFY_BEFORE_FULL_PLAN_UNLESS_EXPLICIT",
            "selfExpertiseClaims": "ONLY_FOR_DLD_DEVELOPED_CAPABILITIES",
        },
    }


def write_projection(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "projection.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_early_formation_context_separates_latent_knowledge_from_developed_competence(
    tmp_path: Path,
) -> None:
    path = write_projection(tmp_path, projection())
    context = DevelopmentContextProjector(
        life_did="did:example:life",
        projection_file=str(path),
    ).context()
    assert "phase=EARLY_FORMATION" in context
    assert "evidence_total=2" in context
    assert "developed_capabilities=none" in context
    assert "latent substrate knowledge" in context
    assert "first learn the user's key goals/constraints" in context
    assert "Do not imitate baby talk" in context


def test_cross_life_projection_fails_safe_without_leaking_other_identity(
    tmp_path: Path,
) -> None:
    path = write_projection(tmp_path, projection("did:example:other"))
    context = DevelopmentContextProjector(
        life_did="did:example:life",
        projection_file=str(path),
    ).context()
    assert "temporarily unavailable or invalid" in context
    assert "did:example:other" not in context
    assert "dl_subject_a" not in context


def test_non_owner_only_projection_fails_safe(tmp_path: Path) -> None:
    path = write_projection(tmp_path, projection())
    path.chmod(0o640)
    context = DevelopmentContextProjector(
        life_did="did:example:life",
        projection_file=str(path),
    ).context()
    assert "temporarily unavailable or invalid" in context


def test_developed_capability_is_rendered_without_raw_evidence(tmp_path: Path) -> None:
    value = projection()
    value["phase"] = "DEVELOPING"
    value["selfModel"] = {"state": "EMERGING"}
    value["capabilities"] = [
        {
            "capabilityId": "travel-planning",
            "ownership": "DEVELOPING",
            "stage": "LEARNING",
            "competenceScore": 0.3,
            "competenceConfidence": 0.5,
            "autonomy": "SUPERVISED",
        }
    ]
    value["expressionPolicy"]["undevelopedComplexTaskMode"] = (
        "CALIBRATE_TO_DEVELOPED_CAPABILITY"
    )
    path = write_projection(tmp_path, value)
    context = DevelopmentContextProjector(
        life_did="did:example:life",
        projection_file=str(path),
    ).context()
    assert "travel-planning[DEVELOPING/LEARNING]" in context
    assert "Calibrate initiative and confidence" in context
    assert "evidence-real" not in context
