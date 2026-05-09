"""Phase 3 review-pass-6 regressions.

Four findings, one or more tests each:
  C1 — /api/v1/config never leaks MCP env/header values (mask + strip).
  H2 — master `mcp.enabled=False` blocks subprocess launches via PUT and apply_server.
  H3 — POST /_test for stdio requires the same `confirmed_command_hash` as PUT.
  M4 — UI Edit action / HTTP/SSE headers — covered by structural app.js / index.html assertions.
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
    MCPHttpParams,
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


def _make_loop_with_mcp(mcp_manager):
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


# ── C1: /api/v1/config must not leak MCP env/header secrets ─────────────


def test_config_get_does_not_leak_arbitrary_mcp_env_keys(monkeypatch, tmp_path):
    """`Authorization`, `MY_SECRET`, etc. don't match the generic
    `_mask_sensitive` regex (`api_key|token|app_secret|encrypt_key`). The MCP
    section must be masked via the dedicated walker AND stripped from the
    /config response so it can never be the leak path."""
    from syll.config.schema import Config

    real_config = Config()
    real_config.mcp = MCPConfig(
        servers={
            "remote": MCPServerConfig(
                transport="streamableHttp",
                http=MCPHttpParams(
                    url="https://api.example/mcp",
                    headers={
                        "Authorization": "Bearer literal-secret-1234",
                        "X-API-Key": "abracadabra-9999",
                        "MY_SECRET": "deadbeef-leak",
                    },
                ),
                enabled=False,
            ),
            "fs": MCPServerConfig(
                transport="stdio",
                stdio=MCPStdioParams(
                    command="echo",
                    env={
                        "OPENAI_API_KEY": "sk-realsecret",
                        "AUTH": "literal-auth-secret",
                        "OTHER": "literal-other-secret",
                    },
                ),
                enabled=False,
            ),
        }
    )
    monkeypatch.setattr("syll.web.routes.config.load_config", lambda: real_config)

    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get("/api/v1/config")
        assert r.status_code == 200
        text = r.text
        # NONE of the literal secrets must appear in the response.
        for secret in [
            "literal-secret-1234",
            "abracadabra-9999",
            "deadbeef-leak",
            "sk-realsecret",
            "literal-auth-secret",
            "literal-other-secret",
        ]:
            assert secret not in text, f"secret {secret!r} leaked in /api/v1/config response"
        # And mcp is stripped — UI must use /api/v1/mcp.
        body = r.json()
        assert "mcp" not in body, "GET /api/v1/config must strip mcp; UI uses /api/v1/mcp"


def test_save_config_js_strips_mcp_from_put_body():
    """saveConfig() must `delete body.mcp` defensively."""
    app_js = (Path(__file__).resolve().parent.parent
              / "syll" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    # Search inside the saveConfig method body.
    idx = app_js.index("async saveConfig()")
    snippet = app_js[idx:idx + 1500]
    assert "delete body.mcp" in snippet, (
        "saveConfig must strip mcp from PUT body to avoid leaking secrets"
    )


# ── H2: master mcp.enabled=False blocks subprocess launches ─────────────


def test_put_server_refused_when_master_disabled(monkeypatch):
    """Even with a valid consent hash, enabling a server while master is
    off must return 409 with `mcp_master_disabled`."""
    cfg_disk = MCPConfig(enabled=False, servers={})
    app, _, _ = _build_app(mcp_cfg=cfg_disk)
    monkeypatch.setattr("syll.web.routes.mcp.save_config", lambda c, p=None: None)
    monkeypatch.setattr("syll.web.routes.mcp.load_config",
                        lambda: SimpleNamespace(mcp=cfg_disk))

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = {
            "transport": "stdio",
            "stdio": {"command": sys.executable, "args": ["-m", "tests.fixtures.echo_mcp_server"]},
            "enabled": True,
        }
        body["confirmed_command_hash"] = command_hash(MCPServerConfig.model_validate(body))
        r = client.put("/api/v1/mcp/servers/echo", json=body)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "mcp_master_disabled"


def test_put_disabled_entry_allowed_when_master_disabled(monkeypatch):
    """Saving a server with enabled=False is config-only — should be allowed
    even when master is off."""
    cfg_disk = MCPConfig(enabled=False, servers={})
    app, _, _ = _build_app(mcp_cfg=cfg_disk)
    monkeypatch.setattr("syll.web.routes.mcp.save_config", lambda c, p=None: None)
    monkeypatch.setattr("syll.web.routes.mcp.load_config",
                        lambda: SimpleNamespace(mcp=cfg_disk))

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = {
            "transport": "stdio",
            "stdio": {"command": "echo"},
            "enabled": False,
        }
        r = client.put("/api/v1/mcp/servers/echo", json=body)
        assert r.status_code == 200, r.text


@pytest.mark.timeout(30)
async def test_apply_server_skips_when_master_disabled(repo_root, monkeypatch):
    """Defense in depth: even if a caller bypasses the route, the manager's
    apply_server is a hard gate when master is off — no subprocess launches."""
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(enabled=False, servers={})
    mgr = MCPManager(cfg)
    server = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.echo_mcp_server"],
        ),
        enabled=True,
    )
    try:
        # apply_server must not raise AND must not register a session.
        await mgr.apply_server("echo", server, strict=True)
        assert mgr._sessions == {}, "subprocess launched while master is off"
    finally:
        await mgr.stop()


def test_put_mcp_settings_toggles_master_switch(monkeypatch):
    """The MCP tab must have a safe route to turn the master switch back on.

    Updating this root setting must not launch configured servers by itself.
    """
    from syll.config.schema import Config

    cfg_disk = Config()
    cfg_disk.mcp = MCPConfig(enabled=False, servers={})
    app, _, mgr = _build_app(mcp_cfg=cfg_disk.mcp)
    state = {"cfg": cfg_disk}
    monkeypatch.setattr("syll.web.routes.mcp.load_config", lambda: state["cfg"])
    monkeypatch.setattr(
        "syll.web.routes.mcp.save_config",
        lambda c, p=None: state.update(cfg=c),
    )

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.put("/api/v1/mcp", json={"enabled": True})
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True
        assert state["cfg"].mcp.enabled is True
        assert mgr.cfg.enabled is True
        assert mgr._sessions == {}


# ── H3: _test consent for stdio ─────────────────────────────────────────


def test_test_endpoint_requires_hash_for_stdio(repo_root, monkeypatch):
    """POST /_test with a stdio body and no hash → 409 with preview.
    Covers the same threat surface as PUT — `_test` IS launching a subprocess."""
    monkeypatch.chdir(repo_root)
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = {
            "transport": "stdio",
            "stdio": {"command": sys.executable, "args": ["-m", "tests.fixtures.echo_mcp_server"]},
            "enabled": True,
        }
        r = client.post("/api/v1/mcp/_test", json=body)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "confirmation_required"
        assert detail["required_command_hash"].startswith("sha256:")
        assert sys.executable in detail["effective_command_preview"]


def test_test_endpoint_runs_with_correct_hash(repo_root, monkeypatch):
    """Re-POST with the hash must succeed."""
    monkeypatch.chdir(repo_root)
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = {
            "transport": "stdio",
            "stdio": {"command": sys.executable, "args": ["-m", "tests.fixtures.echo_mcp_server"]},
            "enabled": True,
        }
        body["confirmed_command_hash"] = command_hash(MCPServerConfig.model_validate(body))
        r = client.post("/api/v1/mcp/_test", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["tool_count"] == 2


def test_test_endpoint_http_does_not_require_hash():
    """HTTP / SSE probes don't launch local processes — bypass consent gate."""
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = {
            "transport": "streamableHttp",
            "http": {"url": "http://10.255.255.1:65000/mcp"},  # non-routable; will fail-fast
            "enabled": True,
        }
        r = client.post("/api/v1/mcp/_test", json=body)
        # 200 (with ok=False) — endpoint succeeds, probe fails. Critically NOT 409.
        assert r.status_code == 200
        assert r.json()["ok"] is False


