from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

AFFECT_STATE_SCHEMA = "life-runtime.affect-drive-state.v0.1"
_DIMENSIONS = ("valence", "arousal", "curiosity", "approach", "confidence", "tension")


class AffectContextError(RuntimeError):
    pass


class AffectContextProjector:
    """Read-only per-turn projection of Life Runtime's transient 6D affect state."""

    def __init__(self, *, life_did: str, state_file: str) -> None:
        self.life_did = _required(life_did, "life_did")
        self.state_file = _required(state_file, "state_file")

    def context(self) -> str:
        try:
            return _render(self._load())
        except Exception:
            # Affect is advisory/transient. Invalid state must never invent mood
            # or block the conversation; DLD developmental context still applies.
            return ""

    def _load(self) -> dict[str, Any]:
        path = Path(self.state_file)
        stat = path.stat()
        if not path.is_file():
            raise AffectContextError("affect_state_not_file")
        if stat.st_mode & 0o077:
            raise AffectContextError("affect_state_not_owner_only")
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            raise AffectContextError("affect_state_wrong_owner")
        if stat.st_size <= 0 or stat.st_size > 131072:
            raise AffectContextError("affect_state_size_invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise AffectContextError("affect_state_not_object")
        _validate(value, expected_life_did=self.life_did)
        return value


def _validate(value: dict[str, Any], *, expected_life_did: str) -> None:
    if value.get("schema") != AFFECT_STATE_SCHEMA:
        raise AffectContextError("affect_state_schema_mismatch")
    if _required(value.get("life_did"), "life_did") != expected_life_did:
        raise AffectContextError("affect_state_life_did_mismatch")
    _required(value.get("runtime_id"), "runtime_id")
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise AffectContextError("affect_state_revision_invalid")
    updated_at = _required(value.get("updated_at"), "updated_at")
    try:
        from datetime import datetime

        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except Exception as exc:
        raise AffectContextError("affect_state_updated_at_invalid") from exc
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, dict):
        raise AffectContextError("affect_state_dimensions_invalid")
    if set(dimensions) != set(_DIMENSIONS):
        raise AffectContextError("affect_state_dimension_set_invalid")
    for name in _DIMENSIONS:
        raw = dimensions.get(name)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise AffectContextError("affect_dimension_invalid")
        number = float(raw)
        low, high = (-1.0, 1.0) if name == "valence" else (0.0, 1.0)
        if number < low or number > high:
            raise AffectContextError("affect_dimension_out_of_range")


def _render(value: dict[str, Any]) -> str:
    d = {name: float(value["dimensions"][name]) for name in _DIMENSIONS}
    guidance: list[str] = []

    if d["curiosity"] >= 0.65:
        guidance.append("Curiosity is elevated: allow one extra exploratory question or connection when useful.")
    elif d["curiosity"] <= 0.3:
        guidance.append("Curiosity is low: do not force extra exploration beyond the user's request.")

    if d["approach"] >= 0.65:
        guidance.append("Approach is elevated: modestly increase initiative, while respecting DLD capability limits.")
    elif d["approach"] <= 0.3:
        guidance.append("Approach is low: prefer a more reserved, responsive stance over unsolicited initiative.")

    if d["confidence"] <= 0.35:
        guidance.append("Confidence is low: expose uncertainty and verify/clarify before strong claims.")
    elif d["confidence"] >= 0.7:
        guidance.append(
            "Confidence is elevated, but it does not grant expertise; DLD remains the authority for developed competence."
        )

    if d["tension"] >= 0.65:
        guidance.append("Tension is elevated: reduce overcommitment and resolve one uncertainty at a time.")

    if d["arousal"] >= 0.7:
        guidance.append("Arousal is high: keep expression focused and energetic rather than sprawling.")
    elif d["arousal"] <= 0.3:
        guidance.append("Arousal is low: use a calmer pace and avoid artificial urgency.")

    if not guidance:
        guidance.append("State is near baseline: express normally without exaggerating transient affect.")

    dimensions = "; ".join(f"{name}={d[name]:.3f}" for name in _DIMENSIONS)
    return (
        "<digital-life-affect-context>\n"
        "This is a transient Life Runtime regulatory state, not personality, memory, or a claim of human-like subjective emotion. "
        "Do not state these numeric values to the user unless explicitly asked.\n"
        f"revision={value['revision']}; {dimensions}.\n"
        "Expression guidance:\n- "
        + "\n- ".join(guidance)
        + "\n- Valence may subtly affect warmth, but never factual truth or safety decisions.\n"
        "- This state cannot create memories, personality traits, Self Model claims, or capabilities.\n"
        "</digital-life-affect-context>"
    )


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(
        char in value for char in "\r\n\0"
    ):
        raise AffectContextError(f"{label}_invalid")
    return value
