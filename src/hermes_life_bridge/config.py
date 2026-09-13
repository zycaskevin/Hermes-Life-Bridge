from __future__ import annotations
from dataclasses import dataclass
import math
from pathlib import Path
import os


def _strict_feature_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_must_be_strict_boolean")


def _bounded_seconds(
    value: str,
    *,
    name: str,
    minimum: float = 0.0,
    maximum: float = 604800.0,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}_must_be_number") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name}_out_of_range")
    return parsed


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except Exception:
        return {}
    return values


@dataclass(frozen=True)
class BridgeConfig:
    life_did: str
    runtime_socket: str
    trace_path: str
    connect_timeout_seconds: float = 0.25
    ack_timeout_seconds: float = 1.5
    cognition_socket: str = ""
    cognition_db: str = ""
    cognition_timeout_seconds: float = 120.0
    hermes_api_base_url: str = "http://127.0.0.1:8642"
    hermes_api_key: str = ""
    hermes_model: str = "hermes-agent"
    contact_socket: str = ""
    contact_db: str = ""
    contact_timeout_seconds: float = 30.0
    contact_delivery_enabled: bool = False
    contact_target: str = ""
    hermes_cli_path: str = "hermes"
    route_path: str = ""
    operation_db: str = ""
    compatibility_path: str = ""
    compatibility_evidence_path: str = ""
    route_max_age_seconds: float = 604800.0
    trace_max_bytes: int = 10485760
    trace_backup_count: int = 3
    operation_retention_seconds: float = 2592000.0
    work_progress_enabled: bool = False
    work_progress_runtime_delivery: bool = False
    work_projection_db: str = ""
    work_progress_idle_seconds: float = 900.0
    # WP-2 names. The two fields above remain compatibility aliases for the
    # syntax-valid partial implementation and any local callers of it.
    work_ledger_db: str = ""
    work_idle_seconds: float = 900.0

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        runtime_dir = os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        config_home = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        state_home = Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        config_file = Path(os.getenv("HLB_CONFIG_FILE", str(config_home / "hermes-life-bridge.env")))
        file_values = _read_env_file(config_file)
        hermes_env_path = os.getenv("HLB_HERMES_ENV") or file_values.get("HLB_HERMES_ENV", "")
        hermes_values = _read_env_file(Path(hermes_env_path)) if hermes_env_path else {}

        def get(*names: str, default: str) -> str:
            for name in names:
                value = os.getenv(name)
                if value:
                    return value
                value = file_values.get(name)
                if value:
                    return value
                value = hermes_values.get(name)
                if value:
                    return value
            return default

        work_ledger_db = get(
            "HLB_WORK_LEDGER_DB",
            "HLB_WORK_PROJECTION_DB",
            default=str(
                state_home / "hermes-life-bridge" / "work_projection.sqlite3"
            ),
        )
        work_idle_seconds = _bounded_seconds(
            get(
                "HLB_WORK_IDLE_SECONDS",
                "HLB_WORK_PROGRESS_IDLE_SECONDS",
                default="900",
            ),
            name="HLB_WORK_IDLE_SECONDS",
        )

        return cls(
            life_did=get("LIFE_RUNTIME_LIFE_DID", "LIVE_RUNTIME_LIFE_DID", default="did:example:life"),
            runtime_socket=get("LIFE_RUNTIME_SOCKET", "LIVE_RUNTIME_SOCKET", default=str(Path(runtime_dir) / "nancy-live-runtime.sock")),
            trace_path=get("HLB_TRACE_PATH", default=str(state_home / "hermes-life-bridge" / "trace.jsonl")),
            connect_timeout_seconds=float(get("HLB_CONNECT_TIMEOUT_SECONDS", default="0.25")),
            ack_timeout_seconds=float(get("HLB_ACK_TIMEOUT_SECONDS", default="1.5")),
            cognition_socket=get("HLB_COGNITION_SOCKET", default=str(Path(runtime_dir) / "hermes-life-cognition.sock")),
            cognition_db=get("HLB_COGNITION_DB", default=str(state_home / "hermes-life-bridge" / "cognition.sqlite3")),
            cognition_timeout_seconds=float(get("HLB_COGNITION_TIMEOUT_SECONDS", default="120")),
            hermes_api_base_url=get("HLB_HERMES_API_BASE_URL", default="http://127.0.0.1:8642"),
            hermes_api_key=get("HLB_HERMES_API_KEY", "API_SERVER_KEY", default=""),
            hermes_model=get("HLB_HERMES_MODEL", "API_SERVER_MODEL_NAME", default="hermes-agent"),
            contact_socket=get("HLB_CONTACT_SOCKET", default=str(Path(runtime_dir) / "hermes-life-contact.sock")),
            contact_db=get("HLB_CONTACT_DB", default=str(state_home / "hermes-life-bridge" / "contact.sqlite3")),
            contact_timeout_seconds=float(get("HLB_CONTACT_TIMEOUT_SECONDS", default="30")),
            contact_delivery_enabled=get("HLB_CONTACT_DELIVERY_ENABLED", default="false").lower() in {"1","true","yes","on"},
            contact_target=get("HLB_CONTACT_TARGET", default=""),
            hermes_cli_path=get("HLB_HERMES_CLI", default="hermes"),
            route_path=get("HLB_ROUTE_PATH", default=str(state_home / "hermes-life-bridge" / "last_route.json")),
            operation_db=get("HLB_OPERATION_DB", default=str(state_home / "hermes-life-bridge" / "operations.sqlite3")),
            compatibility_path=get("HLB_COMPATIBILITY_PATH", default=str(state_home / "hermes-life-bridge" / "compatibility.json")),
            compatibility_evidence_path=get("HLB_COMPATIBILITY_EVIDENCE_PATH", default=str(state_home / "hermes-life-bridge" / "compatibility-evidence.json")),
            route_max_age_seconds=float(get("HLB_ROUTE_MAX_AGE_SECONDS", default="604800")),
            trace_max_bytes=int(get("HLB_TRACE_MAX_BYTES", default="10485760")),
            trace_backup_count=int(get("HLB_TRACE_BACKUP_COUNT", default="3")),
            operation_retention_seconds=float(get("HLB_OPERATION_RETENTION_SECONDS", default="2592000")),
            work_progress_enabled=_strict_feature_bool(
                get("HLB_WORK_PROGRESS_ENABLED", default="false"),
                name="HLB_WORK_PROGRESS_ENABLED",
            ),
            work_progress_runtime_delivery=_strict_feature_bool(
                get("HLB_WORK_PROGRESS_RUNTIME_DELIVERY", default="false"),
                name="HLB_WORK_PROGRESS_RUNTIME_DELIVERY",
            ),
            work_projection_db=work_ledger_db,
            work_progress_idle_seconds=work_idle_seconds,
            work_ledger_db=work_ledger_db,
            work_idle_seconds=work_idle_seconds,
        )
