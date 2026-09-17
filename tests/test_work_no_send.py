from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest

from hermes_life_bridge.bridge import HermesLifeBridge
from hermes_life_bridge.cognition_service import CognitionService
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_delivery import HermesSendClient
from hermes_life_bridge.contact_service import ContactService
from hermes_life_bridge.hermes_api import HermesApiClient
from hermes_life_bridge.transport import UnixSocketTransport
from hermes_life_bridge.work_adapter import WorkAdapter
from hermes_life_bridge.work_store import WorkStore
import hermes_life_bridge.work_adapter as work_adapter_module
from hermes_life_bridge.work_contract import WorkContractError
from hermes_life_bridge.work_projection import (
    RuntimeDeliveryForbidden,
    WorkProjectionDisabled,
    WorkProjectionError,
)


def work_mapping(
    *,
    fact_id: str = "fact-001",
    idempotency_key: str = "idem-001",
    observed_at: str = "2026-09-13T12:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": "work_fact.v1",
        "fact_id": fact_id,
        "idempotency_key": idempotency_key,
        "work_item_id": "work-001",
        "session_id": "session-001",
        "executor_id": "executor-001",
        "outcome_class": "verified_complete",
        "terminal": True,
        "result_hash": "sha256:" + "a" * 64,
        "novelty_hash": "sha256:" + "b" * 64,
        "evidence_refs": ["evidence-001"],
        "counters": {"attempt_count": 1, "recovery_count": 0},
        "observed_at": observed_at,
    }


def activity_mapping(observed_at: str) -> dict[str, object]:
    return {
        "schema_version": "activity_fact.v1",
        "session_id": "session-001",
        "observer_id": "observer-001",
        "observed_at": observed_at,
    }


def config(tmp_path: Path, **changes: object) -> BridgeConfig:
    values: dict[str, object] = {
        "life_did": "did:example:life",
        "runtime_socket": str(tmp_path / "runtime.sock"),
        "trace_path": str(tmp_path / "trace.jsonl"),
        "contact_delivery_enabled": True,
        "work_progress_enabled": True,
        "work_progress_runtime_delivery": False,
        "work_ledger_db": str(tmp_path / "work.db"),
        "work_idle_seconds": 0.0,
    }
    values.update(changes)
    return BridgeConfig(**values)


def install_external_call_spies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def forbidden(name: str):
        def record(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"forbidden_external_call:{name}")

        return record

    monkeypatch.setattr(HermesLifeBridge, "_deliver", forbidden("runtime_bridge"))
    monkeypatch.setattr(
        UnixSocketTransport, "send_percept", forbidden("runtime_transport")
    )
    monkeypatch.setattr(CognitionService, "process", forbidden("cognition_service"))
    monkeypatch.setattr(HermesApiClient, "cognize", forbidden("cognition_provider"))
    monkeypatch.setattr(ContactService, "process", forbidden("contact_service"))
    monkeypatch.setattr(HermesSendClient, "send", forbidden("contact_delivery"))
    monkeypatch.setattr(subprocess, "run", forbidden("subprocess"))
    return calls


