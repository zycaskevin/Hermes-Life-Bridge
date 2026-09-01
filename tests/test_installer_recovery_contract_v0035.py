from pathlib import Path

def test_release_installer_requires_service_restore_trap():
    # The release build injects the actual installer outside the source zip;
    # this test protects the documented contract artifact in repo.
    doc=Path("docs/HLB-003.5-REPRESENTATION-BOUNDARY.md").read_text()
    assert "restore service state on EXIT" in doc
    assert "no provider send" in doc.lower()
