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
    cfg_file = tmp_path / "isolated.env"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("HLB_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("HLB_OPERATION_DB", raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.operation_db == str(tmp_path / "state" / "hermes-life-bridge" / "operations.sqlite3")

    monkeypatch.setenv("HLB_OPERATION_DB", str(tmp_path / "custom-operations.db"))
    cfg = BridgeConfig.from_env()
    assert cfg.operation_db == str(tmp_path / "custom-operations.db")


def test_compatibility_paths_follow_state_home(monkeypatch, tmp_path):
    cfg_file = tmp_path / "isolated.env"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("HLB_CONFIG_FILE", str(cfg_file))
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


def test_maintenance_config_defaults_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in (
        "HLB_TRACE_MAX_BYTES",
        "HLB_TRACE_BACKUP_COUNT",
        "HLB_OPERATION_RETENTION_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.trace_max_bytes == 10485760
    assert cfg.trace_backup_count == 3
    assert cfg.operation_retention_seconds == 2592000.0

    monkeypatch.setenv("HLB_TRACE_MAX_BYTES", "2097152")
    monkeypatch.setenv("HLB_TRACE_BACKUP_COUNT", "5")
    monkeypatch.setenv("HLB_OPERATION_RETENTION_SECONDS", "86400")
    cfg = BridgeConfig.from_env()
    assert cfg.trace_max_bytes == 2097152
    assert cfg.trace_backup_count == 5
    assert cfg.operation_retention_seconds == 86400.0


def test_work_producer_config_is_disabled_by_default_and_supports_owner_private_file_path(monkeypatch, tmp_path):
    cfg_file = tmp_path / "empty.env"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("HLB_CONFIG_FILE", str(cfg_file))
    for name in (
        "HLB_WORK_PRODUCER_ENABLED",
        "HLB_WORK_PRODUCER_ENDPOINT",
        "HLB_WORK_PRODUCER_CREDENTIALS_FILE",
        "HLB_WORK_PRODUCER_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.work_producer_enabled is False
    assert cfg.work_producer_endpoint == "http://127.0.0.1:8791"
    assert cfg.work_producer_credentials_file.endswith(
        "hermes-life-bridge-work-producer.json"
    )
    assert cfg.work_producer_timeout_seconds == 1.0

    credentials = tmp_path / "producer.json"
    monkeypatch.setenv("HLB_WORK_PRODUCER_ENABLED", "true")
    monkeypatch.setenv("HLB_WORK_PRODUCER_ENDPOINT", "http://127.0.0.1:8792")
    monkeypatch.setenv("HLB_WORK_PRODUCER_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("HLB_WORK_PRODUCER_TIMEOUT_SECONDS", "0.75")
    cfg = BridgeConfig.from_env()
    assert cfg.work_producer_enabled is True
    assert cfg.work_producer_endpoint == "http://127.0.0.1:8792"
    assert cfg.work_producer_credentials_file == str(credentials)
    assert cfg.work_producer_timeout_seconds == 0.75


def test_ambient_interest_config_is_disabled_by_default_and_reuses_work_runtime_credential(monkeypatch, tmp_path):
    cfg_file = tmp_path / "empty.env"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("HLB_CONFIG_FILE", str(cfg_file))
    for name in (
        "HLB_AMBIENT_INTEREST_ENABLED",
        "HLB_AMBIENT_INTEREST_ENDPOINT",
        "HLB_AMBIENT_INTEREST_RUNTIME_ID",
        "HLB_AMBIENT_INTEREST_CREDENTIALS_FILE",
        "HLB_AMBIENT_INTEREST_TIMEOUT_SECONDS",
        "HLB_WORK_PRODUCER_CREDENTIALS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg = BridgeConfig.from_env()
    assert cfg.ambient_interest_enabled is False
    assert cfg.ambient_interest_endpoint == "http://127.0.0.1:8794"
    assert cfg.ambient_interest_runtime_id == "nancy-ambient-canary"
    assert cfg.ambient_interest_credentials_file.endswith(
        "hermes-life-bridge-work-producer.json"
    )
    assert cfg.ambient_interest_timeout_seconds == 0.75

    shared_credentials = tmp_path / "shared-producer.json"
    monkeypatch.setenv("HLB_WORK_PRODUCER_CREDENTIALS_FILE", str(shared_credentials))
    monkeypatch.setenv("HLB_AMBIENT_INTEREST_ENABLED", "true")
    monkeypatch.setenv("HLB_AMBIENT_INTEREST_ENDPOINT", "http://127.0.0.1:8894")
    monkeypatch.setenv("HLB_AMBIENT_INTEREST_RUNTIME_ID", "nancy-ambient-test")
    monkeypatch.setenv("HLB_AMBIENT_INTEREST_TIMEOUT_SECONDS", "1.25")
    cfg = BridgeConfig.from_env()
    assert cfg.ambient_interest_enabled is True
    assert cfg.ambient_interest_endpoint == "http://127.0.0.1:8894"
    assert cfg.ambient_interest_runtime_id == "nancy-ambient-test"
    assert cfg.ambient_interest_credentials_file == str(shared_credentials)
    assert cfg.ambient_interest_timeout_seconds == 1.25
