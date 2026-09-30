"""Direct loopback HTTP for the existing AF Hermes Runs/evidence contracts.

This transport does not select models, read secrets, start gateways or implement
native tools. Credentials and the exact endpoint are constructor inputs from
trusted host composition. Environment proxy variables are never consulted;
redirects, unrelated endpoints and credential-bearing URLs are rejected.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import http.client
import json
import math
import re
from urllib.parse import urlsplit

from .runtime_adapter import RuntimeBridgeError


class DirectHermesApi:
    def __init__(self, *, base_url: str, auth_token: str,
                 timeout_seconds: float = 5, max_bytes: int = 2_000_000,
                 submission_gate=None):
        try:
            parsed = urlsplit(base_url)
            port = 80 if parsed.port is None else parsed.port
        except (ValueError, TypeError):
            raise RuntimeBridgeError('hermes_endpoint_invalid') from None
        prefix = parsed.path.rstrip('/')
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (prefix and not re.fullmatch(r'/p/[A-Za-z0-9_-]{1,64}', prefix))
                or not 1 <= port <= 65535):
            raise RuntimeBridgeError('hermes_endpoint_requires_explicit_loopback')
        if (not isinstance(auth_token, str) or not auth_token
                or len(auth_token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in auth_token)):
            raise RuntimeBridgeError('hermes_api_credential_invalid')
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30
                or type(max_bytes) is not int or not 1024 <= max_bytes <= 4_000_000):
            raise RuntimeBridgeError('hermes_transport_bounds_invalid')
        self.base_url = base_url.rstrip('/')
        self._host, self._port, self._prefix = parsed.hostname, port, prefix
        self._token, self.timeout_seconds = auth_token, float(timeout_seconds)
        self.max_bytes = max_bytes
        self._submission_gate = submission_gate

    def __repr__(self):
        return '<DirectHermesApi credential=REDACTED>'

    @staticmethod
    def _allowed(method, path):
        if method == 'GET' and path in {'/health', '/v1/capabilities', '/v1/toolsets'}:
            return True
        if method == 'POST' and path == '/v1/runs':
            return True
        return bool(re.fullmatch(r'/v1/runs/[A-Za-z0-9_-]{1,256}' + (
            r'(?:/events)?' if method == 'GET' else r'/stop' if method == 'POST' else r'(?!)'), path))

    def _call(self, method: str, path: str, *, body=None, timeout=None, events=False):
        if not isinstance(path, str) or not self._allowed(method, path):
            raise RuntimeBridgeError('hermes_transport_path_denied')
        timeout = self.timeout_seconds if timeout is None else min(timeout, self.timeout_seconds)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeBridgeError('hermes_transport_timeout_invalid')
        headers = {'Accept': 'text/event-stream' if events else 'application/json'}
        if path != '/health':
            headers['Authorization'] = 'Bearer ' + self._token
        encoded = None
        if body is not None:
            try:
                encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()
            except (ValueError, TypeError):
                raise RuntimeBridgeError('hermes_transport_body_invalid') from None
            if len(encoded) > 1_000_000:
                raise RuntimeBridgeError('hermes_transport_body_too_large')
            headers['Content-Type'] = 'application/json'
            if method == 'POST' and path == '/v1/runs':
                # Exact body includes the invocation-scoped session; the same
                # submission can be reconciled by Hermes' durable idempotency.
                headers['Idempotency-Key'] = 'rlb-' + hashlib.sha256(encoded).hexdigest()
        connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        try:
            connection.request(method, self._prefix + path, body=encoded, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise RuntimeBridgeError('hermes_api_redirect_denied')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > self.max_bytes):
                raise RuntimeBridgeError('hermes_api_response_too_large')
            raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise RuntimeBridgeError('hermes_api_response_too_large')
            return response.status, response.getheader('Content-Type') or '', raw
        except RuntimeBridgeError:
            raise
        except Exception:
            raise RuntimeBridgeError('hermes_api_transport_unavailable') from None
        finally:
            connection.close()

    @staticmethod
    def _json(raw):
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        try:
            value = json.loads(raw, object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
            if not isinstance(value, dict):
                raise ValueError('not object')
            return value
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeBridgeError('hermes_api_json_invalid') from None

    async def get_json(self, path):
        from agent_factory.runtimes.hermes_native_api import HermesNativeApiResponse
        status, content_type, raw = await asyncio.to_thread(self._call, 'GET', path)
        if status != 200 or 'json' not in content_type.lower():
            raise RuntimeBridgeError('hermes_api_discovery_unavailable')
        return HermesNativeApiResponse(status, self._json(raw))

    async def get_events(self, path):
        if not isinstance(path, str) or not path.endswith('/events'):
            raise RuntimeBridgeError('hermes_transport_path_denied')
        status, content_type, raw = await asyncio.to_thread(self._call, 'GET', path, events=True)
        if status != 200 or not content_type.lower().startswith('text/event-stream'):
            raise RuntimeBridgeError('hermes_api_events_unavailable')
        try:
            lines = raw.decode('utf-8').splitlines()
        except UnicodeError:
            raise RuntimeBridgeError('hermes_api_events_invalid') from None
        result, data = [], []
        for line in [*lines, '']:
            if line.startswith('data:'):
                data.append(line[5:].lstrip(' '))
            elif line == '' and data:
                result.append(self._json('\n'.join(data)))
                data.clear()
                if len(result) > 1000:
                    raise RuntimeBridgeError('hermes_api_event_limit')
        return tuple(result)

    def runtime_transport(self):
        """Satisfy the existing AF abstract transport without changing AF code."""
        from agent_factory.runtimes.hermes import HermesTransport, HermesHttpResponse, HermesTransportError
        direct = self
        class RunsTransport(HermesTransport):
            async def request(self, method, url, *, headers, body, timeout_seconds):
                if not isinstance(url, str) or not url.startswith(direct.base_url + '/'):
                    raise HermesTransportError('protocol', 'Hermes endpoint mismatch')
                path = url[len(direct.base_url):]
                expected = None if path == '/health' else 'Bearer ' + direct._token
                if headers.get('Authorization') != expected:
                    raise HermesTransportError('protocol', 'Hermes credential binding mismatch')
                try:
                    bounded_body = copy.deepcopy(body)
                    if method == 'POST' and path == '/v1/runs':
                        from .host_budget_admission import HostBudgetAdmission
                        if not isinstance(direct._submission_gate, HostBudgetAdmission):
                            raise RuntimeBridgeError('existing_hard_budget_guard_required')
                        await direct._submission_gate.before_submit(
                            endpoint=direct.base_url, body=bounded_body)
                    status, content_type, raw = await asyncio.to_thread(direct._call,
                        method, path, body=bounded_body, timeout=timeout_seconds)
                    if 'json' not in content_type.lower():
                        raise RuntimeBridgeError('hermes_api_json_required')
                    return HermesHttpResponse(status, direct._json(raw))
                except RuntimeBridgeError:
                    raise HermesTransportError('protocol', 'Hermes transport contract failed') from None
        return RunsTransport()
