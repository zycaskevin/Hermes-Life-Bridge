"""Contact remains available without DLS/native-research binding or credentials."""
from dataclasses import replace

import pytest

from hermes_life_bridge.contact_service import ContactService
from hermes_life_bridge.runtime_contact import HermesContactBridge
from hermes_life_bridge.runtime_adapter import RuntimeBridgeError
from test_contact_service import cfg, intent, decision, Sender


def test_proactive_contact_does_not_depend_on_native_host_or_dls(tmp_path, monkeypatch):
    from hermes_life_bridge import runtime_adapter
    monkeypatch.setattr(runtime_adapter, '_BINDINGS', {})
    monkeypatch.setattr(runtime_adapter, '_contracts', lambda: pytest.fail('Contact must not load native contracts'))
    config = cfg(tmp_path, True)
    sender = Sender()
    service = ContactService(config, sender=sender)
    bridge = HermesContactBridge(config)
    first = bridge.deliver(selected_runtime='hermes', intent=intent(), decision=decision(), service=service)
    duplicate = bridge.deliver(selected_runtime='hermes', intent=intent(), decision=decision(), service=service)
    assert first.status == 'delivered' and duplicate.duplicate


def test_contact_does_not_enable_disabled_delivery(tmp_path):
    config = cfg(tmp_path, False)
    service = ContactService(config, sender=Sender())
    result = HermesContactBridge(config).deliver(selected_runtime='hermes', intent=intent(),
        decision=decision(), service=service)
    assert result.status == 'dry_run'


def test_contact_rejects_wrong_runtime_scope_and_noncontact_decision(tmp_path):
    config = cfg(tmp_path, True)
    service = ContactService(config, sender=Sender())
    bridge = HermesContactBridge(config)
    with pytest.raises(RuntimeBridgeError, match='hermes_runtime_mismatch'):
        bridge.deliver(selected_runtime='openclaw', intent=intent(), decision=decision(), service=service)
    with pytest.raises(RuntimeBridgeError, match='contact_life_scope_mismatch'):
        bridge.deliver(selected_runtime='hermes', intent=replace(intent(), life_did='did:other'),
            decision=decision(), service=service)
    with pytest.raises(ValueError, match='decision_not_contact'):
        bridge.deliver(selected_runtime='hermes', intent=intent(),
            decision=replace(decision(), outcome='stay_quiet'), service=service)
