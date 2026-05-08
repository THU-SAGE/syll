"""Phase 1a auth tests: admin token, loopback gate, Origin/CORS."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from syll.web import auth as auth_module
from syll.web.app import create_app
from tests.test_app_factory import _make_agent_loop, _make_config

# ── Token storage isolation ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_token(monkeypatch, tmp_path):
    """Redirect ~/.syll/admin_token to a tmpdir so tests don't touch the user's real token."""
    tok = tmp_path / "admin_token"
    monkeypatch.setattr(auth_module, "ADMIN_TOKEN_PATH", tok)
    yield


# ── ipaddress-based loopback gate (rev. 5 R3) ─────────────────────────────


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("::1", True),
    ("::ffff:127.0.0.1", True),  # IPv4-mapped IPv6 loopback
    ("localhost", True),
    ("192.168.1.5", False),
    ("10.0.0.1", False),
    ("8.8.8.8", False),
    ("", False),
    (None, False),
    ("not-an-ip", False),
])
def test_is_loopback_cases(host, expected):
    assert auth_module._is_loopback(host) is expected


# ── App fixture with custom gateway ───────────────────────────────────────


def _make_app(*, allow_remote_admin: bool = False, allow_origins: list[str] | None = None):
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_config(tmp)
    cfg.gateway.allow_remote_admin = allow_remote_admin
    cfg.gateway.allow_origins = allow_origins or []
    app = create_app(
        config=cfg,
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )
    # Add a stub mutating route guarded by require_admin so we can probe the gate.
    probe_router = APIRouter()

    @probe_router.post("/_probe", dependencies=[Depends(auth_module.require_admin)])
    async def probe():
        return {"ok": True}

    app.include_router(probe_router, prefix="/api/v1")
    return app


# ── Admin token routes ────────────────────────────────────────────────────


def test_admin_token_initialized_on_lifespan():
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get(
            "/api/v1/admin-token",
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"].startswith("syll-admin-")


def test_admin_token_loopback_only_even_with_remote_admin_enabled():
    """`/admin-token` is ALWAYS loopback-only — see auth.py docstring."""
    app = _make_app(allow_remote_admin=True, allow_origins=["http://example.com"])
    with TestClient(app, client=("203.0.113.5", 12345)) as client:
        r = client.get(
            "/api/v1/admin-token",
            headers={"Origin": "http://example.com"},
        )
        assert r.status_code == 403
        assert "loopback" in r.json()["detail"].lower()


# ── require_admin gate ────────────────────────────────────────────────────


def test_mutating_requires_token():
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.post(
            "/api/v1/_probe",
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 401


def test_mutating_token_ok_on_loopback():
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        token = client.get(
            "/api/v1/admin-token",
            headers={"Origin": "http://localhost:18790"},
        ).json()["token"]
        r = client.post(
            "/api/v1/_probe",
            headers={
                "X-Syll-Admin-Token": token,
                "Origin": "http://localhost:18790",
            },
        )
        assert r.status_code == 200


def test_mutating_remote_denied_when_remote_admin_off():
    """Even with the right token, a non-loopback caller is rejected unless
    gateway.allow_remote_admin is on."""
    app = _make_app(allow_remote_admin=False)
    # Write a token to disk so the token check passes; the loopback gate is
    # the layer we're isolating here.
    auth_module.boot_admin_token()
    token = auth_module.get_admin_token()
    with TestClient(app, client=("203.0.113.5", 12345)) as client:
        # The remote caller's Origin must be in allow_origins to even reach
        # the loopback gate; we configure it so we cleanly observe the
        # remote-admin denial rather than a CORS-precondition denial.
        cfg = app.state.config
        cfg.gateway.allow_origins = ["http://example.com"]
        r = client.post(
            "/api/v1/_probe",
            headers={
                "X-Syll-Admin-Token": token,
                "Origin": "http://example.com",
            },
        )
        assert r.status_code == 403
        assert "remote admin disabled" in r.json()["detail"]


def test_mutating_remote_allowed_when_explicitly_enabled():
    app = _make_app(
        allow_remote_admin=True,
        allow_origins=["http://example.com"],
    )
    auth_module.boot_admin_token()
    token = auth_module.get_admin_token()
    with TestClient(app, client=("203.0.113.5", 12345)) as client:
        r = client.post(
            "/api/v1/_probe",
            headers={
                "X-Syll-Admin-Token": token,
                "Origin": "http://example.com",
            },
        )
        assert r.status_code == 200


def test_mutating_remote_denied_with_unallowed_origin():
    app = _make_app(allow_remote_admin=True, allow_origins=["http://example.com"])
    auth_module.boot_admin_token()
    token = auth_module.get_admin_token()
    with TestClient(app, client=("203.0.113.5", 12345)) as client:
        r = client.post(
            "/api/v1/_probe",
            headers={
                "X-Syll-Admin-Token": token,
                "Origin": "http://attacker.example",
            },
        )
        assert r.status_code == 403


def test_origin_required_for_mutating_no_origin():
    app = _make_app()
    auth_module.boot_admin_token()
    token = auth_module.get_admin_token()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.post(
            "/api/v1/_probe",
            headers={"X-Syll-Admin-Token": token},  # no Origin
        )
        assert r.status_code == 403
        assert "origin" in r.json()["detail"].lower()


def test_admin_token_get_with_sec_fetch_site_no_origin():
    """Sec-Fetch-Site: same-origin should substitute for Origin on GETs."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get(
            "/api/v1/admin-token",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert r.status_code == 200


