"""Tests for Aloha planner tool helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from syll.agent.tools.aloha_planner_tool import AlohaPlannerTool


def test_format_action_history_includes_positions_and_click_diagnostics():
    """Planner history should include both coordinate spaces and backend diagnostics."""
    result = AlohaPlannerTool._format_action_history(
        "Double-click the Stardew Valley.app icon on the desktop",
        "Selected desktop app at (178, 70) and opened with Command+O [backend=pyautogui, accessibility=authorized, event_style=down_up, frontmost=Finder]",
        [132, 50],
        [178, 70],
        "pyautogui",
        "authorized",
        "down_up",
        "Finder",
    )

    assert "model_position=[132, 50]" in result
    assert "executor_position=[178, 70]" in result
    assert "click_backend=pyautogui" in result
    assert "mac_accessibility=authorized" in result
    assert "event_style=down_up" in result
    assert "frontmost_app=Finder" in result
    assert "Selected desktop app at (178, 70) and opened with Command+O" in result



def test_rewrite_forbidden_first_step_action_replaces_cmd_h_with_guidance():
    """Step 1 should not plan Cmd+H when the demo already defines a concrete first action."""
    skill = SimpleNamespace(
        trajectory=[
            {
                "step_idx": 1,
                "caption": {
                    "action": "Double-click the Stardew Valley.app icon on the desktop"
                },
            }
        ],
        steps=[],
    )

    result = AlohaPlannerTool._rewrite_forbidden_first_step_action(
        "Use keyboard shortcut Cmd+H to hide the current window first",
        1,
        skill,
    )

    assert result == "Double-click the Stardew Valley.app icon on the desktop"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_execute_prefers_skill_actor_mode_over_global_actor_model(tmp_path):
    """Skill actor_mode should win even when the global actor model looks like Claude."""
    screenshot_path = tmp_path / "shot.png"
    screenshot_path.write_bytes(b"fake")

    skill = SimpleNamespace(
        meta=SimpleNamespace(actor_mode="ui-tars"),
        trajectory=[{"step_idx": 1, "caption": {"action": "Open app"}}],
        steps=[],
    )
    store = SimpleNamespace(load_skill=lambda _: skill)
    gui_config = SimpleNamespace(max_steps=3, execution_mode="planner", selected_screen=0)
    syll_config = SimpleNamespace(
        resolve_endpoint=lambda purpose: SimpleNamespace(
            litellm_model="anthropic/claude-opus-4.6",
            model="anthropic/claude-opus-4.6",
            api_key="test-key",
            api_base="https://openrouter.ai/api/v1",
        )
    )
    tool = AlohaPlannerTool(gui_config, store, syll_config=syll_config)

    class FakePlanner:
        async def plan(self, **kwargs):
            return {
                "Action": "Double-click the Stardew Valley.app icon on the desktop",
                "Observation": "desktop visible",
                "Reasoning": "follow step 1",
                "Current Step": 1,
            }

    seen_modes: list[str] = []

    async def fake_call_actor(mode, plan_action, screenshot_b64, os_name):
        seen_modes.append(mode)
        return {"action": "FINISHED", "value": "", "position": [0, 0]}, True

    with patch("syll.agent.tools.aloha_planner_tool.AlohaPlanner", return_value=FakePlanner()), patch.object(
        tool, "_take_screenshot", AsyncMock(return_value=str(screenshot_path))
    ), patch.object(tool, "_call_actor", AsyncMock(side_effect=fake_call_actor)):
        result = await tool.execute(
            instruction="Launch Stardew Valley game from desktop",
            skill_name="stardew_start",
        )

    assert seen_modes == ["ui-tars"]
    assert "GUI task completed by actor" in result.text
