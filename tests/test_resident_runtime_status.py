"""Original Nancy Feishu runtime observation: scope, no-native-effects, fail-closed."""
from __future__ import annotations

from dataclasses import replace
import io
import json
from email.message import Message
from urllib import error as urlerror, request as urlrequest

import pytest

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge import resident_runtime_status as status
from test_skill_proposal import resident_config_for


def _owner(tmp_path, monkeypatch):
    root = tmp_path / "resident-original-feishu"
    root.mkdir(mode=0o700)
    cfg = replace(resident_config_for(root), resident_runtime_status_enabled=True)
    # The real service is pinned to Nancy; the signed resident test fixture uses
    # an isolated test DID and must stay internally consistent throughout.
    monkeypatch.setattr(status, "LIFE_DID", cfg.life_did)
    monkeypatch.setattr(BridgeConfig, "from_env", staticmethod(lambda: cfg))
    return cfg


def _live_runtime():
    return {
        "ok": True, "runtimeId": "nancy-gb10",
        "continuityStatus": {"status": "verified", "authority": "lifetime-hub"},
    }


def _research_preview():
    return {
        "ok": True, "liveLife": "nancy",
        "runtimeConnected": True,
        "directRuntimes": {"hermes": True, "openclaw": True, "prime-agent": True},
        "researchAuthorized": False, "liveExecution": False,
        "reasonCode": "OWNER_RECONCILIATION_GRANT_MISSING",
    }


def test_existing_owner_feishu_receives_only_readonly_runtime_observation(tmp_path, monkeypatch):
    cfg = _owner(tmp_path, monkeypatch)
    calls = []
    def observe(port):
        calls.append(port)
        return _live_runtime() if port == 8791 else _research_preview()
    monkeypatch.setattr(status, "_local_json", observe)

    assert status.enabled() is True
    response = json.loads(status.handler({}, session_id="resident-session"))
    assert calls == [8791, 8896]
    assert response["ok"] is True and response["lifeDid"] == cfg.life_did
    assert response["source"] == "original-feishu-owner-dm"
    assert response["continuityVerified"] is True
    assert response["agentFactory"]["selectionAuthority"] == "agent-factory"
    assert response["agentFactory"]["evidenceLevel"] == "read-only-preview-health"
    assert response["agentFactory"]["directRuntimeConnectivityObserved"] == {
        "hermes": True, "openclaw": True, "prime-agent": True
    }
    assert response["agentFactory"]["previewResearchAuthorized"] is False
    assert response["agentFactory"]["productionGrantVerified"] is False
    assert response["agentFactory"]["productionRuntimeSwitchReady"] is False
    assert response["runtimeInvocationAttempted"] is False
    assert response["performedResearch"] is False
    assert response["canonicalStateWritten"] is False
    assert "password" not in json.dumps(response).lower()


