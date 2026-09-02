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
