"""Tests for rich Aloha ICL context in the UI-TARS tool."""

import base64
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from syll.agent.aloha_gui_skill import (
    AlohaAction,
    AlohaSkill,
    AlohaSkillMeta,
    AlohaSkillStore,
    AlohaStep,
    AlohaTrace,
)
from syll.agent.tools.ui_tars import Conversation, UITarsTool


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_tool(tmp_path):
    store = AlohaSkillStore(tmp_path)
    config = SimpleNamespace(max_steps=5, selected_screen=0)
    tool = UITarsTool(config, aloha_skill_store=store)
    return tool, store


def _write_keyframe(store: AlohaSkillStore, skill_name: str, filename: str, content: bytes) -> None:
    keyframes_dir = store.skills_dir / skill_name / "keyframes"
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    (keyframes_dir / filename).write_bytes(content)


def test_build_aloha_icl_prefers_crop_and_upgrades_open_click(tmp_path):
    """Rich ICL should prefer crop images and upgrade open intents to double-click."""
    tool, store = _make_tool(tmp_path)
    skill_name = "demo-skill"
    crop_bytes = b"crop-image"
    full_bytes = b"full-image"
    _write_keyframe(store, skill_name, "step_01_crop.jpg", crop_bytes)
    _write_keyframe(store, skill_name, "step_01_full.jpg", full_bytes)

    skill = AlohaSkill(
        meta=AlohaSkillMeta(name=skill_name, description="Open calculator"),
        steps=[
            AlohaStep(
                index=1,
                screenshot="step_01_full.jpg",
                screenshot_crop="step_01_crop.jpg",
                action=AlohaAction(
                    type="click",
                    coordinates=[40, 50],
                    description="Open calculator from the dock",
                ),
                trace=AlohaTrace(
                    observation="Calculator icon is visible",
                    think="Open the Calculator app",
                    action="Open calculator from the dock",
                ),
            )
        ],
    )

    turns = tool._build_aloha_icl(skill, skill_name)

    assert len(turns) == 2
    assert turns[0].screenshot_b64 == base64.b64encode(crop_bytes).decode()
    assert turns[0].screenshot_mime == "image/jpeg"
    assert turns[1].text == (
        "Thought: Open the Calculator app\n"
        "Action: double_click(start='(40, 50)')"
    )


def test_build_aloha_icl_falls_back_to_full_image_and_description(tmp_path):
    """When crop or trace is missing, rich ICL should fall back gracefully."""
    tool, store = _make_tool(tmp_path)
    skill_name = "fallback-skill"
    full_bytes = b"full-only-image"
    _write_keyframe(store, skill_name, "step_01_full.jpg", full_bytes)

    skill = AlohaSkill(
        meta=AlohaSkillMeta(name=skill_name, description="Focus search"),
        steps=[
            AlohaStep(
                index=1,
                screenshot="step_01_full.jpg",
                screenshot_crop="missing_crop.jpg",
                action=AlohaAction(
                    type="click",
                    coordinates=[11, 22],
                    description="Focus the search input",
                ),
                trace=None,
            )
        ],
    )

    turns = tool._build_aloha_icl(skill, skill_name)

    assert len(turns) == 2
    assert turns[0].screenshot_b64 == base64.b64encode(full_bytes).decode()
    assert turns[0].screenshot_mime == "image/jpeg"
    assert turns[1].text == (
        "Thought: Focus the search input\n"
        "Action: double_click(start='(11, 22)')"
    )


@pytest.mark.anyio
async def test_call_uitars_uses_conversation_image_mime(tmp_path):
    """The outbound multimodal payload should preserve each image's true MIME type."""
    tool, _ = _make_tool(tmp_path)
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Action: finished(content='done')"))]
        )

    with patch.dict(sys.modules, {"litellm": SimpleNamespace(acompletion=fake_acompletion)}):
        await tool._call_uitars(
            "Launch Stardew Valley",
            [
                Conversation(
                    role="user",
                    screenshot_b64="ZmFrZS1qcGVn",
                    screenshot_mime="image/jpeg",
                    is_icl=True,
                )
            ],
        )

    image_url = captured["messages"][2]["content"][0]["image_url"]["url"]
    assert image_url == "data:image/jpeg;base64,ZmFrZS1qcGVn"


@pytest.mark.anyio
async def test_execute_action_uses_mac_mouse_backend_fallback(tmp_path):
    """UI-TARS execution should use the shared mac click backend diagnostics."""
    config = SimpleNamespace(
        max_steps=5,
        selected_screen=0,
        click_backend="pyautogui",
        mac_click_style="auto",
        preflight_permissions=True,
    )
    tool = UITarsTool(config)
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.asyncio.sleep", new=AsyncMock()
    ), patch("syll.agent.gui_click.platform.system", return_value="Darwin"), patch(
        "syll.agent.gui_click.detect_mac_accessibility_status", return_value="authorized"
    ), patch("syll.agent.gui_click.detect_frontmost_app", return_value="Stardew Valley"):
        success, message = await tool._execute_action(
            "click(start='(10, 20)')",
            "Click the confirm button",
        )

    assert success is True
    assert "Double-clicked at (10, 20)" in message
    assert "backend=pyautogui" in message
    assert "event_style=down_up" in message
    assert pyautogui.mouseDown.call_count == 2
    assert pyautogui.mouseUp.call_count == 2


@pytest.mark.anyio
async def test_execute_hotkey_normalizes_cmd_alias_on_macos(tmp_path):
    """UI-TARS hotkeys should normalize cmd+h to command+h on macOS."""
    config = SimpleNamespace(
        max_steps=5,
        selected_screen=0,
        click_backend="pyautogui",
        mac_click_style="auto",
        preflight_permissions=True,
    )
    tool = UITarsTool(config)
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.platform.system", return_value="Darwin"
    ):
        success, message = await tool._execute_action("hotkey(key='cmd+h')", "Hide the current window")

    assert success is True
    pyautogui.hotkey.assert_called_once_with("command", "h")
    pyautogui.press.assert_not_called()
    assert message == "Pressed: command+h"
