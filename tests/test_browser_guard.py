"""Other websites must not drive the local API (core.browser_guard).

A page the user visits can send form POSTs, no-cors fetches and WebSocket
handshakes to 127.0.0.1:3900, or rebind its own hostname onto it. These tests
pin the refusal paths and, just as importantly, that the first-party UI and
non-browser clients keep working.
"""
import os

os.environ.setdefault("OMNIVOICE_MODEL", "test")
os.environ.setdefault("OMNIVOICE_DISABLE_FILE_LOG", "1")

import pytest
from starlette.websockets import WebSocketDisconnect

LOOPBACK = ("127.0.0.1", 1)
EVIL = "https://evil.example"
PUBLIC_IP = "93.184.215.14"


def _client(base_url="http://testserver", client=LOOPBACK):
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app, base_url=base_url, client=client)


@pytest.fixture
def spawned(monkeypatch):
    """Record every subprocess the gallery route would start."""
    import api.routers.gallery as gallery

    calls = []

    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"stub failure"

    async def fake_spawn(*argv, **_kwargs):
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(gallery, "spawn_subprocess", fake_spawn)
    return calls


@pytest.fixture
def public_dns(monkeypatch):
    """Every host resolves to a public address; no real DNS in tests."""
    import socket

    from core import url_safety

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 0))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.delenv(url_safety.ALLOW_PRIVATE_ENV, raising=False)


_DOWNLOAD = "/gallery/download?video_url=https://media.example/v&character_name=x"


# ── Cross-site requests ──────────────────────────────────────────────────


