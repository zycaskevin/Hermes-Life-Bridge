"""Owner-scoped, read-only runtime observability for Nancy's existing Feishu DM.

The DLS research preview's direct-runtime health booleans are NOT Agent Factory
admission, host attestation or permission to execute. This plugin issues no AF
request, runtime invocation, signed grant or canonical memory operation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib import request as urlrequest

from .config import BridgeConfig

TOOL = "digital_life_runtime_status"
TOOLSET = "digital_life"
SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL,
        "description": (
            "Check Nancy's current Digital Life runtime and multi-Runtime research "
            "readiness using existing read-only control-plane observations. "
            "This does NOT authorize or perform research, choose a Runtime, or change "
            "identity, memory or configuration. A connected Runtime is not an "
            "Agent Factory production execution grant."
        ),
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
}
LIFE_DID = "did:arthurverse:nancy"
RUNTIME_ID = "nancy-gb10"
RUNTIMES = ("hermes", "openclaw", "prime-agent")
MAX_READ_BYTES = 16384
READ_TIMEOUT = 2.5


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _verified_owner_binding(config: BridgeConfig):
    if (not config.resident_runtime_status_enabled or config.agent_tools_enabled
            or config.life_did != LIFE_DID or not config.skill_proposal_binding_file):
        raise ValueError("runtime_observation_not_enabled")
    from .skill_proposal import _load_resident_binding, _load_real_conversation_policy

    manifest, root = _load_resident_binding(config)
    policy = _load_real_conversation_policy(root)
    sources = policy.get("sources")
    if (
        manifest.get("lifeDid") != LIFE_DID
        or not isinstance(sources, tuple) or len(sources) != 1
        or sources[0].get("source") != "feishu"
        or sources[0].get("chatTypes") != ("dm",)
    ):
        raise ValueError("runtime_observation_owner_source_invalid")
    return Path(manifest["hermes"]["stateDb"]), policy


def enabled() -> bool:
    try:
        _verified_owner_binding(BridgeConfig.from_env())
        return True
    except Exception:
        return False


def _local_json(port: int) -> dict:
    # Never accept an endpoint, URL, path or credential from the LLM/user.
    if port not in (8791, 8896):
        raise ValueError("status_endpoint_not_allowlisted")
    request = urlrequest.Request(f"http://127.0.0.1:{port}/health", method="GET")
    # A fixed loopback URL is not sufficient when the host has http_proxy set.
    # Never delegate Nancy's local health observations to an external proxy.
    with urlrequest.build_opener(urlrequest.ProxyHandler({}), _NoRedirect()).open(
        request, timeout=READ_TIMEOUT
    ) as response:
        if response.status != 200:
            raise ValueError("status_endpoint_not_healthy")
        raw = response.read(MAX_READ_BYTES + 1)
    if len(raw) > MAX_READ_BYTES:
        raise ValueError("status_response_too_large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("status_response_invalid")
    return result


def handler(args: dict, **kwargs) -> str:
    try:
        if not isinstance(args, dict) or args:
            raise ValueError("runtime_status_arguments_invalid")
        config = BridgeConfig.from_env()
        source_db, policy = _verified_owner_binding(config)
        from .skill_proposal import _assert_real_resident_session

        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("runtime_status_session_missing")
        _assert_real_resident_session(source_db, policy, session_id)

        life = _local_json(8791)
        if life.get("ok") is not True or life.get("runtimeId") != RUNTIME_ID:
            raise ValueError("nancy_life_runtime_identity_invalid")
        continuity = life.get("continuityStatus")
        continuity_verified = isinstance(continuity, dict) and continuity.get("status") == "verified"

        # A preview health endpoint can describe connectivity. It cannot
        # issue, verify, or imply production research authorization.
        try:
            preview = _local_json(8896)
            valid_preview = preview.get("ok") is True and preview.get("liveLife") == "nancy"
        except Exception:
            preview, valid_preview = {}, False
        direct = preview.get("directRuntimes") if valid_preview else None
        direct = direct if isinstance(direct, dict) else {}

        result = {
            "schema": "hlb.nancy-existing-resident-runtime-observation.v1",
            "ok": True,
            "lifeDid": LIFE_DID,
            "observedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "original-feishu-owner-dm",
            "runtimeId": RUNTIME_ID,
            "continuityVerified": continuity_verified,
            "agentFactory": {
                "selectionAuthority": "agent-factory",
                "evidenceLevel": "read-only-preview-health",
                "previewReachable": valid_preview,
                "directRuntimeConnectivityObserved": {
                    name: (direct.get(name) is True) if valid_preview else None
                    for name in RUNTIMES
                },
                "previewResearchAuthorized": preview.get("researchAuthorized") is True if valid_preview else None,
                "previewLiveExecution": preview.get("liveExecution") is True if valid_preview else None,
                "productionGrantVerified": False,
                "productionRuntimeSwitchReady": False,
                "productionExecutionPermittedByThisTool": False,
            },
            "performedResearch": False,
            "runtimeInvocationAttempted": False,
            "canonicalStateWritten": False,
            "notice": (
                "Runtime health and preview connectivity are not production authorization. "
                "No research, Runtime selection or tool invocation was performed. "
                "A signed owner decision, model authentication, and a verified hard budget "
                "must pass the governed execution path separately."
            ),
        }
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        # Fail closed without disclosing private session, owner identity, URLs,
        # local files, API tokens or error strings from the underlying services.
        return json.dumps({
            "ok": False,
            "error": "nancy_runtime_observation_unavailable",
            "executed": False,
            "productionRuntimeSwitchReady": False,
        })