def test_test_endpoint_master_off_returns_409(monkeypatch):
    cfg_disk = MCPConfig(enabled=False, servers={})
    app, _, _ = _build_app(mcp_cfg=cfg_disk)
    monkeypatch.setattr("syll.web.routes.mcp.load_config",
                        lambda: SimpleNamespace(mcp=cfg_disk))
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = {"transport": "stdio", "stdio": {"command": "echo"}, "enabled": True}
        body["confirmed_command_hash"] = command_hash(MCPServerConfig.model_validate(body))
        r = client.post("/api/v1/mcp/_test", json=body)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "mcp_master_disabled"


def test_reconnect_refused_when_master_disabled(monkeypatch):
    server = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(command="echo"),
        enabled=True,
    )
    server.confirmed_command_hash = command_hash(server)
    cfg_disk = MCPConfig(enabled=False, servers={"echo": server})
    app, _, mgr = _build_app(mcp_cfg=cfg_disk)
    monkeypatch.setattr("syll.web.routes.mcp.load_config",
                        lambda: SimpleNamespace(mcp=cfg_disk))

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/mcp/servers/echo/reconnect")
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "mcp_master_disabled"
        assert mgr._sessions == {}


def test_reconnect_requires_hash_for_stdio(repo_root, monkeypatch):
    """Reconnect is also a launch path, so tampered stdio config must not run."""
    monkeypatch.chdir(repo_root)
    server = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.echo_mcp_server"],
        ),
        enabled=True,
        confirmed_command_hash="sha256:tampered",
    )
    cfg_disk = MCPConfig(enabled=True, servers={"echo": server})
    app, _, mgr = _build_app(mcp_cfg=cfg_disk)
    monkeypatch.setattr("syll.web.routes.mcp.load_config",
                        lambda: SimpleNamespace(mcp=cfg_disk))

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/mcp/servers/echo/reconnect")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["error"] == "confirmation_required"
        assert detail["required_command_hash"] == command_hash(server)
        assert mgr._sessions == {}


