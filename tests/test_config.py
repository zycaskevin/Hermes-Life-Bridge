from hermes_life_bridge.config import BridgeConfig

def test_config_file_fallback(monkeypatch, tmp_path):
    cfg_file = tmp_path / "bridge.env"
    cfg_file.write_text(
        "LIVE_RUNTIME_LIFE_DID=did:example:from-file\n"
        "LIVE_RUNTIME_SOCKET=/tmp/from-file.sock\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HLB_CONFIG_FILE", str(cfg_file))
    monkeypatch.delenv("LIVE_RUNTIME_LIFE_DID", raising=False)
    monkeypatch.delenv("LIFE_RUNTIME_LIFE_DID", raising=False)
    monkeypatch.delenv("LIVE_RUNTIME_SOCKET", raising=False)
    monkeypatch.delenv("LIFE_RUNTIME_SOCKET", raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.life_did == "did:example:from-file"
    assert cfg.runtime_socket == "/tmp/from-file.sock"


def test_hermes_env_fallback_for_api_key(monkeypatch, tmp_path):
    hermes_env=tmp_path/"hermes.env"; hermes_env.write_text("API_SERVER_KEY=topsecret\nAPI_SERVER_MODEL_NAME=test-model\n")
    cfg_file=tmp_path/"bridge.env"; cfg_file.write_text(f"HLB_HERMES_ENV={hermes_env}\n")
    monkeypatch.setenv("HLB_CONFIG_FILE",str(cfg_file)); monkeypatch.delenv("API_SERVER_KEY",raising=False)
    cfg=BridgeConfig.from_env()
    assert cfg.hermes_api_key == "topsecret"
    assert cfg.hermes_model == "test-model"


def test_operation_db_config_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("HLB_OPERATION_DB", raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.operation_db == str(tmp_path / "state" / "hermes-life-bridge" / "operations.sqlite3")

    monkeypatch.setenv("HLB_OPERATION_DB", str(tmp_path / "custom-operations.db"))
    cfg = BridgeConfig.from_env()
    assert cfg.operation_db == str(tmp_path / "custom-operations.db")


def test_compatibility_paths_follow_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("HLB_COMPATIBILITY_PATH", raising=False)
    monkeypatch.delenv("HLB_COMPATIBILITY_EVIDENCE_PATH", raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.compatibility_path == str(
        tmp_path / "state" / "hermes-life-bridge" / "compatibility.json"
    )
    assert cfg.compatibility_evidence_path == str(
        tmp_path / "state" / "hermes-life-bridge" / "compatibility-evidence.json"
    )


def test_route_max_age_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("HLB_ROUTE_MAX_AGE_SECONDS", raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.route_max_age_seconds == 604800.0
    monkeypatch.setenv("HLB_ROUTE_MAX_AGE_SECONDS", "3600")
    assert BridgeConfig.from_env().route_max_age_seconds == 3600.0
