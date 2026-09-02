from pathlib import Path

def test_release_installer_requires_service_restore_trap():
    # The release build injects the actual installer outside the source zip;
    # this test protects the documented contract artifact in repo.
    doc=Path("docs/HLB-003.5-REPRESENTATION-BOUNDARY.md").read_text()
    assert "restore service state on EXIT" in doc
    assert "no provider send" in doc.lower()


def test_installer_publishes_hlb004_operation_store_path():
    installer = Path("scripts/install_on_hermes.sh").read_text()
    assert "HLB_OPERATION_DB=$STATE_HOME/hermes-life-bridge/operations.sqlite3" in installer


def test_installer_publishes_compatibility_paths():
    installer = Path("scripts/install_on_hermes.sh").read_text()
    assert "HLB_COMPATIBILITY_PATH=$STATE_HOME/hermes-life-bridge/compatibility.json" in installer
    assert "HLB_COMPATIBILITY_EVIDENCE_PATH=$STATE_HOME/hermes-life-bridge/compatibility-evidence.json" in installer


def test_installer_sets_route_freshness_policy():
    installer = Path("scripts/install_on_hermes.sh").read_text()
    assert "HLB_ROUTE_MAX_AGE_SECONDS=604800" in installer


def test_installer_configures_bounded_trace_and_daily_maintenance():
    installer = Path("scripts/install_on_hermes.sh").read_text()
    assert "HLB_TRACE_MAX_BYTES=10485760" in installer
    assert "HLB_TRACE_BACKUP_COUNT=3" in installer
    assert "HLB_OPERATION_RETENTION_SECONDS=2592000" in installer
    assert "enable --now hermes-life-maintenance.timer" in installer


def test_release_installer_has_source_level_rollback_and_safe_delivery_default():
    installer = Path("scripts/install_on_hermes.sh").read_text()
    assert "trap 'rollback_install $?' EXIT" in installer
    assert "restoring previous Hermes Life Bridge state" in installer
    assert "PLUGIN_BACKUP" in installer
    assert "hermes-life-bridge.env" in installer
    assert "HLB_CONTACT_DELIVERY_ENABLED=false" in installer
    assert "hermes gateway restart" in installer
    assert "hermes_life_bridge.soak --iterations 1000" in installer


def test_nancy_acceptance_script_requires_all_hlb_services():
    script = Path("scripts/accept_on_nancy.sh").read_text()
    for unit in (
        "hermes-life-cognition.service",
        "hermes-life-contact.service",
        "hermes-life-percept-recovery.service",
        "hermes-life-maintenance.timer",
    ):
        assert unit in script
    assert "NANCY_ACCEPTANCE=PASS" in script