def test_reconnect_with_valid_hash_still_works(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    server = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.echo_mcp_server"],
        ),
        enabled=True,
    )
    server.confirmed_command_hash = command_hash(server)
    cfg_disk = MCPConfig(enabled=True, servers={"echo": server})
    app, _, mgr = _build_app(mcp_cfg=cfg_disk)
    monkeypatch.setattr("syll.web.routes.mcp.load_config",
                        lambda: SimpleNamespace(mcp=cfg_disk))

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/mcp/servers/echo/reconnect")
        assert r.status_code == 200, r.text
        assert "echo" in mgr._sessions


# ── M4: UI surface for edit + HTTP/SSE headers ──────────────────────────


def test_ui_has_edit_button_and_start_edit_method():
    """Phase 3 review-pass-6 M4: the list row must offer an Edit action that
    populates mcpForm; the JS must define mcpStartEdit."""
    static = Path(__file__).resolve().parent.parent / "syll" / "web" / "static"
    app_js = (static / "app.js").read_text(encoding="utf-8")
    index_html = (static / "index.html").read_text(encoding="utf-8")

    assert "mcpStartEdit(name)" in app_js, "mcpStartEdit() must exist in app.js"
    assert "@click=\"mcpStartEdit(name)\"" in index_html, (
        "Edit button must call mcpStartEdit"
    )
    # Inspect button is renamed (no longer the "Edit" text on the toggle).
    # The expand/collapse button now labels itself Inspect/Hide.


def test_ui_has_master_switch_and_settings_route():
    static = Path(__file__).resolve().parent.parent / "syll" / "web" / "static"
    app_js = (static / "app.js").read_text(encoding="utf-8")
    index_html = (static / "index.html").read_text(encoding="utf-8")
    assert "mcpSaveSettings()" in app_js
    assert "fetch('/api/v1/mcp'" in app_js
    assert "MCP master switch" in index_html
    assert "Save MCP settings" in index_html


def test_ui_has_http_sse_headers_input():
    """The form must support headers JSON for non-stdio transports."""
    index_html = (Path(__file__).resolve().parent.parent / "syll" / "web" / "static"
                  / "index.html").read_text(encoding="utf-8")
    # Look for the headers JSON input that's gated on transport !== 'stdio'.
    assert "Headers (JSON object)" in index_html, (
        "HTTP/SSE headers input missing from MCP form"
    )


def test_ui_confirm_modal_dispatches_test_vs_save():
    """The confirm modal carries an `action` discriminator and offers
    distinct buttons for test vs save."""
    static = Path(__file__).resolve().parent.parent / "syll" / "web" / "static"
    app_js = (static / "app.js").read_text(encoding="utf-8")
    index_html = (static / "index.html").read_text(encoding="utf-8")
    assert "mcpConfirmAndTest" in app_js
    assert "mcpConfirmAndSave" in app_js
    # Modal text differentiates.
    assert "Confirm and run test" in index_html
    assert "Confirm and enable" in index_html
