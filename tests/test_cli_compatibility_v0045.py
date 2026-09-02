import json

from hermes_life_bridge.cli import main


def test_compatibility_cli_prints_report(monkeypatch, capsys):
    class Report:
        supported = True
        def to_dict(self):
            return {"supported": True, "hermes_version": "0.20.0"}

    class Discovery:
        def __init__(self, config):
            pass
        def discover(self):
            return Report()

    monkeypatch.setattr("hermes_life_bridge.cli.CompatibilityDiscovery", Discovery)
    assert main(["compatibility"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["supported"] is True
    assert data["hermes_version"] == "0.20.0"


def test_emit_test_compatibility_with_legacy_cli_args(monkeypatch, capsys):
    class Receipt:
        def __init__(self):
            self.ok = True

    class Bridge:
        def __init__(self, config):
            pass
        def cli_turn(self, **kwargs):
            assert kwargs["session_id"] == "s"
            assert kwargs["turn_id"] == "t"
            assert kwargs["user_message"] == "hello"
            return Receipt()

    monkeypatch.setattr("hermes_life_bridge.cli.HermesLifeBridge", Bridge)
    assert main([
        "emit-test",
        "--surface", "cli",
        "--session-id", "s",
        "--turn-id", "t",
        "--message", "hello",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
