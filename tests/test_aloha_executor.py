"""Tests for mac click backend selection in the Aloha executor."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from syll.agent.aloha.act.executor import AlohaExecutor


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_click_uses_extra_consecutive_clicks_by_default():
    """Plain CLICK actions should execute one extra consecutive click by default."""
    executor = AlohaExecutor(SimpleNamespace(click_backend="pyautogui", mac_click_style="click"))
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.asyncio.sleep", new=AsyncMock()
    ), patch("syll.agent.gui_click.platform.system", return_value="Linux"):
        success, message = await executor.execute({"action": "CLICK", "position": [10, 20]})

    assert success is True
    assert "Double-clicked at (10, 20)" in message
    assert "backend=pyautogui" in message
    assert "event_style=click" in message
    assert pyautogui.click.call_count == 2


@pytest.mark.anyio
async def test_click_uses_mouse_down_up_on_macos_pyautogui_backend():
    """macOS pyautogui fallback should emit explicit mouseDown/mouseUp events."""
    executor = AlohaExecutor(
        SimpleNamespace(click_backend="pyautogui", mac_click_style="auto", preflight_permissions=True)
    )
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.asyncio.sleep", new=AsyncMock()
    ), patch("syll.agent.gui_click.platform.system", return_value="Darwin"), patch(
        "syll.agent.gui_click.detect_mac_accessibility_status", return_value="authorized"
    ), patch("syll.agent.gui_click.detect_frontmost_app", return_value="Stardew Valley"):
        success, message = await executor.execute({"action": "CLICK", "position": [42, 84]})

    assert success is True
    assert "Double-clicked at (42, 84)" in message
    assert "backend=pyautogui" in message
    assert "event_style=down_up" in message
    assert "frontmost=Stardew Valley" in message
    assert pyautogui.click.call_count == 0
    assert pyautogui.mouseDown.call_count == 2
    assert pyautogui.mouseUp.call_count == 2


@pytest.mark.anyio
async def test_double_click_stays_strict_double_click_on_macos():
    """Explicit DOUBLE_CLICK should stay a strict double-click sequence on macOS."""
    executor = AlohaExecutor(
        SimpleNamespace(click_backend="pyautogui", mac_click_style="auto", preflight_permissions=True)
    )
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.asyncio.sleep", new=AsyncMock()
    ), patch("syll.agent.gui_click.platform.system", return_value="Darwin"), patch(
        "syll.agent.gui_click.detect_mac_accessibility_status", return_value="authorized"
    ), patch("syll.agent.gui_click.detect_frontmost_app", return_value="Stardew Valley"):
        success, message = await executor.execute({"action": "DOUBLE_CLICK", "position": [7, 9]})

    assert success is True
    assert "Double-clicked at (7, 9)" in message
    assert "event_style=down_up" in message
    assert pyautogui.mouseDown.call_count == 2
    assert pyautogui.mouseUp.call_count == 2


@pytest.mark.anyio
async def test_triple_click_uses_explicit_click_sequence():
    """TRIPLE_CLICK should stay a strict triple-click sequence."""
    executor = AlohaExecutor(SimpleNamespace(click_backend="pyautogui", mac_click_style="click"))
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.asyncio.sleep", new=AsyncMock()
    ), patch("syll.agent.gui_click.platform.system", return_value="Linux"):
        success, message = await executor.execute({"action": "TRIPLE_CLICK", "position": [5, 6]})

    assert success is True
    assert "Triple-clicked at (5, 6)" in message
    assert pyautogui.click.call_count == 3


@pytest.mark.anyio
async def test_desktop_app_icon_uses_command_o_shortcut_on_macos():
    """Desktop .app icons on macOS should use single-click selection + Cmd+O."""
    executor = AlohaExecutor(
        SimpleNamespace(click_backend="pyautogui", mac_click_style="auto", preflight_permissions=True)
    )
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.asyncio.sleep", new=AsyncMock()
    ), patch("syll.agent.gui_click.platform.system", return_value="Darwin"), patch(
        "syll.agent.gui_click.detect_mac_accessibility_status", return_value="authorized"
    ), patch("syll.agent.gui_click.detect_frontmost_app", return_value="Finder"):
        success, message = await executor.execute(
            {
                "action": "CLICK",
                "position": [132, 50],
                "intent": "Double-click the Stardew Valley.app icon on the desktop",
            }
        )

    assert success is True
    assert "Selected desktop app at (132, 50) and opened with Command+O" in message
    assert "backend=pyautogui" in message
    assert pyautogui.click.call_count == 0
    assert pyautogui.mouseDown.call_count == 1
    assert pyautogui.mouseUp.call_count == 1
    pyautogui.hotkey.assert_called_once_with("command", "o")


@pytest.mark.anyio
async def test_click_blocks_when_accessibility_is_denied():
    """Preflight should block mac clicks when Accessibility is explicitly denied."""
    executor = AlohaExecutor(
        SimpleNamespace(click_backend="pyautogui", mac_click_style="auto", preflight_permissions=True)
    )
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.asyncio.sleep", new=AsyncMock()
    ), patch("syll.agent.gui_click.platform.system", return_value="Darwin"), patch(
        "syll.agent.gui_click.detect_mac_accessibility_status", return_value="denied"
    ), patch("syll.agent.gui_click.detect_frontmost_app", return_value="Stardew Valley"):
        success, message = await executor.execute({"action": "CLICK", "position": [10, 20]})

    assert success is False
    assert "Accessibility permission is not authorized" in message
    assert "accessibility=denied" in message


@pytest.mark.anyio
async def test_key_action_normalizes_cmd_alias_on_macos():
    """cmd+h should normalize to command+h instead of typing only h on macOS."""
    executor = AlohaExecutor(
        SimpleNamespace(click_backend="pyautogui", mac_click_style="auto", preflight_permissions=True)
    )
    pyautogui = MagicMock()

    with patch.dict(sys.modules, {"pyautogui": pyautogui}), patch(
        "syll.agent.gui_click.platform.system", return_value="Darwin"
    ):
        success, message = await executor.execute({"action": "KEY", "value": "cmd+h", "position": [0, 0]})

    assert success is True
    pyautogui.hotkey.assert_called_once_with("command", "h")
    pyautogui.press.assert_not_called()
    assert message == "Pressed: command+h"
