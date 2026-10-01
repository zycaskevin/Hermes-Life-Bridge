"""Mandatory admission at the actual Runs transport; all authorities synthetic.

These tests prove that HLB does not submit without existing-authority and
host-budget verification. They do NOT prove real provider cost enforcement.
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytest.importorskip('agent_factory.runtimes.hermes_native')
pytest.importorskip('digital_life_native_capability')

from agent_factory.runtimes.hermes import HermesTransportError
from agent_factory.contracts import (AuthorizationOutcome, ContextProjection, ExecutionAuthorization,
    RiskLevel, SideEffectPolicy)
from agent_factory.contracts.runtime_invocation import CapabilityIntent, GovernedInvocation
from hermes_life_bridge.hermes_direct_api import DirectHermesApi
from hermes_life_bridge.host_budget_admission import HostBudgetAdmission
from hermes_life_bridge.runtime_adapter import HermesRuntimeAdapter, RuntimeBridgeError
from hermes_life_bridge.config import BridgeConfig
from host_budget_fixtures import FixtureHostBudget
from test_hermes_runs_binding import server, setup_port, execute, KEY, MODEL, FixtureAuthority
from test_agent_tools import tools
from native_bridge_fixtures import intent_and_selection


def submitted(state):
    return [c for c in state['calls'] if c[0] == 'POST' and c[1] == '/v1/runs']


@pytest.mark.parametrize('mode', ['deny', 'boolean', 'wrong_binding', 'wrong_origin', 'expired',
    'future', 'overbroad_time', 'prompt_only', 'no_cost', 'no_unknown_hold', 'unbounded_reference'])
def test_invalid_host_budget_proof_prevents_every_runs_post(server, mode):
    endpoint, state = server
    port, authority = setup_port(endpoint)
    budget = port._submission_gate._budget_port
    budget.mode = mode
    _, receipt = asyncio.run(execute(port))
    assert authority.calls and len(budget.calls) == 1
    assert not submitted(state)
    assert receipt['status'] != 'completed'
    assert receipt['executionRuntime'] is None and not receipt['nativeEvents']
    assert 'PRIVATE_BUDGET_ERROR' not in str(receipt)
    assert port._submission_gate.diagnostics()['verifiedSubmissionCount'] == 0


def test_existing_authority_denial_happens_before_budget_callback(server):
    endpoint, state = server
    port, authority = setup_port(endpoint)
    authority.deny = True
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'failed'
    assert authority.calls and not port._submission_gate._budget_port.calls
    assert not submitted(state)



def test_reserved_dls_identity_binds_distinct_af_invocation_and_rejects_selection_tamper(server):
    endpoint, _state = server
    port, _ = setup_port(endpoint)

    class Admission:
        async def admit(self, intent, selection, descriptor, native):
            return None

    async def build_intent():
        cfg = BridgeConfig(life_did=port.life_did, runtime_socket='unused', trace_path='unused')
        descriptor = await port.discover()
        adapter = HermesRuntimeAdapter(cfg, tenant_id=port.tenant_id, descriptor=descriptor,
            native=port, admission=Admission())
        intent, _selection = intent_and_selection(adapter)
        return intent

    intent = asyncio.run(build_intent())
    dls_invocation_id = 'native:' + 'a' * 32
    reserved = port.reserved_binding_metadata(dls_invocation_id, intent)
    budget = intent['budget']
    issued = datetime.now(timezone.utc) - timedelta(seconds=1)
    expiry = datetime.now(timezone.utc) + timedelta(milliseconds=850)
    af_intent = CapabilityIntent(
        intent['intentId'], port.life_did, 'fixture-life', 'public_research',
        intent['request'], ('public_web',), RiskLevel.L0, budget['maxTokens'],
        Decimal(budget['maxCostUsd']), expiry,
        json.dumps({'dlsBinding': reserved, 'maxNativeCalls': budget['maxNativeCalls'],
                    'maxRuntimeMs': budget['maxRuntimeMs']}))
    projection = ContextProjection(
        'reserved-budget-projection', port.life_did, 'fixture-self-gateway', 'test-only',
        issued, expiry, project_state={'capability_intent': af_intent.to_dict(),
                                       'dlsBinding': reserved},
        source_hashes=tuple(intent['contextBinding'].values()))
    auth = ExecutionAuthorization(
        'reserved-budget-auth', af_intent.request_id, port.life_did,
        projection.projection_hash(), AuthorizationOutcome.ALLOW,
        frozenset({'public_research'}), frozenset({'public_web'}), RiskLevel.L0,
        SideEffectPolicy.NONE, budget['maxTokens'], Decimal(budget['maxCostUsd']),
        'fixture-self-gateway', issued, expiry, 'SYNTHETIC_SIGNATURE_NOT_PRODUCTION')
    # Deliberately distinct from DLS invocation_id. The signed dlsBinding is the link.
    governed = GovernedInvocation(
        'af-durable-invocation-fixture', af_intent, 'hermes', port.implementation_id,
        1, budget['maxTokens'], Decimal(budget['maxCostUsd']), projection, auth)
    selection = {
        'schema': port.contract.SELECTION_SCHEMA,
        'selectionRef': governed.digest(),
        'authorizationRef': intent['authorizationRef'],
        'intentDigest': port.contract.digest(intent),
        'tenantId': port.tenant_id,
        'lifeDid': port.life_did,
        'selectedRuntime': 'hermes',
        'implementationId': port.implementation_id,
        'capability': intent['capability'],
        'expiresAt': expiry.isoformat(),
        'previousInvocationId': None,
    }
    request = port.contract.NativeInvocation(dls_invocation_id, copy.deepcopy(intent), selection)
    gate = port._submission_gate
    gate.bind_verified_invocation(
        request=request, governed=governed, model=MODEL, session_id='cognitive-reserved-fixture')
    context = gate.bind(governed)
    assert context['schema'] == 'af.native-effect-budget-context.v1'
    assert gate.diagnostics()['boundCount'] == 1
    assert not gate._budget_port.calls

    endpoint2, _ = server
    # This fixture invocation is local-only; use a fresh gate and identical authority shape.
    port2, _ = setup_port(endpoint2)
    bad_selection = dict(selection, selectionRef=port.contract.digest('tampered-selection'))
    bad_request = port2.contract.NativeInvocation(
        dls_invocation_id, copy.deepcopy(intent), bad_selection)
    with pytest.raises(RuntimeBridgeError, match='host_budget_af_selection_mismatch'):
        port2._submission_gate.bind_verified_invocation(
            request=bad_request, governed=governed, model=MODEL,
            session_id='cognitive-reserved-fixture-tampered')
    assert not port2._submission_gate._budget_port.calls


def test_verified_budget_metadata_is_exact_but_not_an_actual_billing_receipt(server):
    endpoint, state = server
    port, authority = setup_port(endpoint)
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'completed' and len(submitted(state)) == 1
    binding = port._submission_gate._budget_port.calls[0]
    assert binding['tenantId'] == 'tenant-test' and binding['lifeDid'] == port.life_did
    assert binding['limits'] == {'maxTokens': 1000, 'maxCostUsd': '0.05',
                                 'maxNativeCalls': 2, 'maxRuntimeMs': 1000}
    assert all(binding[k].startswith('sha256:') for k in ('afInvocationDigest',
        'dlsIntentDigest','dlsSelectionDigest','authorizationDigest','contextProjectionDigest',
        'modelDigest','sessionRef','endpointRef','submissionDigest'))
    assert 'Public fixture topic' not in str(binding) and KEY not in str(binding)
    assert receipt['origin'] == 'SYNTHETIC_TEST' and receipt['usage']['actualCost'] is None
    assert port._submission_gate.diagnostics() == {'boundCount': 1, 'verifiedSubmissionCount': 1,
        'origin': 'SYNTHETIC_TEST', 'localBudgetLedger': False, 'releaseAttempted': False}


@pytest.mark.parametrize('phase', ['resolve_governed', 'validate_binding', 'prepare_existing_runtime'])
def test_presubmission_failure_reclaims_only_local_capacity(server, monkeypatch, phase):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    port._capacity = 1
    if phase == 'resolve_governed':
        async def fail(request): raise ValueError('test-only resolver failure')
        monkeypatch.setattr(port, '_resolve', fail)
    elif phase == 'validate_binding':
        monkeypatch.setattr(port, '_validate_governed', lambda *a: (_ for _ in ()).throw(ValueError('test-only binding failure')))
    else:
        monkeypatch.setattr(port._runtime, 'prepare_cognitive_task', lambda *a, **k: (_ for _ in ()).throw(ValueError('test-only prepare failure')))
    async def run():
        for _ in range(2):
            _, receipt = await execute(port)
            assert receipt['status'] == 'failed'
            assert port.diagnostics()['attemptCount'] == 0
            assert receipt['executionRuntime'] is None
    asyncio.run(run())
    assert not submitted(state) and not port._submission_gate._budget_port.calls
    assert port._submission_gate.diagnostics()['releaseAttempted'] is False



def test_bound_context_failure_before_runs_post_discards_only_unconsumed_local_binding(
        server, monkeypatch):
    endpoint, state = server
    port, _ = setup_port(endpoint)

    async def fail_before_post(*_args, **_kwargs):
        raise ValueError('synthetic pre-post runtime failure')

    monkeypatch.setattr(port._runtime, 'start_cognitive_task', fail_before_post)
    _, receipt = asyncio.run(execute(port))

    assert receipt['status'] == 'failed'
    assert not submitted(state)
    assert port.diagnostics()['attemptCount'] == 0
    assert port._submission_gate.diagnostics()['boundCount'] == 0
    assert port._submission_gate.diagnostics()['verifiedSubmissionCount'] == 0
    assert port._submission_gate.diagnostics()['releaseAttempted'] is False


def test_invalid_context_after_host_bind_is_retired_before_local_cleanup(
        server, monkeypatch):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    budget = port._submission_gate._budget_port

    original = budget.bind_execution_context
    def invalid_context(binding, governed):
        context = original(binding, governed)
        context['schema'] = 'invalid.context'
        return context

    monkeypatch.setattr(budget, 'bind_execution_context', invalid_context)
    _, receipt = asyncio.run(execute(port))

    assert receipt['status'] == 'failed'
    assert not submitted(state)
    assert port.diagnostics()['attemptCount'] == 0
    assert port._submission_gate.diagnostics()['boundCount'] == 0
    assert len(budget.retired) == 1


def test_failed_host_retirement_blocks_rebind_and_requires_reconciliation(
        server, monkeypatch):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    budget = port._submission_gate._budget_port

    original = budget.bind_execution_context
    def invalid_context(binding, governed):
        context = original(binding, governed)
        context['schema'] = 'invalid.context'
        return context

    def retirement_failure(_binding):
        raise RuntimeError('synthetic retirement failure')

    monkeypatch.setattr(budget, 'bind_execution_context', invalid_context)
    monkeypatch.setattr(budget, 'retire_execution_context', retirement_failure)
    _, receipt = asyncio.run(execute(port))

    assert receipt['status'] == 'failed'
    assert not submitted(state)
    assert port.diagnostics()['attemptCount'] == 1
    assert port._submission_gate.diagnostics()['boundCount'] == 1

    gate_entry = next(iter(port._submission_gate._bound.values()))
    with pytest.raises(RuntimeBridgeError, match='replayed'):
        port._submission_gate.bind(gate_entry.governed)

def test_verification_failure_retains_attempt_for_reconciliation(server):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    port._submission_gate._budget_port.mode = 'deny'
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'failed'
    assert port.diagnostics()['attemptCount'] == 1
    assert port._submission_gate.diagnostics()['boundCount'] == 1
    assert port._submission_gate.diagnostics()['releaseAttempted'] is False
    assert not submitted(state)


def test_missing_native_guard_cannot_reach_http_submission(server):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    port._submission_gate = None
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'failed' and not submitted(state)



def test_direct_transport_rejects_explicit_zero_port():
    with pytest.raises(RuntimeBridgeError, match='hermes_endpoint_requires_explicit_loopback'):
        DirectHermesApi(base_url='http://127.0.0.1:0', auth_token=KEY)


def test_direct_runs_transport_cannot_bypass_guard(server):
    endpoint, state = server
    direct = DirectHermesApi(base_url=endpoint, auth_token=KEY)
    async def run():
        with pytest.raises(HermesTransportError):
            await direct.runtime_transport().request('POST', endpoint+'/v1/runs',
                headers={'Authorization': 'Bearer '+KEY}, body={'session_id': 'ungranted'}, timeout_seconds=1)
    asyncio.run(run())
    assert not state['calls']


@pytest.mark.parametrize('field', ['model', 'provider', 'input', 'session_id', 'model_options', 'instructions'])
def test_first_post_mutation_is_rejected_before_budget_verification(server, monkeypatch, field):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    transport = port._runtime._transport
    original = transport.request
    async def mutate(method, url, *, headers, body, timeout_seconds):
        if method == 'POST' and url.endswith('/v1/runs'):
            body = copy.deepcopy(body)
            body[field] = {'max_tokens':999999} if field == 'model_options' else 'replaced-by-test'
        return await original(method, url, headers=headers, body=body, timeout_seconds=timeout_seconds)
    monkeypatch.setattr(transport, 'request', mutate)
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'failed' and not submitted(state)
    assert not port._submission_gate._budget_port.calls


def test_concurrent_duplicate_during_verification_cannot_submit_twice(server, monkeypatch):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    budget = port._submission_gate._budget_port
    transport = port._runtime._transport
    original = transport.request
    captured = {}
    async def observe(method, url, *, headers, body, timeout_seconds):
        if method == 'POST' and url.endswith('/v1/runs'):
            captured['body'] = copy.deepcopy(body)
        return await original(method, url, headers=headers, body=body, timeout_seconds=timeout_seconds)
    monkeypatch.setattr(transport, 'request', observe)
    async def run():
        budget.block = asyncio.Event()
        first = asyncio.create_task(execute(port))
        for _ in range(100):
            if budget.calls: break
            await asyncio.sleep(0.001)
        assert len(budget.calls) == 1
        with pytest.raises(RuntimeBridgeError, match='replayed'):
            await port._submission_gate.before_submit(endpoint=endpoint, body=captured['body'])
        budget.block.set()
        _, receipt = await first
        assert receipt['status'] == 'completed'
    asyncio.run(run())
    assert len(submitted(state)) == 1 and len(budget.calls) == 1


def test_bootstrap_without_host_enforcement_stays_unregistered(tools, monkeypatch):
    from hermes_life_bridge import runtime_adapter
    from hermes_life_bridge.native_bootstrap import bootstrap_hermes_runs
    monkeypatch.setattr(runtime_adapter, '_BINDINGS', {})
    async def forbidden(*args, **kwargs):
        pytest.fail('no execution or grant callback may run')
    class Admission:
        admit = staticmethod(forbidden)
    async def run():
        with pytest.raises(RuntimeBridgeError, match='existing_hard_budget_port_required'):
            await bootstrap_hermes_runs(tools.test_config, base_url='http://127.0.0.1:1',
                auth_token=KEY, model=MODEL, admission=Admission(), authority_verifier=FixtureAuthority(),
                prepare=forbidden, resolve_governed=forbidden, accept_output=forbidden,
                origin='SYNTHETIC_TEST')
    asyncio.run(run())
    assert runtime_adapter._BINDINGS == {}


def test_no_budget_port_has_no_permissive_default():
    for value in (None, True, {}, object()):
        with pytest.raises(RuntimeBridgeError, match='existing_hard_budget_port_required'):
            HostBudgetAdmission(authority_verifier=FixtureAuthority(), budget_port=value,
                endpoint='http://127.0.0.1:8642', tenant_id='test', life_did='did:test:life',
                origin='LIVE_RUNTIME_REPORTED')


@pytest.mark.parametrize('mutation', ['replay','model','provider','prompt','session','options','endpoint'])
def test_verified_submission_cannot_be_reused_or_mutated(server, mutation):
    endpoint, state = server
    port, _ = setup_port(endpoint)
    async def run():
        _, receipt = await execute(port)
        assert receipt['status'] == 'completed'
        body = copy.deepcopy(submitted(state)[0][2])
        target = endpoint
        if mutation == 'model': body['model'] = 'other-model'
        elif mutation == 'provider': body['provider'] = 'other-provider'
        elif mutation == 'prompt': body['input'] = 'private replacement'
        elif mutation == 'session': body['session_id'] = 'other-life'
        elif mutation == 'options': body['model_options'] = {'max_tokens': 999999}
        elif mutation == 'endpoint': target += '/p/otherlife'
        with pytest.raises(RuntimeBridgeError):
            await port._submission_gate.before_submit(endpoint=target, body=body)
        assert len(port._submission_gate._budget_port.calls) == 1
    asyncio.run(run())
    assert len(submitted(state)) == 1
