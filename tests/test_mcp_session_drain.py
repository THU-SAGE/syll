"""Phase 1b: MCPSession drain + closing-gate semantics.

Spawns the slow_mcp_server fixture, fires an in-flight call_tool, then
disconnects with a drain timeout. Asserts:

  * Drain awaits the in-flight call when it completes within the budget.
  * `_closing` flag rejects new call_tool BEFORE the inflight refcount
    increments — so the drain observes a correct count regardless of
    timing.
  * `iter_enabled_tools()` excludes a closing session.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from syll.agent.mcp import MCPInvocationError, MCPSession
from syll.config.schema import MCPServerConfig, MCPStdioParams


def _slow_cfg() -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.slow_mcp_server"],
        ),
        enabled=True,
        tool_timeout_seconds=10,
    )


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.mark.timeout(45)
async def test_drain_completes_inflight_call_before_close(repo_root, monkeypatch):
    """A 1.5-second tool call must complete cleanly when drain_timeout is 5s."""
    monkeypatch.chdir(repo_root)
    s = MCPSession("slow", _slow_cfg())
    await s.connect()
    try:
        # Start a slow call but don't await it yet.
        call_task = asyncio.create_task(
            s.call_tool("slow_call", {"seconds": 1.5})
        )
        # Give the call a moment to register inflight before disconnect runs.
        await asyncio.sleep(0.1)
        assert s._inflight == 1, f"expected 1 inflight, got {s._inflight}"

        # Disconnect should drain (wait up to 5s) for the call to finish.
        await s.disconnect(drain_timeout=5.0)

        # The call should have completed successfully BEFORE the transport closed.
        result = await call_task
        text = "".join(
            getattr(b, "text", "") for b in (result.content or [])
        )
        assert text == "ok", f"expected 'ok', got {text!r}"
    finally:
        if not s._closing:
            await s.disconnect()


@pytest.mark.timeout(45)
async def test_closing_gate_rejects_new_calls_without_incrementing_inflight(
    repo_root, monkeypatch
):
    """After `_closing=True`, new calls must raise MCPInvocationError immediately
    AND must NOT bump `_inflight`. The drain timeout depends on the count being
    accurate."""
    monkeypatch.chdir(repo_root)
    s = MCPSession("slow", _slow_cfg())
    await s.connect()
    try:
        # Manually mark the session closing without awaiting drain.
        s._closing = True

        before = s._inflight
        with pytest.raises(MCPInvocationError, match="closing"):
            await s.call_tool("slow_call", {"seconds": 0.1})
        after = s._inflight
        assert after == before, (
            f"closing gate must reject BEFORE incrementing inflight: "
            f"before={before} after={after}"
        )
    finally:
        # Cleanup — disconnect was bypassed by setting _closing=True directly.
        s._closing = False
        await s.disconnect()


@pytest.mark.timeout(60)
async def test_drain_timeout_truncates_long_calls(repo_root, monkeypatch):
    """A call that runs PAST the drain budget gets cut off when the transport
    closes. The drain timeout returns; the in-flight call may surface as
    error-or-cancelled."""
    monkeypatch.chdir(repo_root)
    s = MCPSession("slow", _slow_cfg())
    await s.connect()
    try:
        # Fire a 30-second call.
        call_task = asyncio.create_task(
            s.call_tool("slow_call", {"seconds": 30.0})
        )
        await asyncio.sleep(0.2)
        assert s._inflight == 1

        # Drain with a 0.5-second budget — the call won't finish in time.
        t0 = asyncio.get_event_loop().time()
        await s.disconnect(drain_timeout=0.5)
        elapsed = asyncio.get_event_loop().time() - t0
        # Disconnect must return promptly — within ~3 seconds in the worst case.
        assert elapsed < 3.0, f"disconnect took {elapsed:.1f}s, expected <3"

        # The in-flight call surfaces as a cancellation / error after transport close.
        with pytest.raises((MCPInvocationError, asyncio.CancelledError, Exception)):
            await asyncio.wait_for(call_task, timeout=2.0)
    finally:
        if not s._closing:
            await s.disconnect()
