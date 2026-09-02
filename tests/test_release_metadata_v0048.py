from pathlib import Path

import hermes_life_bridge


def test_release_versions_are_consistent():
    assert hermes_life_bridge.__version__ == "0.4.0"
    pyproject = Path("pyproject.toml").read_text()
    plugin = Path("plugin.yaml").read_text()
    assert 'version = "0.4.0"' in pyproject
    assert 'version: "0.4.0"' in plugin
    assert ".dev" not in hermes_life_bridge.__version__


def test_readme_declares_hlb004_complete_and_safe_install_default():
    readme = Path("README.md").read_text()
    assert "HLB v0.4.0" in readme
    assert "External proactive Contact defaults **OFF**" in readme
    assert "DELIVERY_UNKNOWN" in readme
    assert "NANCY_ACCEPTANCE=PASS" in readme


def test_roadmap_marks_hlb004_complete():
    roadmap = Path("ROADMAP.md").read_text()
    assert "HLB-004 — Runtime Reliability & Compatibility ✅ COMPLETE" in roadmap
    assert "HLB-004.8 — Soak, Maintenance & Release Closure ✅ DEVELOPMENT COMPLETE" in roadmap
