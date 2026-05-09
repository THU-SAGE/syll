"""Phase 1b: MCPManager integration with the echo stdio fixture.

Spawns `python -m tests.fixtures.echo_mcp_server` as a stdio subprocess,
runs the full connect / list / call / disconnect lifecycle, and asserts
manager-owned name tracking + tool execution.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from syll.agent.mcp import MCPManager
from syll.config.schema import (
    MCPConfig,
    MCPServerConfig,
    MCPStdioParams,
)

# ── Fixture: an echo MCP server spawned as a subprocess ─────────────────


def _echo_server(name: str = "echo", *, enabled_tools: list[str] | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.echo_mcp_server"],
        ),
        enabled=True,
        enabled_tools=enabled_tools if enabled_tools is not None else ["*"],
    )


def _slow_server(name: str = "slow") -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.slow_mcp_server"],
        ),
        enabled=True,
        enabled_tools=["*"],
        tool_timeout_seconds=10,
    )


@pytest.fixture
def repo_root() -> Path:
    """The CWD must allow `python -m tests.fixtures.echo_mcp_server` to import."""
    return Path(__file__).resolve().parent.parent


# ── Lifecycle: connect, list, call, disconnect ─────────────────────────


@pytest.mark.timeout(30)
async def test_manager_connects_and_lists_echo_tools(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        # Both echo tools must appear, namespaced.
        owned = mgr.iter_enabled_tools()
        names = {t.name for t in owned}
        assert "mcp__echo__echo" in names, names
        assert "mcp__echo__add" in names, names
        assert len(owned) == 2
    finally:
        await mgr.stop()


@pytest.mark.timeout(30)
async def test_manager_call_tool_returns_text(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        echo_tool = next(
            t for t in mgr.iter_enabled_tools() if t.name == "mcp__echo__echo"
        )
        result = await echo_tool.execute(text="hello world")
        assert result == "hello world"
        add_tool = next(
            t for t in mgr.iter_enabled_tools() if t.name == "mcp__echo__add"
        )
        result = await add_tool.execute(a=2, b=3)
        assert result == "5"
    finally:
        await mgr.stop()


@pytest.mark.timeout(30)
async def test_manager_validates_params_via_jsonschema(repo_root, monkeypatch):
    """Echo server's input schema requires `text`; missing it should produce
    a validation error string from MCPTool.validate_params."""
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        echo_tool = next(
            t for t in mgr.iter_enabled_tools() if t.name == "mcp__echo__echo"
        )
        errors = echo_tool.validate_params({})
        assert errors  # non-empty list
        assert any("text" in e or "required" in e.lower() for e in errors)
    finally:
        await mgr.stop()


# ── enabled_tools filter and spelling drift warning ─────────────────────


@pytest.mark.timeout(30)
async def test_manager_filters_by_enabled_tools(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(
        servers={"echo": _echo_server(enabled_tools=["echo"])}
    )
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        names = {t.name for t in mgr.iter_enabled_tools()}
        assert names == {"mcp__echo__echo"}, names
    finally:
        await mgr.stop()


@pytest.mark.timeout(30)
async def test_manager_warns_on_unknown_enabled_tool_name(repo_root, monkeypatch, caplog):
    """Phase 1b improvement over NanoBot: spelling drift in enabled_tools
    must produce a warning at connect time."""
    monkeypatch.chdir(repo_root)
    import logging
    caplog.set_level(logging.WARNING)
    # Capture loguru output too — pipe it through stdlib logging.
    from loguru import logger as loguru_logger
    handler_id = loguru_logger.add(lambda msg: logging.getLogger().warning(msg.strip()))
    try:
        cfg = MCPConfig(
            servers={
                "echo": _echo_server(enabled_tools=["echoo", "ad"]),  # typos
            }
        )
        mgr = MCPManager(cfg)
        try:
            await mgr.start()
            warnings = " ".join(r.message for r in caplog.records)
            assert "echoo" in warnings or "unknown" in warnings.lower()
        finally:
            await mgr.stop()
    finally:
        loguru_logger.remove(handler_id)


# ── Manager-owned name tracking + collision behavior ────────────────────


@pytest.mark.timeout(30)
async def test_owned_names_cleared_after_stop(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    await mgr.start()
    assert len(mgr._owned_names) == 2
    await mgr.stop()
    assert mgr._owned_names == set()
    assert mgr._owned_by_server == {}


@pytest.mark.timeout(30)
async def test_disconnect_from_different_task_does_not_cross_cancel_scope(
    repo_root, monkeypatch, caplog
):
    """HTTP hot-reload routes run in request tasks, not the startup task.

    The MCP SDK's anyio scopes must still unwind in the task that opened
    them; otherwise subprocess cleanup logs "different task" and can leak.
    """
    import logging

    monkeypatch.chdir(repo_root)
    caplog.set_level(logging.WARNING)

    from loguru import logger as loguru_logger

    handler_id = loguru_logger.add(lambda msg: logging.getLogger().warning(msg.strip()))
    cfg = MCPConfig(servers={"echo": _echo_server()})
    mgr = MCPManager(cfg)
    try:
        await asyncio.create_task(mgr.start())
        assert "echo" in mgr._sessions
        off = _echo_server()
        off.enabled = False
        await mgr.apply_server("echo", off, strict=True)
        assert mgr._sessions == {}
        warnings = " ".join(r.message for r in caplog.records)
        assert "different task" not in warnings
        assert "cancel scope" not in warnings.lower()
    finally:
        loguru_logger.remove(handler_id)
        await mgr.stop()


# ── apply_server: strict vs non-strict ─────────────────────────────────


@pytest.mark.timeout(30)
async def test_apply_server_strict_raises_on_bad_command(repo_root, monkeypatch):
    """apply_server(strict=True) must raise so the HTTP route can refuse
    to persist a save with a broken command."""
    from syll.agent.mcp import MCPConnectionError

    monkeypatch.chdir(repo_root)
    mgr = MCPManager(MCPConfig())
    bad = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command="/nonexistent-binary-path-syll-test", args=[]
        ),
        enabled=True,
    )
    try:
        with pytest.raises(MCPConnectionError):
            await mgr.apply_server("bad", bad, strict=True)
        # Manager state must remain clean — no half-registered session.
        assert "bad" not in mgr._sessions
        assert mgr._owned_names == set()
    finally:
        await mgr.stop()


@pytest.mark.timeout(30)
async def test_apply_server_non_strict_swallows_failure(repo_root, monkeypatch):
    """`start()` calls apply with strict=False; a single bad server must not
    bring down the rest."""
    monkeypatch.chdir(repo_root)
    mgr = MCPManager(MCPConfig())
    bad = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command="/nonexistent-binary-path-syll-test", args=[]
        ),
        enabled=True,
    )
    try:
        await mgr.apply_server("bad", bad, strict=False)
        # No exception; state stays clean.
        assert "bad" not in mgr._sessions
    finally:
        await mgr.stop()


# ── test_server (one-shot probe for the UI Test button) ─────────────────


@pytest.mark.timeout(30)
async def test_test_server_returns_tool_list(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    mgr = MCPManager(MCPConfig())
    result = await mgr.test_server(_echo_server())
    assert result["ok"] is True
    assert result["tool_count"] == 2
    assert set(result["tools"]) == {"echo", "add"}


@pytest.mark.timeout(15)
async def test_test_server_returns_error_on_bad_command():
    mgr = MCPManager(MCPConfig())
    bad = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command="/nonexistent-binary-path-syll-test", args=[]
        ),
        enabled=True,
    )
    result = await mgr.test_server(bad)
    assert result["ok"] is False
    assert "error" in result
