"""Real HTTP sockets + actual AF/DLS adapters; server/authority are SYNTHETIC.

No installed gateway, provider credential, native web tool or model is contacted.
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip('agent_factory.runtimes.hermes_native')
pytest.importorskip('digital_life_native_capability')
from agent_factory._validation import wire_sha256
from agent_factory.contracts import (AccessMethod, AuthorizationOutcome, ContextProjection,
    ExecutionAuthorization, ModelConfig, RiskLevel, SideEffectPolicy)
from agent_factory.contracts.runtime_invocation import CapabilityIntent, GovernedInvocation
from agent_factory.policy.execution import ExecutionAuthorityVerification
from agent_factory.runtimes.hermes import HermesAdapter
from agent_factory.runtimes.hermes_native_api import HermesApiEvidenceRegistry, HermesApiNativeEvidencePort

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.hermes_direct_api import DirectHermesApi
from hermes_life_bridge.hermes_runs_binding import HermesRunsNativePort
from hermes_life_bridge.hermes_native_events import digest
from hermes_life_bridge.runtime_adapter import HermesRuntimeAdapter, RuntimeBridgeError
from native_bridge_fixtures import intent_and_selection
from host_budget_fixtures import FixtureHostBudget
from hermes_life_bridge.host_budget_admission import HostBudgetAdmission

KEY = 'synthetic-api-token-not-a-real-credential'
MODEL = ModelConfig('fixture-provider', 'fixture-model', AccessMethod.API)


class FixtureAuthority:
    def __init__(self):
        self.deny = False
        self.calls = []

    def verify(self, authorization, workspace_grant, *, at):
        self.calls.append(authorization.authorization_id)
        if self.deny:
            raise PermissionError('PRIVATE_AUTHORITY_ERROR')
        return ExecutionAuthorityVerification(verification_ref='authority-verification://fixture',
            verifier_id='synthetic-only', authorization_hash=wire_sha256(authorization.to_dict()),
            workspace_grant_hash=None, verified_at=at, expiry=authorization.expiry)


@pytest.fixture
def server():
    state = {'calls': [], 'readonly': True, 'events': [], 'run_status': 'completed',
             'stop_status': 'cancelled', 'redirect': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            body = None
            if self.command == 'POST':
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(raw) if raw else None
            # The fixture does not retain headers/token values in diagnostic logs.
            state['calls'].append((self.command, self.path, body,
                self.headers.get('Idempotency-Key')))
            if self.path != '/health' and self.headers.get('Authorization') != 'Bearer ' + KEY:
                self.respond({'error': 'unauthorized'}, 401)
                return
            if state['redirect']:
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:1/should-not-be-called')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/health':
                self.respond({'status': 'ok'})
            elif self.path == '/v1/capabilities':
                self.respond({'object': 'hermes.api_server.capabilities', 'platform': 'hermes-agent',
                    'runtime': {'mode': 'server_agent', 'tool_execution': 'server', 'split_runtime': False},
                    'features': {k: True for k in ('run_submission', 'run_status', 'run_events_sse', 'tool_progress_events', 'governed_execution_context')},
                    'endpoints': {'runs': {'method': 'POST', 'path': '/v1/runs'},
                        'run_status': {'method': 'GET', 'path': '/v1/runs/{run_id}'},
                        'run_events': {'method': 'GET', 'path': '/v1/runs/{run_id}/events'},
                        'toolsets': {'method': 'GET', 'path': '/v1/toolsets'}}})
            elif self.path == '/v1/toolsets':
                tools = ['web_search', 'web_extract'] + ([] if state['readonly'] else ['terminal'])
                self.respond({'object': 'list', 'platform': 'api_server',
                    'data': [{'name': 'web', 'enabled': True, 'configured': True, 'tools': tools}]})
            elif self.path == '/v1/runs' and self.command == 'POST':
                state['session_id'] = body.get('session_id')
                state['execution_context_ref'] = digest(body['execution_context'])
                self.respond({'run_id': 'run_fixture', 'status': 'running'}, 202)
            elif self.path == '/v1/runs/run_fixture/stop':
                state['run_status'] = state['stop_status']
                self.respond({'status': 'stopping'})
            elif self.path == '/v1/runs/run_fixture':
                self.respond({'object': 'hermes.run', 'run_id': 'run_fixture',
                    'status': state['run_status'], 'output': 'PUBLIC_FIXTURE_ANSWER',
                    'session_id': 'wrong_session' if state.get('wrong_session') else state.get('session_id'),
                    'execution_context_ref': ('sha256:' + '0' * 64)
                        if state.get('wrong_context_ref') else state.get('execution_context_ref'),
                    'runtime': {'model': MODEL.model, 'provider': MODEL.provider},
                    'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}})
            elif self.path == '/v1/runs/run_fixture/events':
                events = state['events'] or [
                    {'event': 'tool.started', 'tool': 'web_search', 'preview': 'PRIVATE_QUERY'},
                    {'event': 'tool.completed', 'tool': 'web_search', 'error': False,
                     'duration': 0.01, 'preview': 'PUBLIC_SEARCH_SOURCE_SNAPSHOT'},
                    {'event': 'tool.started', 'tool': 'web_extract'},
                    {'event': 'tool.completed', 'tool': 'web_extract', 'error': False,
                     'duration': 0.02, 'preview': 'PUBLIC_READ_SOURCE_SNAPSHOT'},
                    {'event': 'run.completed'}]
                payload = ''.join('data: ' + json.dumps(dict(e, run_id='run_fixture')) + '\n\n'
                    for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.respond({'error': 'not found'}, 404)

        def respond(self, value, status=200):
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = handle_request
        do_POST = handle_request
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=http.serve_forever, daemon=True)
    t.start()
    try:
        yield 'http://127.0.0.1:' + str(http.server_port), state
    finally:
        http.shutdown()
        http.server_close()
        t.join(timeout=2)


def setup_port(endpoint, *, tamper=None, accept=True):
    authority = FixtureAuthority()
    budget_port = FixtureHostBudget()
    gate = HostBudgetAdmission(authority_verifier=authority, budget_port=budget_port,
        endpoint=endpoint, tenant_id='tenant-test', life_did='did:arthurverse:test-life',
        origin='SYNTHETIC_TEST')
    direct = DirectHermesApi(base_url=endpoint, auth_token=KEY, submission_gate=gate)
    runtime = HermesAdapter(base_url=endpoint, auth_token=KEY,
        transport=direct.runtime_transport(), supported_model_providers=(MODEL.provider,),
        supported_access_methods=(AccessMethod.API,), supported_models=(MODEL.model,),
        supported_cognitive_capabilities=('public_research',))
    evidence = HermesApiNativeEvidencePort(transport=direct, registry=HermesApiEvidenceRegistry())
    async def resolve(request):
        binding = port.binding_metadata(request)
        budget = request.intent['budget']
        expiry = datetime.now(timezone.utc) + timedelta(seconds=budget['maxRuntimeMs']/1000 - 0.05)
        af_intent = CapabilityIntent(request.intent['intentId'], port.life_did, 'fixture-life',
            'public_research', request.intent['request'], ('public_web',), RiskLevel.L0,
            budget['maxTokens'], Decimal(budget['maxCostUsd']), expiry,
            json.dumps({'dlsBinding': binding, 'maxNativeCalls': budget['maxNativeCalls'],
                'maxRuntimeMs': budget['maxRuntimeMs']}))
        issued = datetime.now(timezone.utc) - timedelta(seconds=1)
        projection = ContextProjection('fixture-projection', port.life_did, 'fixture-self-gateway',
            'test-only', issued, expiry, project_state={'capability_intent': af_intent.to_dict(),
                'dlsBinding': binding}, source_hashes=tuple(request.intent['contextBinding'].values()))
        auth = ExecutionAuthorization('fixture-auth', af_intent.request_id, port.life_did,
            projection.projection_hash(), AuthorizationOutcome.ALLOW, frozenset({'public_research'}),
            frozenset({'public_web'}), RiskLevel.L0, SideEffectPolicy.NONE,
            budget['maxTokens'], Decimal(budget['maxCostUsd']), 'fixture-self-gateway', issued,
            expiry, 'SYNTHETIC_SIGNATURE_NOT_PRODUCTION')
        governed = GovernedInvocation(request.invocation_id, af_intent, 'hermes', port.implementation_id,
            1, budget['maxTokens'], Decimal(budget['maxCostUsd']), projection, auth)
        return tamper(governed) if tamper else governed
    async def accept_output(request, output, receipt):
        assert output == 'PUBLIC_FIXTURE_ANSWER'
        return accept
    port = HermesRunsNativePort(tenant_id='tenant-test', life_did='did:arthurverse:test-life',
        runtime=runtime, model=MODEL, evidence=evidence, authority_verifier=authority,
        resolve_governed=resolve, origin='SYNTHETIC_TEST', accept_output=accept_output,
        submission_gate=gate)
    return port, authority


async def execute(port):
    cfg = BridgeConfig(life_did=port.life_did, runtime_socket='unused', trace_path='unused')
    class AlreadyGovernedFixture:
        async def admit(self, intent, selection, descriptor, native):
            return None  # explicitly synthetic existing-host admission, not shipping code
    descriptor = await port.discover()
    adapter = HermesRuntimeAdapter(cfg, tenant_id=port.tenant_id, descriptor=descriptor,
        native=port, admission=AlreadyGovernedFixture())
    intent, selection = intent_and_selection(adapter)
    receipt = await adapter.invoke(intent, selection)
    return adapter, receipt


def test_real_socket_transport_through_actual_af_and_dls(server, monkeypatch):
    endpoint, state = server
    # Even hostile inherited proxy settings cannot redirect this explicit transport.
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    port, authority = setup_port(endpoint)
    async def run():
        adapter, receipt = await execute(port)
        assert receipt['status'] == 'completed', json.dumps(port.diagnostics(), sort_keys=True)
        assert receipt['origin'] == 'SYNTHETIC_TEST'
        assert receipt['executionRuntime'] == 'hermes'
        assert len(receipt['nativeEvents']) == 4
        assert receipt['usage']['inputTokens'] == 10
        assert receipt['usage']['actualCost'] is None
        answer = await port.read_answer(receipt)
        assert answer['taskAccepted'] and answer['text'] == 'PUBLIC_FIXTURE_ANSWER'
        assert port.diagnostics()['attemptCount'] == 0
        assert port._submission_gate.diagnostics()['boundCount'] == 0
        assert len(port._submission_gate._budget_port.retired) == 1
        with pytest.raises(RuntimeError, match='unavailable'):
            await port.read_answer(receipt)
        rendered = json.dumps([receipt, adapter.project_receipt(receipt)])
        assert 'PRIVATE_QUERY' not in rendered and 'PUBLIC_FIXTURE_ANSWER' not in rendered
        assert all(ref.startswith('sha256:') for ref in receipt['evidenceRefs'])
    asyncio.run(run())
    assert authority.calls
    posts = [c for c in state['calls'] if c[0] == 'POST' and c[1] == '/v1/runs']
    assert len(posts) == 1 and posts[0][3].startswith('rlb-')
    assert 'Public fixture topic' in posts[0][2]['input']
    assert 'PRIVATE_QUERY' not in posts[0][2]['input']
    # AF evidence and DLS callbacks consume exactly one authenticated snapshot.
    assert sum(c[1].endswith('/events') for c in state['calls']) == 1


def test_effectful_api_profile_never_submits_run(server):
    endpoint, state = server
    state['readonly'] = False
    port, _ = setup_port(endpoint)
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'unavailable'
    assert not any(c[0] == 'POST' for c in state['calls'])


def test_real_af_authority_denial_prevents_submission(server):
    endpoint, state = server
    port, authority = setup_port(endpoint)
    authority.deny = True
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'failed'
    assert not any(c[0] == 'POST' for c in state['calls'])
    assert 'PRIVATE_AUTHORITY_ERROR' not in json.dumps(receipt)
    assert port.diagnostics()['attemptCount'] == 0


@pytest.mark.parametrize('mode', ['runtime', 'objective', 'context', 'budget'])
def test_exact_cross_project_binding_rejects_drift(server, mode):
    endpoint, state = server
    def tamper(governed):
        if mode == 'runtime':
            return replace(governed, selected_runtime='openclaw')
        if mode == 'objective':
            return replace(governed, intent=replace(governed.intent, objective='different'))
        if mode == 'context':
            return replace(governed, projection=replace(governed.projection, project_state={}))
        return replace(governed, intent=replace(governed.intent, constraints_json='{}'))
    port, _ = setup_port(endpoint, tamper=tamper)
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'failed'
    assert not any(c[0] == 'POST' for c in state['calls'])


def test_native_success_is_not_task_acceptance(server):
    endpoint, _ = server
    port, _ = setup_port(endpoint, accept=False)
    async def run():
        _, receipt = await execute(port)
        assert receipt['status'] == 'completed'
        answer = await port.read_answer(receipt)
        assert not answer['taskAccepted']
    asyncio.run(run())


def test_expired_success_answer_retires_only_local_correlation(server):
    endpoint, _ = server
    port, _ = setup_port(endpoint)
    port._answer_ttl = 1.0

    async def run():
        _, receipt = await execute(port)
        invocation_id = receipt['invocationId']
        entry = port._attempts[invocation_id]
        entry.observed_at -= 2.0
        with pytest.raises(RuntimeBridgeError, match='native_answer_expired'):
            await port.read_answer(receipt)
        assert port.diagnostics()['attemptCount'] == 0
        assert port._submission_gate.diagnostics()['boundCount'] == 0
        assert len(port._submission_gate._budget_port.retired) == 1

    asyncio.run(run())


@pytest.mark.parametrize('stop_status,confirmed', [('cancelled', True), ('stopping', False)])
def test_cancellation_requires_actual_bound_terminal_not_stop_ack(server, stop_status, confirmed):
    endpoint, state = server
    state['run_status'] = 'running'
    state['stop_status'] = stop_status
    port, _ = setup_port(endpoint)
    async def run():
        _, receipt = await execute(port)
        assert receipt['status'] != 'completed'
        assert receipt['cancellationConfirmed'] is confirmed
        assert receipt['executionRuntime'] is None
        assert not receipt['nativeEvents']
        assert port.diagnostics()['attemptCount'] == (0 if confirmed else 1)
        assert port._submission_gate.diagnostics()['boundCount'] == (0 if confirmed else 1)
        assert len(port._submission_gate._budget_port.retired) == (1 if confirmed else 0)
    asyncio.run(run())
    assert sum(c[0] == 'POST' and c[1] == '/v1/runs' for c in state['calls']) == 1
    assert any(c[1].endswith('/stop') for c in state['calls'])


def test_wrong_terminal_session_cannot_be_accepted_or_confirm_cancel(server):
    endpoint, state = server
    state['wrong_session'] = True
    port, _ = setup_port(endpoint)
    async def run():
        _, receipt = await execute(port)
        assert receipt['status'] == 'failed'
        assert receipt['executionRuntime'] is None and receipt['nativeEvents'] == []
        assert receipt['cancellationConfirmed'] is False
        assert port.diagnostics()['attempts'][0]['phase'] in {
            'invoke_existing_runtime', 'verify_runtime_receipt'}
    asyncio.run(run())
    assert not any(c[1].endswith('/stop') for c in state['calls'])


def test_missing_runtime_result_evidence_cannot_satisfy_discover_read(server):
    endpoint, state = server
    state['events'] = [
        {'event': 'tool.started', 'tool': 'web_search'},
        {'event': 'tool.completed', 'tool': 'web_search', 'error': False},
        {'event': 'tool.started', 'tool': 'web_extract'},
        {'event': 'tool.completed', 'tool': 'web_extract', 'error': False},
        {'event': 'run.completed'}]
    port, _ = setup_port(endpoint)
    _, receipt = asyncio.run(execute(port))
    assert receipt['status'] == 'unverified'
    assert receipt['errorClass'] == 'NATIVE_EVIDENCE_MISSING'
    assert receipt['evidenceRefs'] == []


def test_missing_captured_snapshot_is_a_bounded_contract_failure(server, monkeypatch):
    from hermes_life_bridge import hermes_runs_binding as module
    endpoint, state = server
    async def forward_without_retaining(self, path):
        return await self.delegate.get_events(path)
    monkeypatch.setattr(module._CaptureTransport, 'get_events', forward_without_retaining)
    port, _ = setup_port(endpoint)
    async def run():
        _, receipt = await execute(port)
        assert receipt['status'] == 'failed'
        assert receipt['nativeEvents'] == [] and receipt['executionRuntime'] is None
        assert port.diagnostics()['attemptCount'] == 0
        assert port._submission_gate.diagnostics()['boundCount'] == 0
        assert len(port._submission_gate._budget_port.retired) == 1
    asyncio.run(run())
    assert sum(c[0] == 'POST' and c[1] == '/v1/runs' for c in state['calls']) == 1


def test_redirect_is_rejected_without_following(server):
    endpoint, state = server
    state['redirect'] = True
    port, _ = setup_port(endpoint)
    with pytest.raises(RuntimeError):
        asyncio.run(port.discover())
    assert not any(c[0] == 'POST' for c in state['calls'])
