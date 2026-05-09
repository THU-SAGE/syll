"""Tests for Aloha skill execution endpoint — direct tool call vs process_direct."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from syll.agent.tools.base import ToolResult
from syll.web.routes.aloha_skills import legacy_router, router


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_app(agent_loop, aloha_store):
    """Create a minimal FastAPI app with mocked state."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.include_router(legacy_router, prefix="/api/v1")
    app.state.agent_loop = agent_loop
    app.state.aloha_skill_store = aloha_store
    return app


def _make_skill():
    """Return a minimal mock skill."""
    skill = MagicMock()
    skill.meta.name = "test-skill"
    skill.meta.description = "open calculator"
    skill.meta.actor_mode = "ui-tars"
    return skill


@pytest.mark.anyio
async def test_execute_planner_calls_tool_via_registry():
    """Planner mode should call ToolRegistry.execute() (Issue 6)."""
    agent_loop = MagicMock()
    agent_loop.tools.execute = AsyncMock(
        return_value=ToolResult(text="Done in 3 steps", media=[])
    )

    store = MagicMock()
    store.load_skill.return_value = _make_skill()

    app = _make_app(agent_loop, store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/recorded-workflows/test-skill/execute",
            json={"mode": "planner", "actor_mode": "ui-tars", "instruction": "open calculator"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "executed"
    assert "Done in 3 steps" in data["response"]

    # Verify ToolRegistry.execute() was called, NOT tool.execute() directly
    agent_loop.tools.execute.assert_awaited_once_with(
        "gui_action_planned",
        {
            "instruction": "open calculator",
            "skill_name": "test-skill",
            "actor_mode": "ui-tars",
        },
    )
    agent_loop.process_direct.assert_not_called()


@pytest.mark.anyio
async def test_execute_icl_rich_uses_process_direct():
    """ICL-rich mode should delegate to agent_loop.process_direct()."""
    from syll.agent.result import AgentResult

    agent_loop = MagicMock()
    agent_loop.process_direct = AsyncMock(
        return_value=AgentResult(text="ICL replay done", media=[])
    )

    store = MagicMock()
    store.load_skill.return_value = _make_skill()

    app = _make_app(agent_loop, store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/recorded-workflows/test-skill/execute",
            json={"mode": "icl-rich", "instruction": "open calculator"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "executed"
    assert data["mode"] == "icl-rich"
    assert data["response"] == "ICL replay done"

    agent_loop.process_direct.assert_awaited_once()
    # Verify the message mentions gui_action (not gui_action_planned)
    call_kwargs = agent_loop.process_direct.call_args
    content = call_kwargs.kwargs.get("content", call_kwargs.args[0] if call_kwargs.args else "")
    assert "gui_action(" in content
    assert "full image+text ICL examples enabled" in content


@pytest.mark.anyio
async def test_execute_planner_returns_screenshots():
    """Planner mode should return base64 screenshots when ToolResult has media."""
    import tempfile
    from pathlib import Path

    # Create a tiny PNG for testing
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    # Minimal 1x1 PNG
    import base64 as b64m

    png_data = b64m.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    tmp.write(png_data)
    tmp.flush()
    tmp_path = tmp.name

    agent_loop = MagicMock()
    agent_loop.tools.execute = AsyncMock(
        return_value=ToolResult(text="Done", media=[tmp_path])
    )

    store = MagicMock()
    store.load_skill.return_value = _make_skill()

    app = _make_app(agent_loop, store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/recorded-workflows/test-skill/execute",
            json={"mode": "planner", "actor_mode": "ui-tars", "instruction": "open calculator"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "executed"
    assert data["mode"] == "planner"
    assert len(data["screenshots"]) == 1
    assert data["screenshots"][0]["mime"] == "image/png"
    assert len(data["screenshots"][0]["data"]) > 10  # base64 string

    # Cleanup
    Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.anyio
async def test_execute_planner_tool_not_registered():
    """Should return 503 if gui_action_planned is not registered."""
    agent_loop = MagicMock()
    # ToolRegistry.execute returns error string when tool not found
    agent_loop.tools.execute = AsyncMock(
        return_value="Error: Tool 'gui_action_planned' not found"
    )

    store = MagicMock()
    store.load_skill.return_value = _make_skill()

    app = _make_app(agent_loop, store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/recorded-workflows/test-skill/execute",
            json={"mode": "planner", "instruction": "open calculator"},
        )

    assert resp.status_code == 503
    assert "GUI tools not enabled" in resp.json()["detail"]


@pytest.mark.anyio
async def test_execute_planner_actor_mode_fallback():
    """When actor_mode is empty, should fall back to skill.meta.actor_mode."""
    agent_loop = MagicMock()
    agent_loop.tools.execute = AsyncMock(
        return_value=ToolResult(text="Done", media=[])
    )

    skill = _make_skill()
    skill.meta.actor_mode = "claude-cua"

    store = MagicMock()
    store.load_skill.return_value = skill

    app = _make_app(agent_loop, store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/recorded-workflows/test-skill/execute",
            json={"mode": "planner", "instruction": "open calculator"},
        )

    assert resp.status_code == 200
    assert resp.json()["mode"] == "planner"
    # Verify the actor_mode fell back to "claude-cua" from skill meta
    call_args = agent_loop.tools.execute.call_args
    assert call_args[0][1]["actor_mode"] == "claude-cua"


@pytest.mark.anyio
async def test_execute_invalid_mode_rejected():
    """Unsupported execute modes should fail request validation."""
    agent_loop = MagicMock()
    store = MagicMock()
    store.load_skill.return_value = _make_skill()

    app = _make_app(agent_loop, store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/recorded-workflows/test-skill/execute",
            json={"mode": "icl", "instruction": "open calculator"},
        )

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_legacy_aloha_alias_still_executes():
    """Legacy alias stays available for older clients, but out of the public surface."""
    agent_loop = MagicMock()
    agent_loop.tools.execute = AsyncMock(
        return_value=ToolResult(text="Done via alias", media=[])
    )

    store = MagicMock()
    store.load_skill.return_value = _make_skill()

    app = _make_app(agent_loop, store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/aloha-skills/test-skill/execute",
            json={"mode": "planner", "instruction": "open calculator"},
        )

    assert resp.status_code == 200
    assert resp.json()["response"] == "Done via alias"


def test_legacy_aloha_alias_is_hidden_from_openapi():
    app = _make_app(MagicMock(), MagicMock())
    schema = app.openapi()

    assert "/api/v1/recorded-workflows/{name}/execute" in schema["paths"]
    assert "/api/v1/aloha-skills/{name}/execute" not in schema["paths"]