# ── CORS tightening ───────────────────────────────────────────────────────


def test_cors_preflight_allowed_for_local_origin():
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.options(
            "/api/v1/admin-token",
            headers={
                "Origin": "http://127.0.0.1:18790",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Allowed origin should yield CORS headers
        assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:18790"


def test_cors_preflight_denied_for_foreign_origin():
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.options(
            "/api/v1/admin-token",
            headers={
                "Origin": "http://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Foreign origin must NOT receive CORS approval
        assert r.headers.get("access-control-allow-origin") != "http://attacker.example"


# ── Token rotation ────────────────────────────────────────────────────────


def test_rotate_admin_token_replaces_value():
    t1 = auth_module.boot_admin_token()
    t2 = auth_module.rotate_admin_token()
    assert t1 != t2
    assert auth_module.get_admin_token() == t2


def test_boot_admin_token_idempotent_without_force():
    """Review-pass-2: boot_admin_token() must not churn the token on every
    `syll web` lifespan re-entry (would invalidate any running Pet UI's
    cached value). Only `force=True` rotates."""
    t1 = auth_module.boot_admin_token()
    t2 = auth_module.boot_admin_token()
    t3 = auth_module.boot_admin_token(force=False)
    assert t1 == t2 == t3
    t4 = auth_module.boot_admin_token(force=True)
    assert t4 != t1


# ── /api/v1/config admin gate (review pass 2) ───────────────────────────


def test_put_config_requires_admin_token():
    """PUT /api/v1/config must reject anonymous mutations even from loopback —
    rev. 4 finding C2 (config bypass) was the door MCP would walk through."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.put(
            "/api/v1/config",
            json={"identity": {"user_name": "evil"}},
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 401, r.text


def test_put_config_succeeds_with_admin_token(monkeypatch):
    """And admits authorized callers — sanity that the gate didn't lock everyone out.

    Review pass 3: this previously accepted 422 as "gate passed". Strengthened
    to demand 200 AND that save_config received our intended mutation.
    """
    saved_to: dict = {}

    def fake_save(cfg):
        saved_to["cfg"] = cfg

    # routes/config.py imports save_config by-name, patch there.
    from syll.web.routes import config as config_routes
    monkeypatch.setattr(config_routes, "save_config", fake_save)

    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update({"Origin": "http://localhost:18790"})
        token = client.get("/api/v1/admin-token").json()["token"]
        client.headers["X-Syll-Admin-Token"] = token
        # Send a minimal partial payload — the route deep-merges with the
        # current persisted config, so this is a focused identity edit.
        r = client.put(
            "/api/v1/config",
            json={"identity": {"user_name": "phase1a-test-user"}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        # save_config must have been called with the merged Config containing
        # our edit.
        assert saved_to.get("cfg") is not None, "save_config was not called"
        assert saved_to["cfg"].identity.user_name == "phase1a-test-user"


def test_put_identity_requires_admin_token():
    """PUT /api/v1/identity is the partial-payload identity endpoint —
    should also be gated."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.put(
            "/api/v1/identity",
            json={"user_name": "evil"},
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 401


# ── AdminGuardMiddleware: every unsafe method on /api/v1/* is gated ──────


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/cron/jobs"),
    ("POST", "/api/v1/skills"),
    ("PUT", "/api/v1/config"),
    ("PUT", "/api/v1/identity"),
    ("DELETE", "/api/v1/sessions/foo"),
    ("PATCH", "/api/v1/cron/jobs/abc"),
    ("POST", "/api/v1/intent/clarify"),
    ("POST", "/api/v1/voice/asr"),
    ("POST", "/api/v1/recorder/start"),
    ("POST", "/api/v1/recorder/stop"),
    ("PUT", "/api/v1/ghost/config"),
    ("POST", "/api/v1/syll/svgs"),
    ("POST", "/api/v1/rituals/install"),
    ("POST", "/api/v1/coord/transform"),
])
def test_unsafe_methods_globally_require_admin_token(method, path):
    """Review pass 3 (Critical): the AdminGuardMiddleware is the source of
    truth for mutation auth, not per-route Depends — every POST/PUT/PATCH/
    DELETE on /api/v1/* must reject unauthenticated callers, even those that
    pre-date Phase 1a and never grew a Depends(require_admin)."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.request(
            method,
            path,
            json={"_": 0},
            headers={"Origin": "http://localhost:18790"},
        )
        # 401 = no token; 403 = had token but other gate failed; either is a
        # rejection. We must NEVER see 200 / 422 / 404 / etc., which would
        # indicate the request reached the route handler.
        assert r.status_code in (401, 403), (
            f"{method} {path} returned {r.status_code} — expected the "
            f"AdminGuardMiddleware to reject. Body: {r.text}"
        )


def test_safe_methods_pass_through_without_token():
    """The middleware MUST NOT gate read endpoints — that would break the
    Pet UI's first-paint loads (loadStatus, loadConfig, loadSessions, etc.)."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        # /api/v1/config is a public read. No token, no Origin header.
        r = client.get("/api/v1/config")
        # 200 = handler succeeded; 5xx = test-stub-incomplete service. The
        # one we must NEVER see is 401 (token rejection on a safe method).
        assert r.status_code != 401, (
            f"AdminGuardMiddleware mistakenly gated GET /api/v1/config "
            f"(returned {r.status_code})"
        )


# ── Review pass 4: AdminGuard rejections must carry security headers ────


def test_admin_guard_401_response_carries_csp_and_cors_headers():
    """A 401 from AdminGuard must still flow through CSP and CORS so the
    client browser sees Content-Security-Policy / Access-Control-* headers
    on the rejection. Earlier order put AdminGuard outermost, leaking
    unhardened 401s to the browser."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.post(
            "/api/v1/cron/jobs",
            json={"_": 0},
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 401, r.text
        # CSP must be present.
        assert "Content-Security-Policy" in r.headers, list(r.headers.keys())
        assert "script-src" in r.headers["Content-Security-Policy"]
        # Frame-options + referrer-policy from CSP middleware.
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("Referrer-Policy") == "no-referrer"
        # CORS Access-Control-Allow-Origin echoed for the local origin.
        assert (
            r.headers.get("access-control-allow-origin")
            == "http://localhost:18790"
        )


# ── Review pass 4: WebSocket loopback Origin gate ────────────────────────


def test_chat_ws_rejects_foreign_origin_even_on_loopback():
    """A malicious page at http://attacker.example can resolve `127.0.0.1`
    and open a WebSocket to ws://127.0.0.1:<port>/api/v1/chat/ws from the
    user's browser. `client.host` is loopback (it's the local browser
    process), so the loopback short-circuit alone is bypass-able. Origin
    must be checked even on loopback."""
    from starlette.websockets import WebSocketDisconnect

    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/v1/chat/ws",
                headers={"Origin": "http://attacker.example"},
            ):
                pass


def test_chat_ws_accepts_loopback_with_local_origin():
    """The Pet UI opens the chat WS from same-origin; this must still work."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        with client.websocket_connect(
            "/api/v1/chat/ws",
            headers={"Origin": "http://localhost:18790"},
        ):
            pass  # connection accepted = pass


