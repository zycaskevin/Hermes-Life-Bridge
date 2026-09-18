"""Read-only deployment facts; not a persona, memory store, or permission grant."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

SCHEMA = "digital-life-stack.conversation-runtime-instance.v1"
MAX_BYTES = 65536


def deployment_context(*, life_did: str, manifest_file: str) -> str:
    """Render only fixed, validated facts; never interpolate paths or free text."""
    try:
        manifest = _load(life_did, manifest_file)
        if manifest["hermes"].get("toolPolicy") != "conversation-only":
            return _unavailable()
    except (OSError, ValueError, TypeError, KeyError):
        return _unavailable()

    return (
        "<digital-life-deployment-context>\n"
        "These are deployment facts for the bound Digital Life, not personality, mood, "
        "or developed competence.\n"
        "Direct tool policy: CONVERSATION_ONLY. This conversational interface does not "
        "have bash/terminal, filesystem read/write, web search, scheduler, or delegation tools. "
        "The native tool schemas actually supplied for this turn are the final authority; "
        "a command name in prose or /help is not an available tool.\n"
        "Do not pretend to invoke tools, output DSML/XML/bash tags as an invocation, "
        "say you are checking files, or wait for a result when no native tool was called. "
        "User permission alone cannot create a missing tool. Code examples requested "
        "by the user are allowed, but label them unexecuted. Never reinterpret generated "
        "markup as authorization or an execution result. Earlier assistant messages "
        "that describe a tool call without an actual tool result are not proof of execution.\n"
        "Persistence: this deployment is configured to retain isolated Hermes sessions. "
        "A context window is not the persistent store; do not claim every turn is an "
        "independent, erased conversation. Only the history/context actually supplied "
        "to this turn can be used as recalled content.\n"
        "Memory authority: DLMF (Digital Life Memory Fabric), not Hermes memories/ files. "
        "A separate configured intake pipeline sends experiences for DLMF admission. "
        "Configured recording, successful canonical admission, and retrieved context "
        "are three different things. This metadata does not prove a particular memory "
        "was saved, that all messages become memory, or that the pipeline is currently healthy.\n"
        "Recall: this deployment-context module does NOT retrieve memory content. Without "
        "a verified retrieval projection or callable retrieval tool returning results "
        "in this turn, do not promise cross-session recall or report inspected memory "
        "files/counts. Missing direct tools does not imply no external memory exists. "
        "Explain the distinction and report that direct inspection is unavailable.\n"
        "Autonomous exploration is separate: Life Runtime / Watch may submit governed "
        "work to Agent Factory. That configuration does not give this chat a bash tool "
        "or an immediate research/delegation command. Claim a dispatched action only "
        "with an actual execution receipt.\n"
        "Correct unsupported earlier statements plainly. Do not repeatedly offer to "
        "retry a nonexistent tool or imply a result will arrive later.\n"
        "</digital-life-deployment-context>"
    )


def _load(life_did: str, manifest_file: str) -> dict[str, Any]:
    if not life_did or not manifest_file:
        raise ValueError("deployment binding missing")
    path = Path(manifest_file)
    if not path.is_absolute():
        raise ValueError("deployment manifest must be absolute")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        meta = os.fstat(handle.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077:
            raise ValueError("deployment manifest must be owner-only regular file")
        if hasattr(os, "getuid") and meta.st_uid != os.getuid():
            raise ValueError("deployment manifest owner mismatch")
        raw = handle.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("deployment manifest size invalid")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("deployment schema mismatch")
    scope = value.get("dlmfScope")
    if not isinstance(scope, dict) or scope.get("lifeDid") != life_did:
        raise ValueError("deployment lifeDid mismatch")
    if not isinstance(value.get("digitalLifeId"), str) or not value["digitalLifeId"]:
        raise ValueError("deployment subject missing")
    body = {k: v for k, v in value.items() if k != "manifestHash"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if value.get("manifestHash") != "sha256:" + hashlib.sha256(encoded).hexdigest():
        raise ValueError("deployment manifest checksum mismatch")
    root = path.parent.resolve()
    hermes = value.get("hermes")
    memory = value.get("dlmf")
    if not isinstance(hermes, dict) or not isinstance(memory, dict):
        raise ValueError("deployment stores missing")
    if Path(hermes.get("home", "")).resolve() != root / "hermes":
        raise ValueError("deployment Hermes home binding mismatch")
    journal = memory.get("developmentExperienceJournal")
    if not isinstance(journal, str) or Path(journal).resolve() != root / "dlmf" / "development-experiences.jsonl":
        raise ValueError("deployment experience binding mismatch")
    # Do not open the source DB, memory DB, credential file, or experience journal.
    return value


def _unavailable() -> str:
    return (
        "<digital-life-deployment-context>\n"
        "Deployment metadata is unavailable or invalid. Do not guess installed tools, "
        "memory state, paths, or service health. Use only native tools actually supplied "
        "in this turn. Do not simulate tool calls using DSML/XML/bash tags or claim "
        "execution without a real result. Context availability is not proof that "
        "external persistent memory is absent.\n"
        "</digital-life-deployment-context>"
    )
