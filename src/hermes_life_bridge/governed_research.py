"""Life-scoped intent surface; delegates execution and evidence to DLS/Runtime.

No AF legacy researcher, model process or credential loader. Sync and async hosts
share one reservation/finalization path. Private answers never enter the ledger.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from .runtime_adapter import get_research_bridge, dispatch_sync, check_sync_dispatch, RuntimeBridgeError


_NATIVE_KIND = 'native_research'


def _needs_reconciliation(raw):
    try:
        metadata = json.loads(raw)
    except (ValueError, TypeError):
        return True
    if not isinstance(metadata, dict):
        return True
    return metadata.get('status') == 'unresolved' or metadata.get('reconciliationRequired') is True


def _reconciliation_block(request_id):
    return {'ok': False, 'request_id': request_id, 'status': 'unresolved',
        'error': 'native_reconciliation_required', 'executed': False,
        'executionReceipt': None, 'reconciliationRequired': True,
        'notice': 'An earlier native attempt is awaiting authoritative reconciliation. Its reservation remains locked; this request was not executed.'}


def _begin(tools, query, session, consumer, max_age_days, *, asynchronous):
    from .agent_tools import ToolBoundaryError, _text, _digest, _CONSUMERS
    from .research_freshness import ResearchFreshnessError, requested_days
    query = _text(query, 'query', 256)
    try:
        days = requested_days(query, max_age_days)
    except ResearchFreshnessError as exc:
        raise ToolBoundaryError(str(exc)) from exc
    if consumer not in _CONSUMERS - {'unknown'}:
        raise ToolBoundaryError('consumer_invalid')
    if re.search(
        r'sk-[A-Za-z0-9_-]{12,}|\bBearer\s|\b\d{5,16}:[A-Za-z0-9_-]{20,}|'
        r'/home/|file://|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', query
    ):
        raise ToolBoundaryError('public_query_required')
    bridge = get_research_bridge(life_did=tools.life_did,
        tenant_id=tools.scope['tenantId'], manifest_hash=tools.manifest['manifestHash'])
    if bridge is None:
        return None, {'ok': False, 'status': 'unavailable', 'error': 'native_bridge_not_bound',
            'executed': False, 'executionReceipt': None,
            'notice': 'No admitted native host is bound. No Runtime was invoked and no later result is pending.'}
    if not asynchronous:
        try:
            check_sync_dispatch(bridge)
        except RuntimeBridgeError as exc:
            return None, {'ok': False, 'error': str(exc), 'executed': False,
                          'executionReceipt': None}
    elif bridge.host_loop is not None and bridge.host_loop is not asyncio.get_running_loop():
        return None, {'ok': False, 'error': 'native_host_loop_mismatch', 'executed': False,
                      'executionReceipt': None}
    now = time.time()
    rid = 'dlcall:' + uuid.uuid4().hex
    request_hash = _digest(json.dumps({'version': 'dls-native-v2', 'query': query,
        'maxAgeDays': days, 'consumer': consumer}, sort_keys=True))
    with tools._db() as db:
        db.execute('begin immediate')
        prior = db.execute(
            'select request_id,status,result_json from calls where kind=? '
            'and query_hash=? and session_hash=? and started>? order by started desc limit 1',
            (_NATIVE_KIND, request_hash, _digest(session), now - 300)).fetchone()
        if prior:
            if prior[1] == 'completed':
                replay = {**json.loads(prior[2]), 'replayed': True,
                    'notice': 'Historical execution metadata only; owner answer was not cached. Not a new research result.'}
                if replay.get('ok') is False:
                    replay['error'] = 'replay_answer_not_cached'
                return None, replay
            if prior[1] == 'running' and _needs_reconciliation(prior[2]):
                return None, _reconciliation_block(prior[0])
            return None, {'ok': False, 'request_id': prior[0],
                'error': 'duplicate_request_' + prior[1], 'executed': False}
        # Diagnostic only: do not expire, unlock or relabel unresolved attempts.
        pending_native = db.execute(
            "select request_id,result_json from calls where kind=? and status='running' order by started limit 1",
            (_NATIVE_KIND,)).fetchone()
        if pending_native and _needs_reconciliation(pending_native[1]):
            return None, _reconciliation_block(pending_native[0])
        # Include old research work in quotas but protect new unresolved attempts
        # with a distinct kind: the retained legacy expiry code cannot unlock them.
        active = db.execute("select count(*) from calls where kind in ('research','native_research') and status='running'").fetchone()[0]
        hour = db.execute("select count(*) from calls where kind in ('research','native_research') and consumer!='operator_diagnostic' and started>?", (now - 3600,)).fetchone()[0]
        day = db.execute("select count(*) from calls where kind in ('research','native_research') and consumer!='operator_diagnostic' and started>?", (now - 86400,)).fetchone()[0]
        if active or (consumer != 'operator_diagnostic' and (
            hour >= tools.policy['researchPerHour'] or day >= tools.policy['researchPerDay']
        )):
            return None, {'ok': False, 'error': 'research_budget_or_concurrency_limit', 'executed': False}
        db.execute('insert into calls(request_id,kind,query_hash,session_hash,started,status,consumer) values(?,?,?,?,?,?,?)',
            (rid, _NATIVE_KIND, request_hash, _digest(session), now, 'running', consumer))
    return (bridge, rid, days, {'query': query, 'session': session, 'max_age_days': days,
            'timeout_seconds': tools.policy['researchTimeoutSeconds']}), None


def _finish(tools, pending, outcome):
    bridge, rid, days, _ = pending
    receipt = bridge.adapter.projection.validate_receipt(outcome['executionReceipt'],
        bridge.adapter.capabilities(), tenant_id=tools.scope['tenantId'], life_did=tools.life_did)
    unresolved = (receipt['invocationAttempted'] and receipt['status'] != 'completed'
                  and receipt['cancellationConfirmed'] is not True)
    result = {'ok': receipt['status'] == 'completed' and outcome['taskAccepted'],
        'request_id': rid, 'status': 'unresolved' if unresolved else receipt['status'],
        'executed': receipt['executionRuntime'] is not None,
        'lifeDid': tools.life_did, 'runtime': receipt['executionRuntime'],
        'executionReceipt': receipt, 'taskAccepted': outcome['taskAccepted'],
        'answerAvailable': outcome['answerAvailable'], 'summary': outcome['summary'],
        'reconciliationRequired': unresolved,
        'freshness': {'requestedMaxAgeDays': days, 'verification': 'EXTERNAL_TASK_ACCEPTANCE_REQUIRED',
            'publicationDatesVerifiedByBridge': False, 'legacyFreshnessEngineUsed': False},
        'notice': 'Native execution evidence and task acceptance are distinct. DLS owns this receipt; HLB does not certify freshness or select a Runtime.'}
    metadata = {k: v for k, v in result.items() if k != 'summary'}
    metadata.update(answerAvailable=False, taskAccepted=False, ok=False)
    if unresolved:
        with tools._db() as db:
            db.execute('update calls set result_json=? where request_id=?', (json.dumps(metadata), rid))
    else:
        tools._finish(rid, 'completed' if receipt['status'] == 'completed' else 'failed', metadata)
    return result


def _unknown(tools, rid):
    result = {'ok': False, 'request_id': rid, 'status': 'unresolved',
        'error': 'native_invocation_requires_reconciliation', 'executionReceipt': None,
        'notice': 'Native execution could not be verified. No fallback was attempted. Existing authority must reconcile before retrying.'}
    with tools._db() as db:
        db.execute('update calls set result_json=? where request_id=?', (json.dumps(result), rid))
    return result


def research_via_bound_bridge(tools, query: str, *, session: str = '',
                              consumer: str = 'direct', max_age_days=None) -> dict:
    pending, immediate = _begin(tools, query, session, consumer, max_age_days, asynchronous=False)
    if pending is None:
        return immediate
    bridge, rid, _, kwargs = pending
    try:
        outcome = dispatch_sync(bridge, **kwargs)
    except Exception:
        return _unknown(tools, rid)
    return _finish(tools, pending, outcome)


async def research_via_bound_bridge_async(tools, query: str, *, session: str = '',
                                         consumer: str = 'direct', max_age_days=None) -> dict:
    pending, immediate = _begin(tools, query, session, consumer, max_age_days, asynchronous=True)
    if pending is None:
        return immediate
    bridge, rid, _, kwargs = pending
    try:
        outcome = await bridge.dispatch(**kwargs)
    except asyncio.CancelledError:
        _unknown(tools, rid)
        raise
    except Exception:
        return _unknown(tools, rid)
    return _finish(tools, pending, outcome)
