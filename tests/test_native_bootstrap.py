"""Trusted bootstrap with real local HTTP transport and synthetic authorities."""
import asyncio
import json

import pytest

pytest.importorskip('agent_factory.runtimes.hermes_native')
pytest.importorskip('digital_life_native_capability')

from hermes_life_bridge import runtime_adapter
from hermes_life_bridge.native_bootstrap import bootstrap_hermes_runs
from hermes_life_bridge.governed_research import research_via_bound_bridge_async
from hermes_life_bridge.runtime_adapter import RuntimeBridgeError
from test_agent_tools import tools
from test_hermes_runs_binding import server, setup_port, KEY, MODEL
from native_bridge_fixtures import intent_and_selection
from host_budget_fixtures import FixtureHostBudget


async def build(tools, endpoint):
    helper, authority = setup_port(endpoint)
    holder = {}
    class Admission:
        async def admit(self, *args):
            return None  # SYNTHETIC_TEST fixture, no production grant
    async def prepare(*, request, session_ref, budget):
        return intent_and_selection(holder['installed'].bridge.adapter, request, budget)
    installed = await bootstrap_hermes_runs(tools.test_config, base_url=endpoint,
        auth_token=KEY, model=MODEL, admission=Admission(), authority_verifier=authority,
        prepare=prepare, resolve_governed=helper._resolve,
        accept_output=helper._accept_output, origin='SYNTHETIC_TEST', budget_port=FixtureHostBudget())
    holder['installed'] = installed
    return installed


def test_async_and_sync_workers_share_real_host_loop_without_false_reservations(tools, server, monkeypatch):
    endpoint, state = server
    monkeypatch.setattr(runtime_adapter, '_BINDINGS', {})
    async def run():
        installed = await build(tools, endpoint)
        assert installed.status()['liveExecutionVerified'] is False
        # A sync tool called in the event-loop thread cannot block that loop;
        # this is rejected before reservation, so async execution remains usable.
        immediate = tools.research('wrong calling mode')
        assert immediate['error'] == 'native_bridge_requires_host_async_dispatch'
        with tools._db() as db:
            assert db.execute('select count(*) from calls').fetchone()[0] == 0
        first = await research_via_bound_bridge_async(tools, 'first public topic')
        assert first['ok'] and first['summary'] == 'PUBLIC_FIXTURE_ANSWER', first
        # Existing synchronous Hermes worker adapters may rendezvous with the
        # host-owned event loop, not launch a second Runtime or hidden loop.
        second = await asyncio.to_thread(tools.research, 'second public topic')
        assert second['ok'], second
        third = await research_via_bound_bridge_async(tools, 'third public topic')
        assert third['error'] == 'research_budget_or_concurrency_limit'
        with tools._db() as db:
            rows = db.execute('select kind,status,result_json from calls').fetchall()
        assert len(rows) == 2 and all(r[0] == 'native_research' for r in rows)
        assert all(r[1] == 'completed' for r in rows)
        assert 'PUBLIC_FIXTURE_ANSWER' not in json.dumps(rows)
        assert 'first public topic' not in json.dumps(rows)
        assert KEY not in repr(installed)
    asyncio.run(run())
    assert sum(c[0] == 'POST' and c[1] == '/v1/runs' for c in state['calls']) == 2


def test_unhealthy_or_unadmitted_bootstrap_does_not_register_or_change_contact(tools, server, monkeypatch):
    endpoint, state = server
    monkeypatch.setattr(runtime_adapter, '_BINDINGS', {})
    state['readonly'] = False
    with pytest.raises(RuntimeBridgeError, match='host_not_ready'):
        asyncio.run(build(tools, endpoint))
    assert runtime_adapter._BINDINGS == {}
    assert not any(c[0] == 'POST' for c in state['calls'])
    assert tools.test_config.contact_delivery_enabled is False


def test_existing_registration_is_not_replaced(tools, server, monkeypatch):
    endpoint, _ = server
    monkeypatch.setattr(runtime_adapter, '_BINDINGS', {})
    async def run():
        first = await build(tools, endpoint)
        with pytest.raises(RuntimeBridgeError, match='already_registered'):
            await build(tools, endpoint)
        assert list(runtime_adapter._BINDINGS.values()) == [first.bridge]
    asyncio.run(run())