def test_chat_ws_accepts_loopback_without_origin():
    """Native CLI tools (wscat, websocat, etc.) don't send an Origin header.
    Loopback callers without Origin are allowed — they're invoked by the
    user explicitly, and the same-host code-execution model already gives
    those tools direct access to ~/.syll/admin_token if they wanted it."""
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        # Note: TestClient may inject a default Origin; explicitly clear it.
        with client.websocket_connect("/api/v1/chat/ws") as ws:
            pass
        del ws


def test_chat_ws_remote_caller_blocked_without_remote_admin():
    """Non-loopback callers are rejected unless allow_remote_admin is on."""
    from starlette.websockets import WebSocketDisconnect

    app = _make_app(allow_remote_admin=False)
    with TestClient(app, client=("203.0.113.5", 12345)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/v1/chat/ws",
                headers={"Origin": "http://attacker.example"},
            ):
                pass


def test_chat_ws_loopback_nonlocal_origin_requires_token_and_remote_admin():
    """gateway.allow_origins is a CORS allowlist, not tokenless WS auth.

    A browser page hosted at an allowlisted-but-nonlocal Origin can still be
    driven by code outside Syll. Even though the TCP peer is loopback, it must
    take the explicit remote-admin path: allow_remote_admin + token.
    """
    from starlette.websockets import WebSocketDisconnect

    app = _make_app(
        allow_remote_admin=False,
        allow_origins=["http://trusted.example"],
    )
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/v1/chat/ws",
                headers={"Origin": "http://trusted.example"},
            ):
                pass


