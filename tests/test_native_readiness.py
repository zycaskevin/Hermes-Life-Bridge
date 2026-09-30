"""Readiness is public liveness only: no credentials, model call or config load."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_life_bridge import cli
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.native_readiness import native_readiness
from hermes_life_bridge.hermes_direct_api import DirectHermesApi
from hermes_life_bridge.runtime_adapter import RuntimeBridgeError


@pytest.fixture
def health_server():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization') is not None))
            raw = b'{"status":"ok","private":"SHOULD_NOT_BE_PROJECTED"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_public_liveness_is_not_execution_readiness_or_authorization(health_server, monkeypatch):
    endpoint, calls = health_server
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    report = native_readiness(endpoint=endpoint)
    assert calls == [('/health', False)]
    assert report['apiLiveness'] == 'REACHABLE'
    assert not report['nativeExecutionReady'] and report['liveNativeRun'] == 'NOT_EXECUTED'
    assert report['residentRegistration'] == 'NOT_OBSERVED'
    assert report['contactPathChanged'] is False
    assert 'SHOULD_NOT_BE_PROJECTED' not in json.dumps(report)


def test_cli_readiness_does_not_load_environment_or_resident_secret_config(health_server, monkeypatch, capsys):
    endpoint, calls = health_server
    monkeypatch.setattr(BridgeConfig, 'from_env', staticmethod(lambda: pytest.fail('must not load environment')))
    code = cli.main(['native-readiness', '--endpoint', endpoint])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report['apiLiveness'] == 'REACHABLE'
    assert calls == [('/health', False)]


def test_offline_readiness_never_opens_a_socket(monkeypatch):
    from hermes_life_bridge import native_readiness as module
    monkeypatch.setattr(module.http.client, 'HTTPConnection', lambda *a, **k: pytest.fail('no socket'))
    result = native_readiness(endpoint='http://127.0.0.1:8642', probe=False)
    assert result['apiLiveness'] == 'NOT_CHECKED' and not result['nativeExecutionReady']


@pytest.mark.parametrize('endpoint', ['http://example.com', 'http://localhost:8642',
    'http://127.0.0.1:8642/path', 'http://user:password@127.0.0.1',
    'http://127.0.0.1?secret=PRIVATE_VALUE', 'https://127.0.0.1',
    'http://127.0.0.1:0', 'http://127.0.0.1:65536'])
def test_unapproved_endpoint_never_probed_or_exposed(endpoint, monkeypatch):
    from hermes_life_bridge import native_readiness as module
    monkeypatch.setattr(module.http.client, 'HTTPConnection', lambda *a, **k: pytest.fail('no socket'))
    result = native_readiness(endpoint=endpoint)
    assert result['apiLiveness'] == 'INVALID_ENDPOINT'
    assert endpoint not in json.dumps(result)


@pytest.mark.parametrize('value', ['{"x":1,"x":2}', '{"x":NaN}', '[]', 'not json'])
def test_direct_transport_rejects_ambiguous_json(value):
    with pytest.raises(RuntimeBridgeError, match='json_invalid'):
        DirectHermesApi._json(value)


def test_direct_transport_rejects_arbitrary_paths_before_io(monkeypatch):
    import http.client
    transport = DirectHermesApi(base_url='http://127.0.0.1:8642', auth_token='fixture-token')
    monkeypatch.setattr(http.client, 'HTTPConnection', lambda *a, **k: pytest.fail('no socket'))
    for method, path in [('POST', '/v1/runs/run_a/approval'), ('GET', '/etc/passwd'),
                         ('GET', '/v1/runs/../../secret'), ('POST', '/api/jobs')]:
        with pytest.raises(RuntimeBridgeError, match='path_denied'):
            transport._call(method, path)
    assert 'fixture-token' not in repr(transport)
