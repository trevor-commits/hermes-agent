"""Private host attachment must coexist with the gated dashboard auth boundary."""
import os
import socket
import threading
from dataclasses import replace
from urllib.parse import parse_qs
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from gateway import host_rendezvous as hr
from hermes_cli import web_server as web
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.native_http import CaptureNativeSocketPeer
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE
from tests.waiting import wait_until
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

TOKEN = "fixture-private-owner-token"


@pytest.fixture
def owner_app(monkeypatch):
    clear_providers()
    provider = StubAuthProvider()
    register_provider(provider)
    monkeypatch.setattr(web, '_SESSION_TOKEN', TOKEN)
    for key, value in {'auth_required': True, 'bound_host': '127.0.0.1', 'bound_port': 9119,
                       'trusted_public_hosts': frozenset(), 'host_role': hr.ROLE_SERVE,
                       'serves_spa': True, 'gateway_runner': None}.items():
        monkeypatch.setattr(web.app.state, key, value, raising=False)
    called = []
    original = web._require_token
    def require(request):
        called.append(request.url.path)
        return original(request)
    monkeypatch.setattr(web, '_require_token', require)
    yield SimpleNamespace(app=CaptureNativeSocketPeer(ProxyHeadersMiddleware(web.app, trusted_hosts='*')),
                          called=called, provider=provider)
    clear_providers()


def owner_headers():
    return {'X-Hermes-Token': TOKEN, 'Authorization': f'Bearer {TOKEN}'}


@pytest.mark.parametrize("peer", [("127.0.0.1", 41000), ("::1", 41000)])
def test_gated_host_identity_accepts_private_owner_credential(owner_app, peer):
    client = TestClient(owner_app.app, base_url='http://127.0.0.1:9119', client=peer)
    response = client.get(hr.HOST_IDENTITY_PATH, headers=owner_headers())
    assert response.status_code == 200, response.text
    assert response.json() == {'ok': True, 'protocolVersion': 1, 'pid': os.getpid(),
                               'role': hr.ROLE_SERVE, 'servesSpa': True}
    assert owner_app.called == [hr.HOST_IDENTITY_PATH]


def test_valid_owner_token_does_not_authorize_gated_config(owner_app):
    client = TestClient(owner_app.app, base_url='http://127.0.0.1:9119', client=('127.0.0.1', 41000))
    assert client.get('/api/config', headers=owner_headers()).status_code == 401
    assert owner_app.called == []


@pytest.mark.parametrize("request_headers,peer", [
    pytest.param({}, ('127.0.0.1', 41000), id='absent'),
    pytest.param({**owner_headers(), 'X-Hermes-Token': 'wrong-owner'}, ('127.0.0.1', 41000), id='wrong'),
    pytest.param([('X-Hermes-Token', TOKEN), ('X-Hermes-Token', TOKEN)], ('127.0.0.1', 41000), id='duplicate'),
    pytest.param({**owner_headers(), 'X-Hermes-Token': ''}, ('127.0.0.1', 41000), id='empty'),
    pytest.param({**owner_headers(), 'Origin': 'https://example.test'}, ('127.0.0.1', 41000), id='origin'),
    pytest.param({**owner_headers(), 'Origin': ''}, ('127.0.0.1', 41000), id='empty-origin'),
    pytest.param(owner_headers(), ('203.0.113.9', 41000), id='remote'),
    pytest.param({**owner_headers(), 'X-Forwarded-For': '127.0.0.1'}, ('203.0.113.9', 41000), id='forged-proxy'),
    pytest.param(owner_headers(), None, id='no-peer'),
    pytest.param({'Authorization': f'Bearer {TOKEN}'}, ('127.0.0.1', 41000), id='bare-bearer'),
    pytest.param({**owner_headers(), 'X-Hermes-Gateway-Ticket': 'bad-ticket'}, ('127.0.0.1', 41000), id='bad-native-ticket'),
])
def test_owner_identity_rejects_unproven_requests(owner_app, request_headers, peer):
    client = TestClient(owner_app.app, base_url='http://127.0.0.1:9119', client=peer)
    assert client.get(hr.HOST_IDENTITY_PATH, headers=request_headers).status_code == 401
    assert owner_app.called == []


@pytest.mark.parametrize("headers", [
    {'X-Hermes-Token': TOKEN},
    {**owner_headers(), 'X-Forwarded-For': '203.0.113.9'},
])
def test_local_owner_is_bound_to_actual_peer_not_forwarded_client(owner_app, headers):
    client = TestClient(owner_app.app, base_url='http://127.0.0.1:9119', client=('127.0.0.1', 41000))
    assert client.get(hr.HOST_IDENTITY_PATH, headers=headers).status_code == 200


