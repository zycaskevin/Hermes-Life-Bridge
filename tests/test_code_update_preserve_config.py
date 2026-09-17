from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_code_preserve_config.sh"


def test_code_only_updater_is_shell_valid_and_never_rewrites_hlb_env():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "sudo " not in text
    assert "HLB_CONTACT_DELIVERY_ENABLED=false" not in text
    assert "cat > \"$HLB_ENV" not in text
    assert "ENV_HASH_BEFORE" in text
    assert "ENV_HASH_AFTER" in text
    assert "selected_config" in text
    assert '"$CONFIG_BEFORE" = "$CONFIG_AFTER"' in text


def test_code_only_updater_has_bounded_rollback_and_preserves_live_venv():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "rollback" in text
    assert "HLB_CODE_ROLLBACK=PASS" in text
    assert "--exclude='.venv/'" in text
    assert "rsync -a --delete" in text
    assert "hermes-gateway.service" in text
    assert "doctor" in text
    assert "DOCTOR_HEALTHY" in text
    assert "seq 1 60" in text
    assert "refresh_metadata" in text
    assert "uv pip install --offline" in text
    assert "METADATA_VERSION" in text
    assert "metadata_version_mismatch" in text
    assert "HLB_CODE_UPDATE=PASS" in text