def test_config_defaults_aliases_strict_flags_and_bounded_idle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in (
        "HLB_WORK_PROGRESS_ENABLED",
        "HLB_WORK_PROGRESS_RUNTIME_DELIVERY",
        "HLB_WORK_LEDGER_DB",
        "HLB_WORK_PROJECTION_DB",
        "HLB_WORK_IDLE_SECONDS",
        "HLB_WORK_PROGRESS_IDLE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg = BridgeConfig.from_env()
    expected_db = str(
        tmp_path / "state" / "hermes-life-bridge" / "work_projection.sqlite3"
    )
    assert cfg.work_progress_enabled is False
    assert cfg.work_progress_runtime_delivery is False
    assert cfg.work_ledger_db == expected_db
    assert cfg.work_projection_db == expected_db
    assert cfg.work_idle_seconds == 900.0
    assert cfg.work_progress_idle_seconds == 900.0

    custom_db = tmp_path / "custom-work.db"
    monkeypatch.setenv("HLB_WORK_LEDGER_DB", str(custom_db))
    monkeypatch.setenv("HLB_WORK_IDLE_SECONDS", "12.5")
    monkeypatch.setenv("HLB_WORK_PROGRESS_ENABLED", "yes")
    assert BridgeConfig.from_env().work_ledger_db == str(custom_db)
    assert BridgeConfig.from_env().work_idle_seconds == 12.5
    assert BridgeConfig.from_env().work_progress_enabled is True

    monkeypatch.setenv("HLB_WORK_PROGRESS_ENABLED", "enabled")
    with pytest.raises(ValueError, match="must_be_strict_boolean"):
        BridgeConfig.from_env()

    monkeypatch.setenv("HLB_WORK_PROGRESS_ENABLED", "false")
    for invalid in ("-0.1", "604800.1", "nan", "inf", "not-a-number"):
        monkeypatch.setenv("HLB_WORK_IDLE_SECONDS", invalid)
        with pytest.raises(ValueError, match="HLB_WORK_IDLE_SECONDS"):
            BridgeConfig.from_env()


def test_all_work_paths_make_zero_runtime_contact_cognition_or_subprocess_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = install_external_call_spies(monkeypatch)

    with WorkAdapter(config(tmp_path)) as adapter:
        assert adapter.record_work_fact(work_mapping()).status == "accepted"
        assert adapter.record_work_fact(work_mapping()).status == "duplicate"
        suppressed = adapter.evaluate_due(now="2026-09-13T12:00:00Z")
        assert [item.state for item in suppressed] == ["suppressed"]

        adapter.note_work_activity(activity_mapping("2026-09-13T12:00:01Z"))
        adapter.record_work_fact(
            work_mapping(
                fact_id="fact-002",
                idempotency_key="idem-002",
                observed_at="2026-09-13T12:00:02Z",
            )
        )
        candidate = adapter.evaluate_due(now="2026-09-13T12:00:02Z")
        assert [item.state for item in candidate] == ["shadow_candidate"]

    assert calls == []


def test_disabled_hostile_and_delivery_true_fail_before_persistence_or_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = install_external_call_spies(monkeypatch)

    disabled_db = tmp_path / "disabled.db"
    disabled = config(
        tmp_path,
        work_progress_enabled=False,
        work_ledger_db=str(disabled_db),
    )
    with WorkAdapter(disabled) as adapter:
        with pytest.raises(WorkProjectionDisabled, match="work_progress_disabled"):
            adapter.record_work_fact(work_mapping())
    assert not disabled_db.exists()

    hostile_db = tmp_path / "hostile.db"
    hostile = work_mapping()
    hostile["prompt"] = "PRIVATE SENTINEL RAW PROMPT"
    with WorkAdapter(
        config(tmp_path, work_ledger_db=str(hostile_db))
    ) as adapter:
        with pytest.raises(WorkContractError, match="fields_must_be_exact"):
            adapter.record_work_fact(hostile)
    for path in (hostile_db, Path(f"{hostile_db}-wal"), Path(f"{hostile_db}-shm")):
        assert not path.exists()

    delivery_db = tmp_path / "delivery-forbidden.db"
    with pytest.raises(
        RuntimeDeliveryForbidden,
        match="work_progress_runtime_delivery_must_be_false",
    ):
        WorkAdapter(
            config(
                tmp_path,
                work_progress_runtime_delivery=True,
                work_ledger_db=str(delivery_db),
            )
        )
    assert not delivery_db.exists()
    assert calls == []


def test_ledger_path_must_not_alias_existing_hlb_database(tmp_path: Path) -> None:
    contact_db = tmp_path / "contact.sqlite3"
    cfg = config(tmp_path, work_ledger_db=str(contact_db), contact_db=str(contact_db))
    with pytest.raises(WorkProjectionError, match="work_ledger_path_must_be_dedicated"):
        WorkAdapter(cfg).record_work_fact(work_mapping())
    assert not contact_db.exists()


def test_ledger_path_must_reject_hardlink_to_existing_hlb_database(tmp_path: Path) -> None:
    contact_db = tmp_path / "contact.sqlite3"
    contact_db.write_bytes(b"sqlite-fixture")
    ledger_alias = tmp_path / "work-ledger.sqlite3"
    ledger_alias.hardlink_to(contact_db)
    cfg = config(
        tmp_path,
        work_ledger_db=str(ledger_alias),
        contact_db=str(contact_db),
    )
    with pytest.raises(WorkProjectionError, match="work_ledger_path_must_be_dedicated"):
        WorkAdapter(cfg).record_work_fact(work_mapping())
    assert contact_db.read_bytes() == b"sqlite-fixture"


def test_ledger_path_must_reject_symlink_to_existing_hlb_database(tmp_path: Path) -> None:
    contact_db = tmp_path / "contact.sqlite3"
    contact_db.write_bytes(b"sqlite-fixture")
    ledger_alias = tmp_path / "work-ledger.sqlite3"
    ledger_alias.symlink_to(contact_db)
    cfg = config(
        tmp_path,
        work_ledger_db=str(ledger_alias),
        contact_db=str(contact_db),
    )
    with pytest.raises(WorkProjectionError, match="work_ledger_path_must_be_dedicated"):
        WorkAdapter(cfg).record_work_fact(work_mapping())
    assert contact_db.read_bytes() == b"sqlite-fixture"
    assert not Path(f"{contact_db}-wal").exists()
    assert not Path(f"{contact_db}-shm").exists()


def test_ledger_parent_must_be_owner_private(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    ledger = shared / "work.sqlite3"
    with pytest.raises(WorkProjectionError, match="work_ledger_parent_must_be_owner_private"):
        WorkAdapter(config(tmp_path, work_ledger_db=str(ledger))).record_work_fact(
            work_mapping()
        )
    assert not ledger.exists()


def test_ledger_ancestor_must_not_be_writable_by_others(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o777)
    checked = shared / "checked"
    checked.mkdir(mode=0o700)
    ledger = checked / "work.sqlite3"
    with pytest.raises(WorkProjectionError, match="work_ledger_ancestor_must_be_private"):
        WorkAdapter(config(tmp_path, work_ledger_db=str(ledger))).record_work_fact(
            work_mapping()
        )
    assert not ledger.exists()


def test_ledger_ancestor_owner_must_be_current_owner_or_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path / "foreign-owner"
    ancestor.mkdir(mode=0o755)
    checked = ancestor / "checked"
    checked.mkdir(mode=0o700)
    ledger = checked / "work.sqlite3"
    real_stat = os.stat

    def spoofed_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if Path(path) == ancestor:
            values = list(result)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(work_adapter_module.os, "stat", spoofed_stat)
    with pytest.raises(WorkProjectionError, match="work_ledger_ancestor_owner_untrusted"):
        WorkAdapter(config(tmp_path, work_ledger_db=str(ledger))).record_work_fact(
            work_mapping()
        )
    assert not ledger.exists()


def test_sticky_directory_with_private_child_is_allowed(tmp_path: Path) -> None:
    sticky = tmp_path / "sticky"
    sticky.mkdir(mode=0o755)
    sticky.chmod(0o1777)
    checked = sticky / "checked"
    checked.mkdir(mode=0o700)
    ledger = checked / "work.sqlite3"
    with WorkAdapter(config(tmp_path, work_ledger_db=str(ledger))) as adapter:
        accepted = adapter.record_work_fact(work_mapping())
    assert accepted.status == "accepted"
    assert ledger.exists()


def test_rejection_happens_before_workstore_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact_db = tmp_path / "contact.sqlite3"
    calls: list[str] = []

    class SpyWorkStore(WorkStore):
        def __init__(self, path: str) -> None:
            calls.append(path)
            super().__init__(path)

    monkeypatch.setattr(work_adapter_module, "WorkStore", SpyWorkStore)
    cfg = config(tmp_path, work_ledger_db=str(contact_db), contact_db=str(contact_db))
    with pytest.raises(WorkProjectionError, match="work_ledger_path_must_be_dedicated"):
        WorkAdapter(cfg).record_work_fact(work_mapping())
    assert calls == []
    assert not contact_db.exists()