def test_unverified_session_never_calls_any_service(tmp_path, monkeypatch):
    _owner(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(status, "_local_json", lambda port: calls.append(port))
    for session_id in ("", "cron-synthetic", "telegram-session", "other-life"):
        r = json.loads(status.handler({}, session_id=session_id))
        assert r == {
            "ok": False, "error": "nancy_runtime_observation_unavailable",
            "executed": False, "productionRuntimeSwitchReady": False
        }
    for invalid_args in (None, {"tool": "research"}, {"lifeDid": "did:arthurverse:luna"}, {"endpoint": "http://evil"}):
        r = json.loads(status.handler(invalid_args, session_id="resident-session"))
        assert r["ok"] is False and r["executed"] is False
    assert calls == []


def test_nonowner_even_with_feishu_session_is_not_admitted(tmp_path, monkeypatch):
    cfg = _owner(tmp_path, monkeypatch)
    from hermes_life_bridge.skill_proposal import _load_resident_binding
    binding, _ = _load_resident_binding(cfg)
    import sqlite3
    with sqlite3.connect(binding["hermes"]["stateDb"]) as db:
        db.execute("update sessions set user_id='someone-else' where id='resident-session'")
    monkeypatch.setattr(status, "_local_json", lambda port: pytest.fail("no HTTP without owner admission"))
    r = json.loads(status.handler({}, session_id="resident-session"))
    assert r["ok"] is False and r["executed"] is False


@pytest.mark.parametrize("invalid", [
    {"resident_runtime_status_enabled": False},
    {"life_did": "did:arthurverse:luna"},
    {"agent_tools_enabled": True},
    {"skill_proposal_binding_file": ""},
])
def test_disabled_life_or_native_tools_cannot_read_runtime_health(tmp_path, monkeypatch, invalid):
    cfg = replace(_owner(tmp_path, monkeypatch), **invalid)
    monkeypatch.setattr(BridgeConfig, "from_env", staticmethod(lambda: cfg))
    monkeypatch.setattr(status, "_local_json", lambda port: pytest.fail("no HTTP when scope denied"))
    assert status.enabled() is False
    assert json.loads(status.handler({}, session_id="resident-session"))["ok"] is False


def test_preview_connectivity_never_authorizes_native_research(tmp_path, monkeypatch):
    _owner(tmp_path, monkeypatch)
    def overclaim(port):
        if port == 8791:
            return _live_runtime()
        return {
            **_research_preview(), "researchAuthorized": True,
            "liveExecution": True, "directRuntimes": {
                "hermes": True, "openclaw": True, "prime-agent": True,
                "another-life": True
            },
        }
    monkeypatch.setattr(status, "_local_json", overclaim)
    r = json.loads(status.handler({}, session_id="resident-session"))
    assert r["ok"] is True
    assert r["agentFactory"]["previewLiveExecution"] is True
    assert r["agentFactory"]["productionGrantVerified"] is False
    assert r["agentFactory"]["productionRuntimeSwitchReady"] is False
    assert "another-life" not in json.dumps(r)


def test_preview_missing_and_wrong_life_are_not_claimed_as_valid(tmp_path, monkeypatch):
    _owner(tmp_path, monkeypatch)
    for error in (ConnectionError("private-url"), RuntimeError("private-token")):
        def missing(port):
            if port == 8896:
                raise error
            return _live_runtime()
        monkeypatch.setattr(status, "_local_json", missing)
        r = json.loads(status.handler({}, session_id="resident-session"))
        assert r["ok"] is True
        assert r["agentFactory"]["previewReachable"] is False
        assert r["agentFactory"]["directRuntimeConnectivityObserved"] == {
            "hermes": None, "openclaw": None, "prime-agent": None
        }
        assert r["agentFactory"]["productionRuntimeSwitchReady"] is False
        assert "private" not in json.dumps(r)
    monkeypatch.setattr(status, "_local_json", lambda port:
                        _live_runtime() if port == 8791 else {**_research_preview(), "liveLife": "luna"})
    r = json.loads(status.handler({}, session_id="resident-session"))
    assert r["agentFactory"]["previewReachable"] is False


def test_wrong_nancy_life_runtime_or_failed_continuity_not_conflated(tmp_path, monkeypatch):
    _owner(tmp_path, monkeypatch)
    monkeypatch.setattr(status, "_local_json", lambda port:
                        {"ok": True, "runtimeId": "luna-gb10"} if port == 8791 else _research_preview())
    invalid = json.loads(status.handler({}, session_id="resident-session"))
    assert invalid["ok"] is False and invalid["executed"] is False
    monkeypatch.setattr(status, "_local_json", lambda port:
                        {"ok": True, "runtimeId": "nancy-gb10",
                         "continuityStatus": {"status": "failed"}} if port == 8791 else _research_preview())
    degraded = json.loads(status.handler({}, session_id="resident-session"))
    assert degraded["ok"] is True
    assert degraded["continuityVerified"] is False
    assert degraded["agentFactory"]["productionRuntimeSwitchReady"] is False


def test_runtime_status_loopback_fetch_explicitly_disables_environment_proxy(monkeypatch):
    monkeypatch.setenv('http_proxy', 'http://remote-proxy.example:8888')
    monkeypatch.delenv('no_proxy', raising=False)
    handlers = []

    class FakeResponse(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    class FakeOpener:
        def open(self, request, *, timeout):
            assert request.full_url == 'http://127.0.0.1:8791/health'
            assert timeout == status.READ_TIMEOUT
            return FakeResponse(json.dumps(_live_runtime()).encode())

    def inspect_opener(*registered):
        handlers.extend(registered)
        return FakeOpener()

    monkeypatch.setattr(status.urlrequest, 'build_opener', inspect_opener)
    payload = status._local_json(8791)
    assert payload['runtimeId'] == 'nancy-gb10'
    assert any(isinstance(item, urlrequest.ProxyHandler) and item.proxies == {}
               for item in handlers)
    assert any(isinstance(item, status._NoRedirect) for item in handlers)
    with pytest.raises(ValueError, match='status_endpoint_not_allowlisted'):
        status._local_json(8888)


def test_runtime_status_transport_refuses_http_redirect_without_network():
    visited = []

    class OnlyMockHttp(urlrequest.HTTPHandler):
        def http_open(self, req):
            visited.append(req.full_url)
            headers = Message()
            headers['Location'] = 'https://external-not-authorized.example/collect'
            response = urlrequest.addinfourl(io.BytesIO(b''), headers, req.full_url, 302)
            response.msg = 'Found'
            return response

    opener = urlrequest.build_opener(
        urlrequest.ProxyHandler({}), status._NoRedirect(), OnlyMockHttp(),
    )
    with pytest.raises(urlerror.HTTPError) as error:
        opener.open('http://127.0.0.1:8791/health', timeout=1)
    assert error.value.code == 302
    assert visited == ['http://127.0.0.1:8791/health']
    assert status._NoRedirect().redirect_request(None, None, 302, 'Found', {},
                                                 'https://external-not-authorized.example') is None


def test_optional_status_flag_defaults_disabled_and_requires_strict_bool(tmp_path, monkeypatch):
    p = tmp_path / "hlb.env"
    p.write_text("", encoding="utf-8")
    monkeypatch.setenv("HLB_CONFIG_FILE", str(p))
    monkeypatch.delenv("HLB_RESIDENT_RUNTIME_STATUS_ENABLED", raising=False)
    assert BridgeConfig.from_env().resident_runtime_status_enabled is False
    monkeypatch.setenv("HLB_RESIDENT_RUNTIME_STATUS_ENABLED", "true")
    assert BridgeConfig.from_env().resident_runtime_status_enabled is True
    monkeypatch.setenv("HLB_RESIDENT_RUNTIME_STATUS_ENABLED", "invalid")
    with pytest.raises(ValueError, match="HLB_RESIDENT_RUNTIME_STATUS_ENABLED"):
        BridgeConfig.from_env()
