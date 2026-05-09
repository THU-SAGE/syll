"""Phase 1c: AgentLoop + SubagentManager + MCPManager integration.

These tests don't spin up a real LLM — they wire MCPManager into AgentLoop
and SubagentManager and assert the tool registries are populated correctly,
collisions are skipped, and reload_mcp_tools is idempotent.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from syll.agent.mcp import MCPManager
from syll.agent.tools.base import Tool
from syll.config.schema import (
    MCPConfig,
    MCPServerConfig,
    MCPStdioParams,
)


def _echo_server(*, propagate: bool = True, enabled_tools=None) -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.echo_mcp_server"],
        ),
        enabled=True,
        enabled_tools=enabled_tools if enabled_tools is not None else ["*"],
        propagate_to_subagents=propagate,
    )


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _make_agent_loop(mcp_manager=None):
    """Build a minimal AgentLoop without firing up real channels / cron / LLM."""
    from syll.agent.loop import AgentLoop
    from syll.bus.queue import MessageBus

    class _StubProvider:
        def get_default_model(self):
            return "stub"

    # Avoid the real ContextBuilder which reads workspace files; patch its
    # constructor to a SimpleNamespace.
    with patch("syll.agent.loop.ContextBuilder") as ctx_cls, \
         patch("syll.agent.loop.SessionManager") as sess_cls, \
         patch("syll.agent.loop.EventStore") as evt_cls:
        ctx_cls.return_value = SimpleNamespace(identity=None)
        sess_cls.return_value = SimpleNamespace()
        evt_cls.return_value = SimpleNamespace()
        loop = AgentLoop(
            bus=MessageBus(),
            provider=_StubProvider(),
            workspace=Path("/tmp"),
            mcp_manager=mcp_manager,
        )
    return loop


# ── reload_mcp_tools idempotency + ownership ─────────────────────────────


@pytest.mark.timeout(60)
async def test_reload_mcp_tools_registers_owned_set(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        loop = _make_agent_loop(mcp_manager=mgr)
        n = loop.reload_mcp_tools()
        assert n == 2
        assert "mcp__echo__echo" in loop._mcp_owned
        assert "mcp__echo__add" in loop._mcp_owned
        # Adapter is callable via the registry.
        defs = loop.tools.get_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "mcp__echo__echo" in names
    finally:
        await mgr.stop()


@pytest.mark.timeout(60)
async def test_reload_mcp_tools_idempotent(repo_root, monkeypatch):
    """Repeated reloads must not duplicate or strand entries."""
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        loop = _make_agent_loop(mcp_manager=mgr)
        for _ in range(3):
            loop.reload_mcp_tools()
        # Owned set still has exactly the 2 tools.
        assert loop._mcp_owned == {"mcp__echo__echo", "mcp__echo__add"}
        # Registry has them registered exactly once each.
        defs = loop.tools.get_definitions()
        owned_in_registry = [
            d for d in defs if d["function"]["name"].startswith("mcp__echo__")
        ]
        assert len(owned_in_registry) == 2
    finally:
        await mgr.stop()


@pytest.mark.timeout(60)
async def test_reload_drops_stale_entries_when_server_removed(repo_root, monkeypatch):
    """After remove_server + reload, the entries are gone from the registry."""
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        loop = _make_agent_loop(mcp_manager=mgr)
        loop.reload_mcp_tools()
        assert "mcp__echo__echo" in loop._mcp_owned

        await mgr.remove_server("echo")
        loop.reload_mcp_tools()
        assert loop._mcp_owned == set()
        defs = loop.tools.get_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "mcp__echo__echo" not in names
    finally:
        await mgr.stop()


# ── Collision protection ──────────────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_reload_does_not_clobber_non_mcp_tool(repo_root, monkeypatch, caplog):
    """If a builtin tool happens to have the same name as an MCP adapter,
    the MCP one must be skipped — never overwrite a non-MCP tool."""
    import logging

    monkeypatch.chdir(repo_root)
    caplog.set_level(logging.WARNING)
    # Pipe loguru to stdlib logging so caplog catches it.
    from loguru import logger as loguru_logger
    handler_id = loguru_logger.add(lambda m: logging.getLogger().warning(m.strip()))

    class _FakeNonMcpTool(Tool):
        name = "mcp__echo__echo"  # collides with what the manager will try to register
        description = "stub"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return "from-builtin"

    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        loop = _make_agent_loop(mcp_manager=mgr)
        # Pre-register a "non-MCP" tool with a colliding name. (Far-fetched
        # in practice because of the namespacing, but the guard is defense
        # in depth.)
        builtin = _FakeNonMcpTool()
        loop.tools.register(builtin)
        # Snapshot id of the builtin so we can prove it's still there.
        before = id(loop.tools.get("mcp__echo__echo"))
        n = loop.reload_mcp_tools()
        after = id(loop.tools.get("mcp__echo__echo"))
        assert before == after, (
            "MCP adapter clobbered the pre-registered tool"
        )
        assert "mcp__echo__echo" not in loop._mcp_owned
        assert n == 1, "the second tool (mcp__echo__add) should still register"
        # Warning emitted.
        warnings = " ".join(r.message for r in caplog.records)
        assert "collide" in warnings or "collides" in warnings.lower()
    finally:
        loguru_logger.remove(handler_id)
        await mgr.stop()


# ── SubagentManager propagation ──────────────────────────────────────────


def _make_subagent_manager(mcp_manager=None):
    from syll.agent.subagent import SubagentManager
    from syll.bus.queue import MessageBus

    class _StubProvider:
        def get_default_model(self):
            return "stub"

    return SubagentManager(
        provider=_StubProvider(),
        workspace=Path("/tmp"),
        bus=MessageBus(),
        mcp_manager=mcp_manager,
    )


@pytest.mark.timeout(60)
async def test_subagent_propagates_only_opted_in_servers(repo_root, monkeypatch):
    """Two servers, one with propagate_to_subagents=True and one False; the
    subagent's registry should contain only the propagated server's tools."""
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(
        servers={
            "yes": _echo_server(propagate=True),
            "no": _echo_server(propagate=False),
        }
    )
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        # `iter_propagating_tools` is what the subagent actually loops over;
        # assert it filters correctly.
        propagating_names = {t.name for t in mgr.iter_propagating_tools()}
        assert any(n.startswith("mcp__yes__") for n in propagating_names)
        assert not any(
            n.startswith("mcp__no__") for n in propagating_names
        ), propagating_names

        # And that SubagentManager carries the manager pointer through.
        sub = _make_subagent_manager(mcp_manager=mgr)
        assert sub.mcp_manager is mgr
    finally:
        await mgr.stop()


@pytest.mark.timeout(60)
async def test_subagent_run_registers_propagating_tools(repo_root, monkeypatch):
    """End-to-end shape: invoke the subagent's per-spawn registry-build path
    and assert MCP tools land on it. We monkeypatch the LLM call so the run
    exits after one turn."""
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server(propagate=True)})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        sub = _make_subagent_manager(mcp_manager=mgr)

        registered_names: list[str] = []

        async def _fake_chat(self, messages, tools, model):
            # Snapshot what the subagent passed to the provider.
            registered_names.extend(
                d["function"]["name"] for d in tools or []
            )
            from syll.providers.base import LLMResponse
            return LLMResponse(content="done", tool_calls=[])

        # Patch the provider's chat method via the manager-installed instance.
        sub.provider.chat = _fake_chat.__get__(sub.provider)
        sub.provider.get_default_model = lambda: "stub"

        # The announcer publishes to the bus — set a noop callback.
        sub._announce_result = lambda *a, **kw: asyncio.sleep(0)

        await sub._run_subagent(
            task_id="t1",
            task="echo something",
            label="t1",
            origin={"channel": "cli", "chat_id": "test"},
        )
        assert "mcp__echo__echo" in registered_names, registered_names
        assert "mcp__echo__add" in registered_names
    finally:
        await mgr.stop()
