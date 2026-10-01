"""Metadata-only reduction of authenticated native event fixtures."""
import copy
import json

import pytest

from hermes_life_bridge.hermes_native_events import reduce_native_events
from hermes_life_bridge.runtime_adapter import RuntimeBridgeError


def events():
    return [
        {'event': 'tool.started', 'tool': 'web_search', 'preview': 'PRIVATE_QUERY'},
        {'event': 'tool.completed', 'tool': 'web_search', 'error': False, 'duration': 0.01,
         'preview': 'SECRET_TOOL_PAYLOAD https://example.com/?token=PRIVATE'},
        {'event': 'tool.started', 'tool': 'web_extract'},
        {'event': 'tool.completed', 'tool': 'web_extract', 'error': False,
         'preview': 'SOURCE_PREVIEW'},
        {'event': 'run.completed'}]


def reduce(data, limit=2):
    emitted = []
    info = reduce_native_events(data, run_id='run_fixture', invocation_id='native:'+'a'*32,
        allowed_tools={'web_search', 'web_extract'}, max_calls=limit, emit=emitted.append)
    return info, emitted


def test_metadata_only_and_honest_serial_correlation():
    raw = events()
    raw.insert(0, {'event': 'reasoning.delta', 'text': 'PRIVATE_REASONING'})
    raw.insert(0, {'event': 'message.delta', 'text': 'MODEL_CLAIMS_SEARCH_EXECUTED'})
    info, emitted = reduce(raw)
    assert info['correlation'] == ['serialized_observation']
    assert info['callCount'] == 2
    encoded = json.dumps([info, emitted])
    for value in ('PRIVATE', 'SECRET_TOOL_PAYLOAD', 'SOURCE_PREVIEW', 'MODEL_CLAIMS'):
        assert value not in encoded
    assert emitted[0]['toolCallId'] == emitted[1]['toolCallId']
    assert emitted[2]['toolCallId'] == emitted[3]['toolCallId']
    assert all(ref.startswith('sha256:') for e in emitted for ref in e['evidenceRefs'])


def test_parallel_same_name_calls_with_real_ids_pair_exactly():
    raw = [
        {'event': 'tool.started', 'tool': 'web_search', 'tool_call_id': 'a'},
        {'event': 'tool.started', 'tool': 'web_search', 'tool_call_id': 'b'},
        {'event': 'tool.completed', 'tool': 'web_search', 'tool_call_id': 'b',
         'error': False, 'preview': 'B'},
        {'event': 'tool.completed', 'tool': 'web_search', 'tool_call_id': 'a',
         'error': False, 'preview': 'A'},
        {'event': 'run.completed'}]
    info, out = reduce(raw)
    assert info['correlation'] == ['native_id']
    assert out[0]['toolCallId'] == out[3]['toolCallId']
    assert out[1]['toolCallId'] == out[2]['toolCallId']


@pytest.mark.parametrize('mode', ['no_id_parallel', 'unpaired', 'wrong_run', 'terminal_first',
    'no_terminal', 'double_terminal', 'after_terminal', 'unexpected_tool', 'approval',
    'subagent', 'truncated', 'no_error_flag', 'invalid_duration', 'too_many'])
def test_invalid_or_ambiguous_native_evidence_never_emits_success(mode):
    raw = events()
    if mode == 'no_id_parallel': raw.insert(1, copy.deepcopy(raw[0]))
    elif mode == 'unpaired': raw.pop(0)
    elif mode == 'wrong_run': raw[1]['run_id'] = 'other_run'
    elif mode == 'terminal_first': raw.insert(0, {'event': 'run.completed'})
    elif mode == 'no_terminal': raw.pop()
    elif mode == 'double_terminal': raw.append({'event': 'run.completed'})
    elif mode == 'after_terminal': raw.append(copy.deepcopy(raw[0]))
    elif mode == 'unexpected_tool': raw[0]['tool'] = 'terminal'
    elif mode == 'approval': raw.insert(0, {'event': 'approval.request'})
    elif mode == 'subagent': raw.insert(0, {'event': 'subagent.start'})
    elif mode == 'truncated': raw.insert(0, {'event': 'replay.truncated'})
    elif mode == 'no_error_flag': raw[1].pop('error')
    elif mode == 'invalid_duration': raw[1]['duration'] = float('nan')
    emitted = []
    with pytest.raises(RuntimeBridgeError):
        reduce_native_events(raw, run_id='run_fixture', invocation_id='native:'+'a'*32,
            allowed_tools={'web_search', 'web_extract'}, max_calls=1 if mode == 'too_many' else 2,
            emit=emitted.append)
    assert not emitted


def test_missing_preview_is_not_invented_as_source_evidence():
    raw = events()
    raw[1].pop('preview')
    _, out = reduce(raw)
    assert out[1]['evidenceRefs'] == []


def test_tool_error_retained_as_error_not_result():
    raw = events()
    raw[1]['error'] = True
    _, out = reduce(raw)
    assert out[1]['kind'] == 'error' and out[1]['errorClass'] == 'NATIVE_TOOL_FAILED'
    assert out[1]['evidenceRefs'] == []
