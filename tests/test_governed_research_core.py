from __future__ import annotations

import asyncio

import pytest

from hermes_life_bridge import governed_research as g


def _pending(bridge):
    return (bridge, 'dlcall:' + '1' * 32, None, {
        'query': 'public topic',
        'session': '',
        'max_age_days': None,
        'timeout_seconds': 1,
    })


def test_sync_finalize_failure_is_not_relabelled_unknown(monkeypatch):
    bridge = object()
    pending = _pending(bridge)
    monkeypatch.setattr(g, '_begin', lambda *a, **k: (pending, None))
    monkeypatch.setattr(g, 'dispatch_sync', lambda *a, **k: {'executionReceipt': {}})
    monkeypatch.setattr(g, '_finish', lambda *a, **k: (_ for _ in ()).throw(ValueError('finalize-failed')))
    monkeypatch.setattr(g, '_unknown', lambda *a, **k: pytest.fail('must not rewrite a completed dispatch as unknown'))
    with pytest.raises(ValueError, match='finalize-failed'):
        g.research_via_bound_bridge(object(), 'public topic')


def test_sync_dispatch_failure_is_marked_unknown(monkeypatch):
    bridge = object()
    pending = _pending(bridge)
    monkeypatch.setattr(g, '_begin', lambda *a, **k: (pending, None))
    monkeypatch.setattr(g, 'dispatch_sync', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('dispatch-failed')))
    monkeypatch.setattr(g, '_unknown', lambda _tools, rid: {'status': 'unresolved', 'request_id': rid})
    result = g.research_via_bound_bridge(object(), 'public topic')
    assert result['status'] == 'unresolved'


def test_async_finalize_failure_is_not_relabelled_unknown(monkeypatch):
    class Bridge:
        async def dispatch(self, **_kwargs):
            return {'executionReceipt': {}}
    bridge = Bridge()
    pending = _pending(bridge)
    monkeypatch.setattr(g, '_begin', lambda *a, **k: (pending, None))
    monkeypatch.setattr(g, '_finish', lambda *a, **k: (_ for _ in ()).throw(ValueError('finalize-failed')))
    monkeypatch.setattr(g, '_unknown', lambda *a, **k: pytest.fail('must not rewrite a completed dispatch as unknown'))
    with pytest.raises(ValueError, match='finalize-failed'):
        asyncio.run(g.research_via_bound_bridge_async(object(), 'public topic'))
