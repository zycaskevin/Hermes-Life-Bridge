"""Reduce authenticated Hermes Runs events into DLS native callback metadata.

This is an evidence adapter, not a tool executor. Raw previews are hashed only
at the trusted runtime boundary and never emitted. A locally correlated ID is
NOT claimed to be a Hermes tool-call ID. Ambiguous same-name calls without IDs
are rejected instead of guessed. Model prose and reasoning are never evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .runtime_adapter import RuntimeBridgeError


def digest(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError):
        raise RuntimeBridgeError("hermes_event_invalid_json") from None
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _text(value: Any, bound: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > bound
            or any(ord(ch) < 32 for ch in value)):
        raise RuntimeBridgeError("hermes_event_identity_invalid")
    return value


def reduce_native_events(events: Sequence[Mapping[str, Any]], *, run_id: str,
                         invocation_id: str, allowed_tools: set[str],
                         max_calls: int, emit: Callable[[Mapping[str, Any]], None]) -> dict:
    """Emit only validated, paired native observations; no raw payload leaves.

    The caller authenticates events and binds the terminal result to this exact
    run. A nonempty runtime result preview is evidence of the reported tool
    output, not a verified publication date, full page, or independent fact check.
    A missing preview yields no evidence ref and cannot satisfy DLS semantic roles.
    """
    _text(run_id, 256)
    _text(invocation_id, 128)
    if (not isinstance(events, (tuple, list)) or not 1 <= len(events) <= 1000
            or type(max_calls) is not int or not 1 <= max_calls <= 32
            or not isinstance(allowed_tools, set) or not allowed_tools):
        raise RuntimeBridgeError("hermes_event_bounds_invalid")
    run_ref = digest(run_id)
    pending: dict[str, tuple[str, str]] = {}
    all_ids: set[str] = set()
    normalized: list[dict] = []
    terminal = False
    correlation: set[str] = set()
    for raw in events:
        if not isinstance(raw, Mapping):
            raise RuntimeBridgeError("hermes_event_invalid")
        kind = raw.get("event")
        if not isinstance(kind, str) or len(kind) > 100:
            raise RuntimeBridgeError("hermes_event_invalid")
        if raw.get("run_id", run_id) != run_id:
            raise RuntimeBridgeError("hermes_event_run_mismatch")
        if kind == "replay.truncated":
            raise RuntimeBridgeError("hermes_event_replay_truncated")
        if kind in {"tool.started", "tool.completed"}:
            if terminal:
                raise RuntimeBridgeError("hermes_tool_after_terminal")
            tool = raw.get("tool")
            if not isinstance(tool, str) or tool not in allowed_tools:
                raise RuntimeBridgeError("hermes_unapproved_tool_observed")
            remote_id = raw.get("tool_call_id")
            if remote_id is not None:
                remote_id = _text(remote_id, 256)
                key = "native:" + digest(remote_id)
                mode = "native_id"
            else:
                key = "serial:" + tool
                mode = "serialized_observation"
            if kind == "tool.started":
                if len(all_ids) >= max_calls or key in pending or (remote_id is not None and key in all_ids):
                    raise RuntimeBridgeError("hermes_ambiguous_or_excess_native_calls")
                # A name-only completion cannot be matched to one of several
                # simultaneous starts. Do not mix identified and name-only calls.
                if any(t == tool and (m != mode or mode == "serialized_observation")
                       for t, m in pending.values()):
                    raise RuntimeBridgeError("hermes_ambiguous_native_calls")
                logical = (key if remote_id is not None else
                           "serial:" + str(len(all_ids)) + ":" + digest(tool))
                all_ids.add(logical)
                pending[key] = (tool, mode)
                # Track the actual start's correlation, not a generated result ID.
                normalized.append({"kind": "call", "invocationId": invocation_id,
                    "nativeToolName": tool, "toolCallId": logical, "evidenceRefs": [],
                    "latencyMs": None})
                correlation.add(mode)
            else:
                if pending.get(key) != (tool, mode):
                    raise RuntimeBridgeError("hermes_unpaired_native_result")
                start = next((e for e in reversed(normalized) if e["kind"] == "call"
                    and e["nativeToolName"] == tool and
                    (remote_id is None or e["toolCallId"] == key)), None)
                if start is None:
                    raise RuntimeBridgeError("hermes_unpaired_native_result")
                del pending[key]
                error = raw.get("error")
                if type(error) is not bool:
                    raise RuntimeBridgeError("hermes_tool_outcome_missing")
                duration = raw.get("duration")
                latency = None
                if duration is not None:
                    if (type(duration) not in (int, float) or not math.isfinite(duration)
                            or not 0 <= duration <= 180):
                        raise RuntimeBridgeError("hermes_tool_duration_invalid")
                    latency = int(duration * 1000)
                preview = raw.get("preview")
                refs = []
                if preview is not None:
                    if not isinstance(preview, str) or len(preview.encode()) > 262144:
                        raise RuntimeBridgeError("hermes_tool_preview_invalid")
                    if preview.strip() and not error:
                        refs = [digest({"kind": "hermes_runtime_result_preview",
                            "runRef": run_ref, "callRef": digest(start["toolCallId"]),
                            "tool": tool, "preview": preview})]
                item = {"kind": "error" if error else "result", "invocationId": invocation_id,
                    "nativeToolName": tool, "toolCallId": start["toolCallId"],
                    "evidenceRefs": refs, "latencyMs": latency}
                if error:
                    item["errorClass"] = "NATIVE_TOOL_FAILED"
                normalized.append(item)
        elif kind == "run.completed":
            if terminal or pending:
                raise RuntimeBridgeError("hermes_terminal_or_pairing_invalid")
            terminal = True
        elif kind in {"run.failed", "run.cancelled", "run.interrupted"}:
            raise RuntimeBridgeError("hermes_terminal_mismatch")
        elif kind.startswith(("tool.", "approval.", "subagent.")):
            # This first native readonly rail has no delegated or approval-gated
            # effects. Do not hide them behind a seemingly clean parent receipt.
            raise RuntimeBridgeError("hermes_unapproved_execution_event")
        # Non-tool text/lifecycle/reasoning values are never inspected or copied.
    if not terminal or pending or not all_ids:
        raise RuntimeBridgeError("hermes_native_evidence_incomplete")
    # All local pairing checks pass before emission. DLS independently validates
    # the events and poisons the attempt if any callback is rejected.
    for item in normalized:
        emit(item)
    return {"runRef": run_ref, "callCount": len(all_ids),
            "correlation": sorted(correlation),
            "observationRef": digest(normalized)}