def test_chat_ws_loopback_nonlocal_origin_allowed_with_remote_admin_and_token():
    """The same nonlocal Origin is allowed only with explicit remote-admin
    opt-in and a valid token."""
    app = _make_app(
        allow_remote_admin=True,
        allow_origins=["http://trusted.example"],
    )
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        token = client.get(
            "/api/v1/admin-token",
            headers={"Origin": "http://localhost:18790"},
        ).json()["token"]
        with client.websocket_connect(
            f"/api/v1/chat/ws?token={token}",
            headers={"Origin": "http://trusted.example"},
        ):
            pass


def test_chat_ws_remote_requires_origin_even_with_token():
    """Non-loopback WS must provide both a valid token and an allowlisted
    Origin; token-only remote CLI/browser clients do not bypass the allowlist."""
    from starlette.websockets import WebSocketDisconnect

    app = _make_app(
        allow_remote_admin=True,
        allow_origins=["http://trusted.example"],
    )
    auth_module.boot_admin_token()
    token = auth_module.get_admin_token()
    with TestClient(app, client=("203.0.113.5", 12345)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/v1/chat/ws?token={token}"):
                pass


def test_chat_ws_remote_accepts_allowed_origin_with_token():
    """Positive remote-admin WS path: explicit opt-in, token, and allowlisted
    Origin all present."""
    app = _make_app(
        allow_remote_admin=True,
        allow_origins=["http://trusted.example"],
    )
    auth_module.boot_admin_token()
    token = auth_module.get_admin_token()
    with TestClient(app, client=("203.0.113.5", 12345)) as client:
        with client.websocket_connect(
            f"/api/v1/chat/ws?token={token}",
            headers={"Origin": "http://trusted.example"},
        ):
            pass


# ── IPv4-mapped IPv6 loopback (review pass 2: version-stable) ────────────


def test_ipv4_mapped_ipv6_loopback_unwrap():
    """`::ffff:127.0.0.1` must report as loopback regardless of Python minor
    version. Earlier 3.11/3.12 don't classify it as loopback through plain
    `is_loopback` — we explicitly unwrap `ipv4_mapped` first."""
    assert auth_module._is_loopback("::ffff:127.0.0.1") is True
    # Counterexample: a non-loopback IPv4 mapped onto IPv6 must NOT pass.
    assert auth_module._is_loopback("::ffff:192.168.1.5") is False
    assert auth_module._is_loopback("::ffff:8.8.8.8") is False


# ── Custom port plumbing (review pass 2: --port / --host) ───────────────


def test_csp_and_cors_track_custom_gateway_port():
    """When `syll web --port 9999` runs, config.gateway.port must be mutated
    BEFORE create_app so CSP/CORS allow http://localhost:9999.

    We simulate the post-override state by passing a config with port=9999
    and assert both layers reflect it."""
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_config(tmp)
    cfg.gateway.port = 9999  # the wake/web command does this before create_app
    app = create_app(
        config=cfg,
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        # CORS preflight from the actual served origin must succeed.
        r = client.options(
            "/api/v1/admin-token",
            headers={
                "Origin": "http://localhost:9999",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-origin") == "http://localhost:9999"
        # CSP connect-src must include the matching ws:// for the custom port.
        body = client.get("/")
        csp = body.headers["Content-Security-Policy"]
        assert "ws://localhost:9999" in csp
        assert "ws://127.0.0.1:9999" in csp
        # And NOT the default port (would indicate hardcoding).
        assert "ws://localhost:18790" not in csp
