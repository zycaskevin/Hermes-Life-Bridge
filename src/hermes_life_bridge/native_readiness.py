"""Content-free, no-credential readiness observation for a named local API.

A separate CLI process cannot observe the resident plugin's in-memory registry.
It reports NOT_OBSERVED rather than misreporting it as unbound or live-ready.
Public /health is liveness only; it never authorizes an execution.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import re
from urllib.parse import urlsplit


def native_readiness(*, endpoint: str, probe: bool = True) -> dict:
    result = {'schema': 'hlb.native-readiness.v1', 'mode': 'read-only-no-credentials',
        'nativeExecutionReady': False, 'residentRegistration': 'NOT_OBSERVED',
        'authenticatedDiscovery': 'NOT_CHECKED', 'authority': 'NOT_OBSERVED',
        'hardBudgetEnforcement': 'NOT_OBSERVED', 'liveNativeRun': 'NOT_EXECUTED',
        'contactPathChanged': False, 'apiLiveness': 'NOT_CHECKED', 'blockers': []}
    dependencies = {}
    for name in ('digital_life_native_capability', 'digital_life_native_projection', 'agent_factory'):
        try:
            dependencies[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, AttributeError):
            dependencies[name] = False
    result['dependenciesInThisProcess'] = dependencies
    if not all(dependencies.values()):
        result['blockers'].append('NATIVE_DEPENDENCIES_NOT_AVAILABLE_IN_THIS_PROCESS')
    try:
        parsed = urlsplit(endpoint)
        prefix = parsed.path.rstrip('/')
        port = parsed.port if parsed.port is not None else 80
        valid = (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1'}
            and 1 <= port <= 65535
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and (not prefix or re.fullmatch(r'/p/[A-Za-z0-9_-]{1,64}', prefix)))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        result['apiLiveness'] = 'INVALID_ENDPOINT'
        result['blockers'].append('EXPLICIT_LOOPBACK_ENDPOINT_REQUIRED')
        return result
    if probe:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=1)
        try:
            # Direct HTTP avoids inherited proxy settings and follows no redirect.
            connection.request('GET', prefix + '/health', headers={'Accept': 'application/json'})
            response = connection.getresponse()
            raw = response.read(4097)
            if (response.status != 200 or len(raw) > 4096
                    or 'json' not in (response.getheader('Content-Type') or '').lower()):
                result['apiLiveness'] = 'UNVERIFIED_RESPONSE'
            else:
                body = json.loads(raw)
                result['apiLiveness'] = 'REACHABLE' if isinstance(body, dict) and body.get('status') == 'ok' else 'UNVERIFIED_RESPONSE'
        except Exception:
            result['apiLiveness'] = 'UNREACHABLE'
        finally:
            connection.close()
        if result['apiLiveness'] != 'REACHABLE':
            result['blockers'].append('HERMES_API_NOT_REACHABLE_OR_UNVERIFIED')
    result['blockers'].extend(['AUTHENTICATED_READONLY_DISCOVERY_REQUIRED',
        'EXISTING_AUTHORITY_AND_HOST_BUDGET_ADMISSION_REQUIRED',
        'RESIDENT_BOOTSTRAP_AND_LIVE_NATIVE_ACCEPTANCE_REQUIRED'])
    return result