@pytest.mark.parametrize("method,path", [
    ('GET', '/api/config?profile=other'), ('POST', '/api/host/identity'),
    ('GET', '/api/host/identity/'), ('GET', '/api/ssh/ownership'),
])
def test_host_identity_principal_cannot_authorize_other_routes(owner_app, method, path):
    client = TestClient(owner_app.app, base_url='http://127.0.0.1:9119', client=('127.0.0.1', 41000), follow_redirects=False)
    assert client.request(method, path, headers=owner_headers()).status_code == 401


def test_host_header_guard_still_rejects_native_owner_credential(owner_app):
    client = TestClient(owner_app.app, base_url='http://127.0.0.1:9119', client=('127.0.0.1', 41000))
    assert client.get(hr.HOST_IDENTITY_PATH, headers={**owner_headers(), 'Host': 'evil.example'}).status_code == 400
    assert owner_app.called == []


def test_existing_gated_session_still_authenticates_identity(owner_app):
    provider = owner_app.provider
    login = provider.start_login(redirect_uri='http://127.0.0.1/callback')
    pkce = parse_qs(login.cookie_payload['hermes_session_pkce'].replace(';', '&'))
    session = provider.complete_login(code='stub_code', state=pkce['state'][0],
                                      code_verifier=pkce['verifier'][0], redirect_uri='http://127.0.0.1/callback')
    client = TestClient(owner_app.app, base_url='http://127.0.0.1:9119', client=('203.0.113.9', 41000))
    for headers in ({'Authorization': 'Bearer ' + session.access_token},
                    {'Cookie': f'{SESSION_AT_COOKIE}={session.access_token}'}):
        assert client.get(hr.HOST_IDENTITY_PATH, headers=headers).status_code == 200


def test_native_probe_attaches_only_to_expected_pid_and_role(owner_app, monkeypatch):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    config, server = web._build_uvicorn_server('127.0.0.1', port)
    config.lifespan = 'off'
    config.load()
    config.loaded_app = CaptureNativeSocketPeer(config.loaded_app)
    thread = threading.Thread(target=lambda: web._run_serve(
        lambda: server.serve(sockets=[listener]), config, '127.0.0.1', port), daemon=True)
    record = hr.HostRecord(role=hr.ROLE_SERVE, pid=os.getpid(), create_time=hr.process_create_time(),
                           host='127.0.0.1', port=port, protocol_version=hr.HOST_PROTOCOL_VERSION,
                           token_fingerprint=hr.token_fingerprint(TOKEN), profiles=('default',), updated_at='fixture')
    monkeypatch.setattr(hr, 'read_token', lambda role: TOKEN)
    thread.start()
    try:
        wait_until(lambda: server.started, timeout=5, what='isolated owner listener startup')
        assert hr.probe_owner(record)['pid'] == os.getpid()
        assert hr.probe_owner(replace(record, pid=os.getpid() + 1)) is None
        assert hr.probe_owner(replace(record, role=hr.ROLE_DESKTOP_SERVE)) is None
        monkeypatch.setattr(hr, 'read_token', lambda role: 'stale-owner-token')
        assert hr.probe_owner(record) is None
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), 'isolated owner listener did not stop'



def test_standalone_startup_installs_peer_capture_before_proxy(owner_app, monkeypatch):
    from contextlib import nullcontext
    import httpx
    import uvicorn

    config = uvicorn.Config(web.app, host='127.0.0.1', port=9119, lifespan='off',
                            proxy_headers=True, forwarded_allow_ips='*')
    responses = []
    class Server:
        should_exit = False
        capture_signals = staticmethod(nullcontext)

        async def startup(self):
            transport = httpx.ASGITransport(app=config.loaded_app, client=('127.0.0.1', 41000))
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:9119') as client:
                responses.append(await client.get(hr.HOST_IDENTITY_PATH, headers={
                    **owner_headers(), 'X-Forwarded-For': '203.0.113.9'}))
            self.should_exit = True

    monkeypatch.setattr(web, '_build_uvicorn_server', lambda *a, **k: (config, Server()))
    monkeypatch.setattr(web, '_configure_auth_gate', lambda *a, **k: None)
    monkeypatch.setattr(web, '_port_bind_conflict', lambda *a: False)
    monkeypatch.setattr('hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive', lambda: None)
    monkeypatch.setattr('tui_gateway.server.install_exit_flush_signal_handlers', lambda: None)
    monkeypatch.setattr('tui_gateway.launch_profile_policy.activate_multi_profile_hosting_eagerly', lambda: None)
    web.start_server(open_browser=False)
    assert len(responses) == 1
    assert responses[0].status_code == 200, responses[0].text
