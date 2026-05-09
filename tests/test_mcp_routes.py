"""Phase 3: HTTP routes for MCP CRUD + test + reconnect.

Uses the echo stdio fixture so connect/list_tools succeed end-to-end. The
admin-token gate (`AdminGuardMiddleware`) wraps every POST/PUT/DELETE on
`/api/v1/*` — tests grab the token from `_admin_headers` once and reuse.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from syll.agent.mcp import MCPManager, command_hash
from syll.config.schema import (
    MCPConfig,
    MCPServerConfig,
    MCPStdioParams,
)
from syll.web import auth as auth_module
from syll.web.app import create_app
from tests.test_app_factory import _admin_headers, _make_config


@pytest.fixture(autouse=True)
def _isolate_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_module, "ADMIN_TOKEN_PATH", tmp_path / "admin_token")


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _echo_dump_dict() -> dict:
    """Echo-server PUT body, structured the way the UI sends it."""
    return {
        "transport": "stdio",
        "stdio": {
            "command": sys.executable,
            "args": ["-m", "tests.fixtures.echo_mcp_server"],
            "env": {},
        },
        "enabled": True,
        "enabled_tools": ["*"],
        "propagate_to_subagents": True,
    }


def _make_loop_with_mcp(mcp_manager):
    """Minimal AgentLoop wired to mcp_manager (matches test_mcp_loop_integration)."""
    from syll.agent.loop import AgentLoop
    from syll.bus.queue import MessageBus

    class _StubProvider:
        def get_default_model(self):
            return "stub"

    with patch("syll.agent.loop.ContextBuilder") as ctx_cls, \
         patch("syll.agent.loop.SessionManager") as sess_cls, \
         patch("syll.agent.loop.EventStore") as evt_cls:
        ctx_cls.return_value = SimpleNamespace(
            identity=None, skills=SimpleNamespace(), memory=SimpleNamespace()
        )
        sess_cls.return_value = SimpleNamespace()
        evt_cls.return_value = SimpleNamespace()
        return AgentLoop(
            bus=MessageBus(),
            provider=_StubProvider(),
            workspace=Path(tempfile.mkdtemp()),
            mcp_manager=mcp_manager,
        )


def _build_app(mcp_cfg: MCPConfig | None = None):
    cfg = _make_config(Path(tempfile.mkdtemp()))
    cfg.mcp = mcp_cfg or MCPConfig()
    mgr = MCPManager(
        cfg.mcp,
        workspace_path=cfg.workspace_path,
        restrict_to_workspace=False,
    )
    agent_loop = _make_loop_with_mcp(mgr)
    app = create_app(
        config=cfg, agent_loop=agent_loop,
        session_manager=SimpleNamespace(), skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(), cron_service=None,
    )
    app.state.mcp_manager = mgr
    return app, cfg, mgr


# ── GET /api/v1/mcp ─────────────────────────────────────────────────────


def test_get_mcp_returns_empty_state(monkeypatch):
    """Empty config: enabled=true but no servers.

    `GET /api/v1/mcp` calls `load_config()` (reads ~/.syll/config.json on
    disk). The user's real config may have entries from manual UI use, so
    we monkeypatch load_config to return a fresh, empty Config for this
    test only."""
    from syll.config.schema import Config

    fresh = Config()
    monkeypatch.setattr("syll.web.routes.mcp.load_config", lambda: fresh)
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get("/api/v1/mcp")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["servers"] == {}


# ── PUT /api/v1/mcp/servers/{name} — consent flow ───────────────────────


def test_put_stdio_server_without_hash_returns_409_with_preview(
    repo_root, monkeypatch
):
    """Enabling a stdio server without confirmed_command_hash → 409 with the
    expected hash and a human-readable command preview."""
    monkeypatch.chdir(repo_root)
    app, cfg, mgr = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = _echo_dump_dict()
        r = client.put("/api/v1/mcp/servers/echo", json=body)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "confirmation_required"
        assert detail["required_command_hash"].startswith("sha256:")
        # Preview shows the command (no env values, but echo has none anyway).
        preview = detail["effective_command_preview"]
        assert sys.executable in preview
        # No persistence happened.
        from syll.config.loader import load_config
        cfg_now = load_config()
        assert "echo" not in cfg_now.mcp.servers


def test_put_stdio_server_with_correct_hash_persists_and_reloads(
    repo_root, monkeypatch
):
    """Happy path: PUT with the right hash → 200 + tools registered on the loop."""
    monkeypatch.chdir(repo_root)
    app, _, mgr = _build_app()

    saved_to: dict = {}

    def fake_save(c, p=None):
        saved_to["cfg"] = c

    from syll.web.routes import mcp as mcp_routes
    monkeypatch.setattr(mcp_routes, "save_config", fake_save)

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = _echo_dump_dict()
        # Compute the consent hash exactly the way the server will.
        body["confirmed_command_hash"] = command_hash(
            MCPServerConfig.model_validate(body)
        )
        r = client.put("/api/v1/mcp/servers/echo", json=body)
        assert r.status_code == 200, r.text

        ret = r.json()
        assert ret["ok"] is True
        assert ret["server"]["status"] == "connected"
        assert "echo" in ret["server"]["available_tools"]
        # Persisted via save_config (fake_save captured it).
        assert "echo" in saved_to["cfg"].mcp.servers
        # Live manager has it.
        assert "echo" in mgr._sessions
        # Agent loop registry has the namespaced tools.
        agent_loop = app.state.agent_loop
        assert "mcp__echo__echo" in agent_loop._mcp_owned


def test_put_disabled_stdio_does_not_require_hash(repo_root, monkeypatch):
    """Saving a stdio server with enabled=false should NOT trigger 409.

    A user might want to add a server entry but leave it off until they
    test it manually. Hash consent only kicks in when actually enabling."""
    monkeypatch.chdir(repo_root)
    app, _, _ = _build_app()
    monkeypatch.setattr("syll.web.routes.mcp.save_config", lambda c, p=None: None)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = _echo_dump_dict()
        body["enabled"] = False
        r = client.put("/api/v1/mcp/servers/echo", json=body)
        assert r.status_code == 200, r.text


def test_put_preserves_metadata_fields_on_enable_disable(monkeypatch):
    """One-click enable/disable PUT bodies must not reset metadata fields."""
    from syll.config.schema import Config

    cfg_disk = Config()
    body = _echo_dump_dict()
    body.update({
        "enabled": False,
        "description": "custom playwright server",
        "tool_timeout_seconds": 123,
    })
    cfg_disk.mcp.servers["echo"] = MCPServerConfig.model_validate(body)
    app, _, _ = _build_app(mcp_cfg=cfg_disk.mcp)
    state = {"cfg": cfg_disk}

    monkeypatch.setattr("syll.web.routes.mcp.load_config", lambda: state["cfg"])
    monkeypatch.setattr(
        "syll.web.routes.mcp.save_config",
        lambda c, p=None: state.update(cfg=c),
    )

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        # Simulates the UI's one-click Disable body: full server payload with
        # enabled=false. This must retain metadata instead of falling back to
        # Pydantic defaults.
        disabled = {
            "transport": "stdio",
            "stdio": body["stdio"],
            "enabled": False,
            "enabled_tools": ["*"],
            "propagate_to_subagents": True,
            "description": "custom playwright server",
            "tool_timeout_seconds": 123,
        }
        r = client.put("/api/v1/mcp/servers/echo", json=disabled)
        assert r.status_code == 200, r.text
        saved = state["cfg"].mcp.servers["echo"]
        assert saved.description == "custom playwright server"
        assert saved.tool_timeout_seconds == 123


# ── PUT — invalid name ──────────────────────────────────────────────────


def test_put_rejects_invalid_server_name():
    """Server name regex: ^[a-z][a-z0-9_]{0,30}$"""
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = _echo_dump_dict()
        body["enabled"] = False
        for bad in ("Echo", "1echo", "echo-server", "echo$"):
            r = client.put(f"/api/v1/mcp/servers/{bad}", json=body)
            assert r.status_code == 400, f"expected 400 for {bad!r}, got {r.status_code}"


# ── PUT — apply_server failure → 502, no save ──────────────────────────


def test_put_502_on_connection_failure_does_not_persist(monkeypatch):
    """A bogus stdio command must result in 502 AND no save_config call."""
    app, _, _ = _build_app()

    save_calls: list = []
    monkeypatch.setattr(
        "syll.web.routes.mcp.save_config",
        lambda c, p=None: save_calls.append(c),
    )

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        bogus = {
            "transport": "stdio",
            "stdio": {
                "command": "/nonexistent-binary-syll-test",
                "args": [],
            },
            "enabled": True,
        }
        bogus["confirmed_command_hash"] = command_hash(
            MCPServerConfig.model_validate(bogus)
        )
        r = client.put("/api/v1/mcp/servers/dud", json=bogus)
        assert r.status_code == 502, r.text
        # save_config NEVER called.
        assert save_calls == [], "broken server was persisted!"


# ── DELETE ──────────────────────────────────────────────────────────────


def test_delete_unknown_returns_404():
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.delete("/api/v1/mcp/servers/missing")
        assert r.status_code == 404


def test_delete_removes_server_and_clears_owned_names(repo_root, monkeypatch):
    """End-to-end: PUT → DELETE → registry empty + manager has no session."""
    monkeypatch.chdir(repo_root)
    app, _, mgr = _build_app()

    # Stub save_config so we don't touch the real ~/.syll/config.json.
    state = {"cfg": None}
    monkeypatch.setattr(
        "syll.web.routes.mcp.save_config",
        lambda c, p=None: state.update(cfg=c),
    )

    # Seed the on-disk config so DELETE finds the entry.
    from syll.config.loader import load_config

    real_load = load_config
    def fake_load_config():
        cfg = real_load()
        if state["cfg"] is not None:
            cfg.mcp = state["cfg"].mcp
        return cfg

    monkeypatch.setattr("syll.web.routes.mcp.load_config", fake_load_config)

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = _echo_dump_dict()
        body["confirmed_command_hash"] = command_hash(
            MCPServerConfig.model_validate(body)
        )
        r = client.put("/api/v1/mcp/servers/echo", json=body)
        assert r.status_code == 200
        agent_loop = app.state.agent_loop
        assert "mcp__echo__echo" in agent_loop._mcp_owned

        r = client.delete("/api/v1/mcp/servers/echo")
        assert r.status_code == 200
        assert "echo" not in mgr._sessions
        assert agent_loop._mcp_owned == set()


# ── POST /api/v1/mcp/_test ──────────────────────────────────────────────


def test_test_endpoint_probes_unsaved_config(repo_root, monkeypatch):
    """Test connection: 200 + ok=True for a working config; never persists.

    Phase 3 review-pass-6 H3: stdio probes now require the same consent hash
    as PUTs — sending it explicitly here mirrors the UI's confirm-modal flow.
    """
    monkeypatch.chdir(repo_root)
    app, _, mgr = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = _echo_dump_dict()
        body["confirmed_command_hash"] = command_hash(MCPServerConfig.model_validate(body))
        r = client.post("/api/v1/mcp/_test", json=body)
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["ok"] is True
        assert result["tool_count"] == 2
        assert set(result["tools"]) == {"echo", "add"}
        # Manager state untouched.
        assert mgr._sessions == {}


def test_test_endpoint_returns_error_on_bad_command():
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        bogus = {
            "transport": "stdio",
            "stdio": {"command": "/nonexistent-binary-syll-test"},
            "enabled": True,
        }
        bogus["confirmed_command_hash"] = command_hash(MCPServerConfig.model_validate(bogus))
        r = client.post("/api/v1/mcp/_test", json=bogus)
        assert r.status_code == 200  # endpoint succeeds
        result = r.json()
        assert result["ok"] is False
        assert "error" in result


# ── GET masks env / headers ─────────────────────────────────────────────


def test_get_masks_env_values_in_response(repo_root, monkeypatch):
    """Persisted env values must be masked in GET responses.

    The route reads via `load_config()` which hits ~/.syll/config.json on
    disk; we monkeypatch it to return a config with our test server."""
    monkeypatch.chdir(repo_root)
    cfg_disk = MCPConfig(
        servers={
            "fs": MCPServerConfig(
                transport="stdio",
                stdio=MCPStdioParams(
                    command="echo",
                    env={"OPENAI_API_KEY": "sk-fakelongkey1234567890"},
                ),
                enabled=False,
            )
        }
    )
    app, cfg, _ = _build_app(mcp_cfg=cfg_disk)
    cfg.mcp = cfg_disk

    def _fake_load():
        return cfg

    monkeypatch.setattr("syll.web.routes.mcp.load_config", _fake_load)

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get("/api/v1/mcp")
        assert r.status_code == 200
        env = r.json()["servers"]["fs"]["stdio"]["env"]
        # Last 4 chars retained, the rest masked behind "...".
        assert env["OPENAI_API_KEY"].startswith("...")
        assert env["OPENAI_API_KEY"].endswith("7890")
        # Real secret never appears.
        assert "sk-fakelongkey" not in r.text


# ── /api/v1/config rejects mcp.* mutations ──────────────────────────────


def test_config_endpoint_rejects_real_mcp_change():
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        # Real change: add a server via /config.
        r = client.put(
            "/api/v1/config",
            json={
                "mcp": {
                    "servers": {
                        "evil": {
                            "transport": "stdio",
                            "stdio": {"command": "/bin/rm", "args": ["-rf", "/"]},
                            "enabled": True,
                        }
                    }
                }
            },
        )
        assert r.status_code == 400
        assert "mcp.* is read-only" in r.json()["detail"]


def test_config_endpoint_tolerates_masked_equal_mcp_payload(
    repo_root, monkeypatch
):
    """A round-trip GET-then-PUT from the Config tab carries `mcp.*` with
    masked values. After mask-restore, the diff is empty → must be a 200,
    not a 400."""
    monkeypatch.chdir(repo_root)
    cfg_disk = MCPConfig(
        servers={
            "fs": MCPServerConfig(
                transport="stdio",
                stdio=MCPStdioParams(
                    command="echo",
                    env={"OPENAI_API_KEY": "sk-realsecret1234"},
                ),
                enabled=False,
            )
        }
    )
    app, _, _ = _build_app(mcp_cfg=cfg_disk)

    monkeypatch.setattr(
        "syll.web.routes.config.save_config",
        lambda c, p=None: None,
    )

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        # Get masked state first (mimicking the UI fetch).
        masked = client.get("/api/v1/config").json()
        # PUT it back unchanged (just a no-op identity edit otherwise).
        masked.setdefault("identity", {})["rituals_enabled"] = True
        r = client.put("/api/v1/config", json=masked)
        assert r.status_code == 200, r.text
