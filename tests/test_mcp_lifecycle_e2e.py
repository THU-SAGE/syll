"""Phase 1c end-to-end: FastAPI lifespan brings up MCP and registers tools.

Mirrors what `syll web` does: build MCPManager + AgentLoop, then enter the
FastAPI app's lifespan via TestClient. After lifespan startup, the agent's
tool registry must contain the namespaced MCP tools.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from syll.agent.mcp import MCPManager
from syll.config.schema import (
    MCPConfig,
    MCPServerConfig,
    MCPStdioParams,
)
from syll.web import auth as auth_module
from syll.web.app import create_app
from tests.test_app_factory import _make_config


@pytest.fixture(autouse=True)
def _isolate_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_module, "ADMIN_TOKEN_PATH", tmp_path / "admin_token")


def _echo_server() -> MCPServerConfig:
    """Build an enabled echo server with a valid consent hash so the
    lifespan's boot_validate accepts it (mirrors what the UI's PUT
    /api/v1/mcp/servers flow will do at confirm-time)."""
    from syll.agent.mcp import command_hash

    cfg = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.echo_mcp_server"],
        ),
        enabled=True,
        enabled_tools=["*"],
    )
    cfg.confirmed_command_hash = command_hash(cfg)
    return cfg


def _make_loop_with_mcp(mcp_manager):
    """Minimal AgentLoop wired to mcp_manager — see test_mcp_loop_integration."""
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


@pytest.mark.timeout(60)
def test_lifespan_starts_mcp_and_loop_sees_tools(monkeypatch):
    """FastAPI lifespan with `manage_mcp_lifecycle=True` must:
       1. Run MCPManager.boot_validate + start
       2. Call agent_loop.reload_mcp_tools
       3. Result: agent_loop.tools contains mcp__echo__echo etc.
    """
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)

    cfg = _make_config(Path(tempfile.mkdtemp()))
    cfg.mcp = MCPConfig(servers={"echo": _echo_server()})

    mgr = MCPManager(
        cfg.mcp,
        workspace_path=cfg.workspace_path,
        restrict_to_workspace=False,
    )
    agent_loop = _make_loop_with_mcp(mgr)

    app = create_app(
        config=cfg,
        agent_loop=agent_loop,
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )
    app.state.mcp_manager = mgr
    app.state.manage_mcp_lifecycle = True

    # Sanity before lifespan.
    assert agent_loop._mcp_owned == set()

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        # Lifespan startup runs synchronously inside TestClient.__enter__.
        defs = agent_loop.tools.get_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "mcp__echo__echo" in names, names
        assert "mcp__echo__add" in names, names
        # And the manager owns them.
        assert "mcp__echo__echo" in agent_loop._mcp_owned
        # Health check: the GET /admin-token still works (i.e., MCP startup
        # didn't break the rest of the lifespan).
        r = client.get(
            "/api/v1/admin-token",
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 200

    # After exit, the manager has been stopped and the tools removed.
    defs_after = agent_loop.tools.get_definitions()
    names_after = {d["function"]["name"] for d in defs_after}
    # MCP tools may or may not still be in the registry — we only stop the
    # MANAGER on lifespan exit; the registry isn't auto-cleared. The
    # important guarantee is that no MCP server processes are still running.
    assert mgr._sessions == {}, "manager.stop didn't clear sessions"


@pytest.mark.timeout(30)
def test_lifespan_with_mcp_disabled_is_noop():
    """When MCPConfig.enabled=False, lifespan must NOT call boot_validate
    (which would reject any tampered hash). Loop's owned set stays empty."""
    cfg = _make_config(Path(tempfile.mkdtemp()))
    cfg.mcp = MCPConfig(enabled=False, servers={})

    mgr = MCPManager(cfg.mcp)
    agent_loop = _make_loop_with_mcp(mgr)

    app = create_app(
        config=cfg, agent_loop=agent_loop,
        session_manager=SimpleNamespace(), skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(), cron_service=None,
    )
    app.state.mcp_manager = mgr
    app.state.manage_mcp_lifecycle = True

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert agent_loop._mcp_owned == set()
        r = client.get(
            "/api/v1/admin-token",
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 200