def test_cross_site_form_post_is_refused_before_ytdlp(spawned, public_dns):
    response = _client().post(
        _DOWNLOAD,
        headers={
            "Origin": EVIL,
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert response.status_code == 403
    assert "another website" in response.json()["detail"]
    assert spawned == []


@pytest.mark.parametrize("origin", ["null", "http://localhost:8080", "chrome-extension://abc"])
def test_foreign_or_opaque_origins_are_refused(spawned, public_dns, origin):
    assert _client().post(_DOWNLOAD, headers={"Origin": origin}).status_code == 403
    assert spawned == []


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_fetch_metadata_without_origin_is_refused(spawned, public_dns, site):
    assert _client().post(_DOWNLOAD, headers={"Sec-Fetch-Site": site}).status_code == 403
    assert spawned == []


@pytest.mark.parametrize(
    "headers",
    [
        {},  # CLI, curl, MCP client, scripts
        {"Sec-Fetch-Site": "none"},  # Electron main-process net.fetch
        {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},  # served web UI
        {"Origin": "app://voicestudio", "Sec-Fetch-Site": "cross-site"},  # packaged renderer
        {"Origin": "http://localhost:3901"},  # browser dev UI
    ],
)
def test_first_party_and_non_browser_requests_pass(spawned, public_dns, headers):
    response = _client().post(_DOWNLOAD, headers=headers)
    # Reaches the route: the stubbed yt-dlp run fails with a 500.
    assert response.status_code == 500
    assert len(spawned) == 1


def test_configured_extra_origin_passes(spawned, public_dns, monkeypatch):
    monkeypatch.setenv("OMNIVOICE_ALLOWED_ORIGINS", "https://ui.example")
    assert _client().post(_DOWNLOAD, headers={"Origin": "https://ui.example"}).status_code == 500


def test_cross_site_post_cannot_enable_lan_sharing(monkeypatch):
    from services import network_share

    called = []

    async def fake_enable(app):
        called.append(app)
        return network_share.ShareState()

    monkeypatch.setattr(network_share, "enable", fake_enable)
    response = _client().post(
        "/system/network/enable",
        headers={"Origin": EVIL, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 403
    assert called == []
    assert _client().post("/system/network/enable").status_code == 200
    assert len(called) == 1


def test_cross_site_reads_do_not_get_cors_access_to_the_pin():
    response = _client().get("/system/network/state", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in response.headers


def test_side_effectful_get_refuses_cross_site_but_navigation_to_the_shell_works():
    client = _client()
    blocked = client.get("/setup/preflight", headers={"Sec-Fetch-Site": "cross-site"})
    assert blocked.status_code == 403
    assert client.get("/health", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_side_effectful_get_routes_carry_the_cross_site_dependency():
    from main import app

    from core.browser_guard import reject_cross_site_get

    expected = {
        "/history",
        "/dub/transcribe-stream/{job_id}",
        "/dub/download/{job_id}",
        "/dub/download/{job_id}/{filename}",
        "/dub/download-audio/{job_id}",
        "/dub/download-audio/{job_id}/{filename}",
        "/dub/download-mp3/{job_id}",
        "/dub/download-mp3/{job_id}/{filename}",
        "/dub/preview-video/{job_id}",
        "/dub/onsets/{job_id}",
        "/archetypes/{archetype_id}/preview",
        "/community/manifest",
        "/community/items",
        "/community/items/{item_id}/preview",
        "/setup/preflight",
        "/models/access/status",
    }
    guarded = set()
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        stack = list(dependant.dependencies)
        while stack:
            dep = stack.pop()
            if dep.call is reject_cross_site_get:
                guarded.add(route.path)
            stack.extend(dep.dependencies)
    assert expected <= guarded


def test_websocket_from_another_site_is_refused():
    with pytest.raises(WebSocketDisconnect):
        with _client().websocket_connect("/ws/events", headers={"Origin": EVIL}) as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with _client().websocket_connect("/ws/tts", headers={"Origin": "null"}) as ws:
            ws.receive_text()


@pytest.mark.parametrize("headers", [{}, {"Origin": "app://voicestudio"}, {"Origin": "http://testserver"}])
def test_websocket_from_the_app_or_a_non_browser_client_connects(headers):
    with _client().websocket_connect("/ws/tts", headers=headers) as ws:
        ws.send_json({})
        assert ws.receive_json()["type"] == "error"


# ── DNS rebinding (Host allowlist) ───────────────────────────────────────


@pytest.fixture
def default_hosts(monkeypatch):
    for name in (
        "OMNIVOICE_ALLOWED_HOSTS",
        "OMNIVOICE_ALLOWED_ORIGINS",
        "OMNIVOICE_MCP_ALLOWED_HOSTS",
        "OMNIVOICE_API_URL",
        "OMNIVOICE_PUBLIC_API_BASE",
        "OMNIVOICE_BIND_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_rebound_hostname_is_refused_even_when_same_origin(default_hosts, monkeypatch):
    from services import network_share

    called = []

    async def fake_enable(app):
        called.append(app)
        return network_share.ShareState()

    monkeypatch.setattr(network_share, "enable", fake_enable)
    client = _client("http://evil.example:3900")
    response = client.post(
        "/system/network/enable", headers={"Origin": "http://evil.example:3900"}
    )
    assert response.status_code == 403
    assert "OMNIVOICE_ALLOWED_HOSTS" in response.json()["detail"]
    assert called == []
    # The public shell still loads so a remote UI can sign in and explain.
    assert client.get("/health").status_code in (200, 503)
    # The PIN-revealing read is refused too, as is a WebSocket.
    assert client.get("/system/network/state").status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_text()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:3900",
        "http://127.0.0.1:3900",
        "http://192.168.1.20:3901",  # LAN share by address
        "http://app.localhost:3900",
        "http://gpu-box.tail1234.ts.net",  # tailscale serve
    ],
)
def test_default_host_names_pass(default_hosts, base_url):
    assert _client(base_url).get("/system/network/state").status_code == 200


def test_host_values_parse_strictly(default_hosts):
    from core.browser_guard import host_allowed

    assert host_allowed("[::1]:3900")
    assert host_allowed("[::ffff:127.0.0.1]:3900")
    assert host_allowed("LOCALHOST.:3900")
    assert host_allowed(None)  # no Host header: not a browser
    for value in ("evil.example", "localhost.evil.example", "127.0.0.1@evil.example", "a b"):
        assert not host_allowed(value), value


def test_configured_host_names_pass(default_hosts, monkeypatch):
    monkeypatch.setenv("OMNIVOICE_ALLOWED_HOSTS", "media.lan,.home.example")
    assert _client("http://media.lan:3900").get("/system/network/state").status_code == 200
    assert _client("http://nas.home.example").get("/system/network/state").status_code == 200
    assert _client("http://other.example").get("/system/network/state").status_code == 403


def test_credentialed_remote_clients_are_not_host_checked(default_hosts, monkeypatch):
    from services.admin_sessions import admin_session_store

    admin_session_store.clear()
    monkeypatch.setenv("OMNIVOICE_API_KEY", "s3cret-key")
    client = _client("http://gpu.example.com", client=("10.0.0.5", 1))
    response = client.get(
        "/v1/audio/voices", headers={"Authorization": "Bearer s3cret-key"}
    )
    assert response.status_code == 200
    # Without a credential the API-key gate answers, so the UI can prompt.
    assert client.get("/v1/audio/voices").status_code == 401
    # A rebinding page reaches the backend over loopback; a made-up
    # credential must not lift the host check there.
    rebound = _client("http://evil.example:3900")
    assert rebound.get(
        "/system/network/state", headers={"Authorization": "Bearer guess"}
    ).status_code == 403
    admin_session_store.clear()
