from __future__ import annotations

import pytest

from hermes_life_bridge import agent_tools
from hermes_life_bridge.config import BridgeConfig


def test_bridge_config_memory_recall_timeout_defaults_and_bounds(tmp_path, monkeypatch):
    config_file = tmp_path / "hlb.env"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("HLB_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("HLB_MEMORY_RECALL_TIMEOUT_SECONDS", raising=False)

    assert BridgeConfig.from_env().memory_recall_timeout_seconds == 5.0

    monkeypatch.setenv("HLB_MEMORY_RECALL_TIMEOUT_SECONDS", "7.5")
    assert BridgeConfig.from_env().memory_recall_timeout_seconds == 7.5

    monkeypatch.setenv("HLB_MEMORY_RECALL_TIMEOUT_SECONDS", "0.5")
    with pytest.raises(ValueError, match="HLB_MEMORY_RECALL_TIMEOUT_SECONDS_out_of_range"):
        BridgeConfig.from_env()

    monkeypatch.setenv("HLB_MEMORY_RECALL_TIMEOUT_SECONDS", "16")
    with pytest.raises(ValueError, match="HLB_MEMORY_RECALL_TIMEOUT_SECONDS_out_of_range"):
        BridgeConfig.from_env()


def test_resident_manual_recall_budget_covers_dlmf_verified_retrieval(monkeypatch, tmp_path):
    import os
    path = tmp_path / "empty-hlb.env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("HLB_CONFIG_FILE", str(path))
    monkeypatch.delenv("HLB_RESIDENT_RECALL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("HLB_MEMORY_RECALL_TIMEOUT_SECONDS", raising=False)
    cfg = BridgeConfig.from_env()
    # DLMF can spend 20s searching primary + historical scopes in parallel.
    assert cfg.resident_recall_timeout_seconds == 30.0
    assert cfg.memory_recall_timeout_seconds == 5.0
    monkeypatch.setenv("HLB_RESIDENT_RECALL_TIMEOUT_SECONDS", "30")
    assert BridgeConfig.from_env().resident_recall_timeout_seconds == 30.0
    for invalid in ("1", "41", "nan", "inf"):
        monkeypatch.setenv("HLB_RESIDENT_RECALL_TIMEOUT_SECONDS", invalid)
        with pytest.raises(ValueError, match="HLB_RESIDENT_RECALL_TIMEOUT_SECONDS"):
            BridgeConfig.from_env()
    monkeypatch.setenv("HLB_RESIDENT_RECALL_TIMEOUT_SECONDS", "40")
    assert BridgeConfig.from_env().resident_recall_timeout_seconds == 40.0


def test_auto_recall_uses_configured_memory_timeout(monkeypatch):
    config = BridgeConfig(
        life_did="did:arthurverse:test-life",
        runtime_socket="unused",
        trace_path="unused",
        auto_recall_enabled=True,
        agent_tools_enabled=False,
        memory_recall_timeout_seconds=6.25,
        memory_recall_binding_file="/bound/memory-binding.json",
    )
    seen: dict[str, object] = {}

    class FakeMemoryClient:
        def __init__(self, received_config: BridgeConfig):
            assert received_config is config

        def recall(self, query: str, *, session: str, timeout: float, consumer: str):
            seen.update(
                query=query,
                session=session,
                timeout=timeout,
                consumer=consumer,
            )
            return {
                "ok": True,
                "authority": "digital-life-memory-fabric",
                "memories": [
                    {
                        "memoryId": "mem_timeout_fixture",
                        "revision": 1,
                        "text": "fixture",
                    }
                ],
            }

    monkeypatch.setattr(
        agent_tools.BridgeConfig,
        "from_env",
        staticmethod(lambda: config),
    )
    monkeypatch.setattr(agent_tools, "MemoryOnlyRecallClient", FakeMemoryClient)

    context = agent_tools.auto_recall_context(
        "what should Nancy remember?",
        "session-timeout",
        consumer="timeout-test",
    )

    assert seen == {
        "query": "what should Nancy remember?",
        "session": "session-timeout",
        "timeout": 6.25,
        "consumer": "timeout-test",
    }
    assert "<digital-life-retrieved-memory>" in context
    assert '"memoryId": "mem_timeout_fixture"' in context
