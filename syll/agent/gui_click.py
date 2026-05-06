"""Shared click helpers for GUI execution backends."""

import asyncio
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from loguru import logger

CLICK_INTERVAL = 0.12
POST_CLICK_WAIT = 0.35
DEFAULT_EXTRA_CLICKS = 1
DESKTOP_APP_SELECTION_WAIT = 0.12
DESKTOP_APP_OPEN_WAIT = 0.5
MOUSE_DOWN_UP_INTERVAL = 0.03
DEFAULT_PRESS_DURATION = 1.0
DEFAULT_DRAG_DURATION = 0.5

_DOUBLE_CLICK_PATTERNS = (
    re.compile(r"\bdouble(?:\s|-|_)?click\b", re.IGNORECASE),
    re.compile(r"\bdouble tap\b", re.IGNORECASE),
    re.compile(r"双击"),
)

_ALWAYS_DOUBLE_PATTERNS = (
    re.compile(r"\bopen\b", re.IGNORECASE),
    re.compile(r"\blaunch\b", re.IGNORECASE),
    re.compile(r"\benter\b", re.IGNORECASE),
    re.compile(r"\bfocus\b", re.IGNORECASE),
    re.compile(r"\bactivate\b", re.IGNORECASE),
    re.compile(r"\bswitch\b", re.IGNORECASE),
    re.compile(r"打开"),
    re.compile(r"进入"),
    re.compile(r"聚焦"),
    re.compile(r"激活"),
    re.compile(r"切换"),
)

_TARGETED_DOUBLE_PATTERNS = (
    re.compile(r"\bselect\b", re.IGNORECASE),
    re.compile(r"选中"),
)

_TARGET_HINT_PATTERNS = (
    re.compile(r"\bapp\b", re.IGNORECASE),
    re.compile(r"\bapplication\b", re.IGNORECASE),
    re.compile(r"\bwindow\b", re.IGNORECASE),
    re.compile(r"\binput\b", re.IGNORECASE),
    re.compile(r"\btextbox\b", re.IGNORECASE),
    re.compile(r"\bsearch box\b", re.IGNORECASE),
    re.compile(r"\bdock\b", re.IGNORECASE),
    re.compile(r"应用"),
    re.compile(r"窗口"),
    re.compile(r"输入框"),
    re.compile(r"搜索框"),
)

_DESKTOP_HINT_PATTERNS = (
    re.compile(r"\bdesktop\b", re.IGNORECASE),
    re.compile(r"桌面"),
)

_ICON_HINT_PATTERNS = (
    re.compile(r"\bicon\b", re.IGNORECASE),
    re.compile(r"图标"),
)

_APP_BUNDLE_PATTERNS = (
    re.compile(r"\.app\b", re.IGNORECASE),
    re.compile(r"应用程序"),
)


class ClickDispatchError(RuntimeError):
    """Raised when GUI click dispatch cannot proceed safely."""


@dataclass
class ClickDiagnostics:
    """Execution diagnostics for one mouse action."""

    backend: str
    accessibility: str
    event_style: str
    frontmost_app: str | None = None
    note: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "click_backend": self.backend,
            "mac_accessibility": self.accessibility,
            "event_style": self.event_style,
            "frontmost_app": self.frontmost_app,
            "click_note": self.note,
        }


class BaseMouseBackend:
    """Common mouse backend interface."""

    name = "base"
    event_style = "unknown"

    async def move_to(self, x: int, y: int) -> None:
        raise NotImplementedError

    async def left_click_once(self, x: int, y: int, *, click_state: int = 1) -> None:
        raise NotImplementedError

    async def right_click(self, x: int, y: int) -> None:
        raise NotImplementedError

    async def drag(self, start: tuple[int, int], end: tuple[int, int], *, duration: float) -> None:
        raise NotImplementedError

    async def press(self, x: int, y: int, *, duration: float) -> None:
        raise NotImplementedError


class PyAutoGUIMouseBackend(BaseMouseBackend):
    """Fallback backend using pyautogui."""

    name = "pyautogui"

    def __init__(self, pyautogui: Any, style: str = "click"):
        self._pyautogui = pyautogui
        self.event_style = style

    async def move_to(self, x: int, y: int) -> None:
        self._pyautogui.moveTo(x, y)

    async def left_click_once(self, x: int, y: int, *, click_state: int = 1) -> None:
        if self.event_style == "down_up":
            self._pyautogui.mouseDown(x=x, y=y)
            await asyncio.sleep(MOUSE_DOWN_UP_INTERVAL)
            self._pyautogui.mouseUp(x=x, y=y)
            return
        self._pyautogui.click(x, y)

    async def right_click(self, x: int, y: int) -> None:
        if self.event_style == "down_up":
            self._pyautogui.mouseDown(x=x, y=y, button="right")
            await asyncio.sleep(MOUSE_DOWN_UP_INTERVAL)
            self._pyautogui.mouseUp(x=x, y=y, button="right")
            return
        self._pyautogui.rightClick(x, y)

    async def drag(self, start: tuple[int, int], end: tuple[int, int], *, duration: float) -> None:
        self._pyautogui.moveTo(start[0], start[1])
        self._pyautogui.drag(end[0] - start[0], end[1] - start[1], duration=duration)

    async def press(self, x: int, y: int, *, duration: float) -> None:
        self._pyautogui.mouseDown(x=x, y=y)
        await asyncio.sleep(duration)
        self._pyautogui.mouseUp(x=x, y=y)


class PynputMouseBackend(BaseMouseBackend):
    """Native backend using pynput on macOS."""

    name = "pynput"
    event_style = "native_click"

    def __init__(self):
        from pynput import mouse as pynput_mouse

        self._controller = pynput_mouse.Controller()
        self._button = pynput_mouse.Button

    async def move_to(self, x: int, y: int) -> None:
        self._controller.position = (x, y)

    async def left_click_once(self, x: int, y: int, *, click_state: int = 1) -> None:
        self._controller.position = (x, y)
        self._controller.click(self._button.left, 1)

    async def right_click(self, x: int, y: int) -> None:
        self._controller.position = (x, y)
        self._controller.click(self._button.right, 1)

    async def drag(self, start: tuple[int, int], end: tuple[int, int], *, duration: float) -> None:
        self._controller.position = start
        self._controller.press(self._button.left)
        await asyncio.sleep(MOUSE_DOWN_UP_INTERVAL)
        self._controller.position = end
        await asyncio.sleep(duration)
        self._controller.release(self._button.left)

    async def press(self, x: int, y: int, *, duration: float) -> None:
        self._controller.position = (x, y)
        self._controller.press(self._button.left)
        await asyncio.sleep(duration)
        self._controller.release(self._button.left)


class QuartzMouseBackend(BaseMouseBackend):
    """Native CGEvent backend for macOS."""

    name = "quartz"
    event_style = "cg_event"

    def __init__(self):
        import Quartz

        self._quartz = Quartz

    async def move_to(self, x: int, y: int) -> None:
        event = self._create_event(
            self._quartz.kCGEventMouseMoved,
            x,
            y,
            self._quartz.kCGMouseButtonLeft,
        )
        self._post(event)

    async def left_click_once(self, x: int, y: int, *, click_state: int = 1) -> None:
        await self.move_to(x, y)
        down = self._create_event(
            self._quartz.kCGEventLeftMouseDown,
            x,
            y,
            self._quartz.kCGMouseButtonLeft,
            click_state=click_state,
        )
        up = self._create_event(
            self._quartz.kCGEventLeftMouseUp,
            x,
            y,
            self._quartz.kCGMouseButtonLeft,
            click_state=click_state,
        )
        self._post(down)
        await asyncio.sleep(MOUSE_DOWN_UP_INTERVAL)
        self._post(up)

    async def right_click(self, x: int, y: int) -> None:
        await self.move_to(x, y)
        down = self._create_event(
            self._quartz.kCGEventRightMouseDown,
            x,
            y,
            self._quartz.kCGMouseButtonRight,
        )
        up = self._create_event(
            self._quartz.kCGEventRightMouseUp,
            x,
            y,
            self._quartz.kCGMouseButtonRight,
        )
        self._post(down)
        await asyncio.sleep(MOUSE_DOWN_UP_INTERVAL)
        self._post(up)

    async def drag(self, start: tuple[int, int], end: tuple[int, int], *, duration: float) -> None:
        await self.move_to(start[0], start[1])
        down = self._create_event(
            self._quartz.kCGEventLeftMouseDown,
            start[0],
            start[1],
            self._quartz.kCGMouseButtonLeft,
        )
        drag = self._create_event(
            self._quartz.kCGEventLeftMouseDragged,
            end[0],
            end[1],
            self._quartz.kCGMouseButtonLeft,
        )
        up = self._create_event(
            self._quartz.kCGEventLeftMouseUp,
            end[0],
            end[1],
            self._quartz.kCGMouseButtonLeft,
        )
        self._post(down)
        await asyncio.sleep(MOUSE_DOWN_UP_INTERVAL)
        self._post(drag)
        await asyncio.sleep(duration)
        self._post(up)

    async def press(self, x: int, y: int, *, duration: float) -> None:
        await self.move_to(x, y)
        down = self._create_event(
            self._quartz.kCGEventLeftMouseDown,
            x,
            y,
            self._quartz.kCGMouseButtonLeft,
        )
        up = self._create_event(
            self._quartz.kCGEventLeftMouseUp,
            x,
            y,
            self._quartz.kCGMouseButtonLeft,
        )
        self._post(down)
        await asyncio.sleep(duration)
        self._post(up)

    def _create_event(
        self,
        event_type: int,
        x: int,
        y: int,
        button: int,
        *,
        click_state: int | None = None,
    ):
        event = self._quartz.CGEventCreateMouseEvent(None, event_type, (x, y), button)
        if click_state is not None:
            self._quartz.CGEventSetIntegerValueField(
                event,
                self._quartz.kCGMouseEventClickState,
                click_state,
            )
        return event

    def _post(self, event: Any) -> None:
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, event)


def _intent_text(raw: dict[str, Any] | None = None) -> str:
    raw = raw or {}
    return "\n".join(
        str(raw.get(key, ""))
        for key in (
            "intent",
            "plan",
            "instruction",
            "description",
            "thought",
            "action_text",
        )
        if raw.get(key)
    )


def resolve_click_count(action_name: str, raw: dict[str, Any] | None = None) -> int:
    """Resolve how many clicks to perform for the current action."""
    raw = raw or {}

    explicit_count = raw.get("click_count")
    if explicit_count is not None:
        try:
            return max(1, int(explicit_count))
        except (TypeError, ValueError):
            pass

    normalized = action_name.upper()
    if normalized == "DOUBLE_CLICK":
        return 2
    if normalized == "TRIPLE_CLICK":
        return 3

    if normalized != "CLICK":
        return 1

    intent_text = _intent_text(raw)
    if not intent_text:
        return 1

    if any(pattern.search(intent_text) for pattern in _DOUBLE_CLICK_PATTERNS):
        return 2

    if any(pattern.search(intent_text) for pattern in _ALWAYS_DOUBLE_PATTERNS):
        return 2

    if any(pattern.search(intent_text) for pattern in _TARGETED_DOUBLE_PATTERNS):
        if any(pattern.search(intent_text) for pattern in _TARGET_HINT_PATTERNS):
            return 2

    return 1


def should_open_desktop_app_with_shortcut(
    action_name: str,
    raw: dict[str, Any] | None = None,
    *,
    os_name: str | None = None,
) -> bool:
    """Detect macOS desktop .app icon interactions that should use Cmd+O."""
    normalized = action_name.upper()
    if normalized not in {"CLICK", "DOUBLE_CLICK"}:
        return False

    platform_name = os_name or platform.system()
    if platform_name != "Darwin":
        return False

    intent_text = _intent_text(raw)
    if not intent_text:
        return False

    has_desktop = any(pattern.search(intent_text) for pattern in _DESKTOP_HINT_PATTERNS)
    has_app_bundle = any(pattern.search(intent_text) for pattern in _APP_BUNDLE_PATTERNS)
    has_icon = any(pattern.search(intent_text) for pattern in _ICON_HINT_PATTERNS)
    has_open_intent = (
        any(pattern.search(intent_text) for pattern in _DOUBLE_CLICK_PATTERNS)
        or any(pattern.search(intent_text) for pattern in _ALWAYS_DOUBLE_PATTERNS)
    )

    return has_desktop and has_app_bundle and (has_icon or has_open_intent)


def get_click_backend_name(config: Any | None = None) -> str:
    """Return configured click backend preference."""
    return str(getattr(config, "click_backend", "auto") or "auto").lower()


def get_mac_click_style(config: Any | None = None, *, os_name: str | None = None) -> str:
    """Return configured click style for macOS."""
    style = str(getattr(config, "mac_click_style", "auto") or "auto").lower()
    platform_name = os_name or platform.system()
    if style != "auto":
        return style
    if platform_name == "Darwin":
        return "down_up"
    return "click"


def should_preflight_permissions(config: Any | None = None) -> bool:
    """Return whether permission preflight should block execution."""
    value = getattr(config, "preflight_permissions", True)
    return bool(value)


_MODIFIER_ALIASES = {
    "cmd": "command",
    "command": "command",
    "meta": "command",
    "super": "command",
    "control": "ctrl",
    "ctrl": "ctrl",
    "ctl": "ctrl",
    "option": "alt",
    "opt": "alt",
    "alt": "alt",
    "shift": "shift",
    "fn": "fn",
}

_SINGLE_KEY_ALIASES = {
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "pgup": "pageup",
    "page up": "pageup",
    "pgdn": "pagedown",
    "page down": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def normalize_hotkey_sequence(spec: str, *, os_name: str | None = None) -> list[str]:
    """Normalize hotkey text into pyautogui key names.

    This fixes cases like ``cmd+h`` on macOS, where passing ``cmd`` through to
    ``pyautogui.hotkey`` can degrade into typing only ``h``.
    """
    raw = str(spec or "").strip()
    if not raw:
        return []

    platform_name = (os_name or platform.system()).lower()
    lowered = raw.lower().strip()

    if lowered in _SINGLE_KEY_ALIASES:
        return [_SINGLE_KEY_ALIASES[lowered]]

    if "+" in lowered:
        parts = [part.strip() for part in lowered.split("+") if part.strip()]
    elif "-" in lowered:
        parts = [part.strip() for part in lowered.split("-") if part.strip()]
    else:
        words = lowered.split()
        if len(words) >= 2 and all(word in _MODIFIER_ALIASES for word in words[:-1]):
            parts = words
        else:
            combined = " ".join(words)
            if combined in _SINGLE_KEY_ALIASES:
                return [_SINGLE_KEY_ALIASES[combined]]
            return [_normalize_hotkey_token(lowered, platform_name)]

    return [_normalize_hotkey_token(part, platform_name) for part in parts]



def _normalize_hotkey_token(token: str, platform_name: str) -> str:
    token = token.strip().lower()
    if not token:
        return token
    if token in _MODIFIER_ALIASES:
        mapped = _MODIFIER_ALIASES[token]
        if mapped == "command" and platform_name != "darwin":
            return "win"
        return mapped
    return _SINGLE_KEY_ALIASES.get(token, token)


def detect_mac_accessibility_status(os_name: str | None = None) -> str:
    """Return macOS accessibility trust status."""
    platform_name = os_name or platform.system()
    if platform_name != "Darwin":
        return "not_applicable"

    try:
        import Quartz

        return "authorized" if bool(Quartz.AXIsProcessTrusted()) else "denied"
    except Exception as exc:
        logger.debug(f"Quartz accessibility detection unavailable: {exc}")

    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore

        return "authorized" if bool(AXIsProcessTrusted()) else "denied"
    except Exception as exc:
        logger.debug(f"ApplicationServices accessibility detection unavailable: {exc}")
        return "unknown"


def detect_frontmost_app(os_name: str | None = None) -> str | None:
    """Best-effort lookup for the active application."""
    platform_name = os_name or platform.system()
    if platform_name != "Darwin":
        return None

    try:
        from AppKit import NSWorkspace  # type: ignore

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        name = app.localizedName() if app else None
        if name:
            return str(name)
    except Exception as exc:
        logger.debug(f"AppKit frontmost app lookup unavailable: {exc}")

    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        name = result.stdout.strip()
        if result.returncode == 0 and name:
            return name
    except Exception as exc:
        logger.debug(f"osascript frontmost app lookup unavailable: {exc}")

    return None


def attach_click_diagnostics(raw: dict[str, Any] | None, diagnostics: ClickDiagnostics) -> None:
    """Persist diagnostics into an action dict for planner feedback."""
    if raw is None:
        return
    raw.update(diagnostics.to_metadata())


def format_diagnostic_message(message: str, diagnostics: ClickDiagnostics) -> str:
    """Append backend diagnostics to an execution message."""
    frontmost = diagnostics.frontmost_app or "unknown"
    return (
        f"{message} "
        f"[backend={diagnostics.backend}, accessibility={diagnostics.accessibility}, "
        f"event_style={diagnostics.event_style}, frontmost={frontmost}]"
    )


def _permission_failure_message(diagnostics: ClickDiagnostics) -> str:
    return format_diagnostic_message(
        "macOS Accessibility permission is not authorized; GUI click dispatch is blocked",
        diagnostics,
    )


def _candidate_backends(preferred: str, os_name: str) -> list[str]:
    if os_name != "Darwin":
        return ["pyautogui"]
    if preferred in {"quartz", "pynput", "pyautogui"}:
        return [preferred]
    return ["quartz", "pynput", "pyautogui"]


def _build_backend(
    name: str,
    pyautogui: Any,
    *,
    config: Any | None = None,
) -> BaseMouseBackend:
    if name == "quartz":
        return QuartzMouseBackend()
    if name == "pynput":
        return PynputMouseBackend()
    style = get_mac_click_style(config)
    return PyAutoGUIMouseBackend(pyautogui, style=style)


def get_mouse_backend(
    pyautogui: Any,
    *,
    config: Any | None = None,
    os_name: str | None = None,
) -> tuple[BaseMouseBackend, ClickDiagnostics]:
    """Resolve the active mouse backend plus execution diagnostics."""
    platform_name = os_name or platform.system()
    preferred = get_click_backend_name(config)
    accessibility = detect_mac_accessibility_status(platform_name)
    frontmost_app = detect_frontmost_app(platform_name)
    note: str | None = None

    for backend_name in _candidate_backends(preferred, platform_name):
        try:
            backend = _build_backend(backend_name, pyautogui, config=config)
            diagnostics = ClickDiagnostics(
                backend=backend.name,
                accessibility=accessibility,
                event_style=backend.event_style,
                frontmost_app=frontmost_app,
                note=note,
            )
            return backend, diagnostics
        except Exception as exc:
            logger.debug(f"Mouse backend '{backend_name}' unavailable: {exc}")
            note = f"{backend_name} unavailable: {exc}"

    backend = PyAutoGUIMouseBackend(pyautogui, style=get_mac_click_style(config, os_name=platform_name))
    diagnostics = ClickDiagnostics(
        backend=backend.name,
        accessibility=accessibility,
        event_style=backend.event_style,
        frontmost_app=frontmost_app,
        note=note,
    )
    return backend, diagnostics


def _ensure_click_allowed(diagnostics: ClickDiagnostics, config: Any | None = None) -> None:
    if diagnostics.accessibility == "denied" and should_preflight_permissions(config):
        raise ClickDispatchError(_permission_failure_message(diagnostics))


async def open_desktop_app_with_shortcut(
    pyautogui: Any,
    x: int,
    y: int,
    *,
    raw: dict[str, Any] | None = None,
    config: Any | None = None,
    selection_wait: float = DESKTOP_APP_SELECTION_WAIT,
    post_wait: float = DESKTOP_APP_OPEN_WAIT,
) -> str:
    """Select a desktop app icon, then open it with Cmd+O on macOS."""
    backend, diagnostics = get_mouse_backend(pyautogui, config=config)
    attach_click_diagnostics(raw, diagnostics)
    _ensure_click_allowed(diagnostics, config)

    previous_pause = getattr(pyautogui, "PAUSE", None)
    try:
        if previous_pause is not None:
            pyautogui.PAUSE = 0
        await backend.left_click_once(x, y, click_state=1)
        await asyncio.sleep(selection_wait)
        pyautogui.hotkey("command", "o")
        await asyncio.sleep(post_wait)
    finally:
        if previous_pause is not None:
            pyautogui.PAUSE = previous_pause

    return format_diagnostic_message(
        f"Selected desktop app at ({x}, {y}) and opened with Command+O",
        diagnostics,
    )


async def perform_click_sequence(
    pyautogui: Any,
    x: int,
    y: int,
    count: int = 1,
    *,
    raw: dict[str, Any] | None = None,
    config: Any | None = None,
    interval: float = CLICK_INTERVAL,
    post_wait: float = POST_CLICK_WAIT,
) -> str:
    """Perform explicit click sequences through the resolved backend."""
    backend, diagnostics = get_mouse_backend(pyautogui, config=config)
    attach_click_diagnostics(raw, diagnostics)
    _ensure_click_allowed(diagnostics, config)

    normalized_count = max(1, int(count))
    if normalized_count == 1:
        normalized_count += DEFAULT_EXTRA_CLICKS

    for idx in range(normalized_count):
        await backend.left_click_once(x, y, click_state=idx + 1)
        if idx < normalized_count - 1:
            await asyncio.sleep(interval)

    await asyncio.sleep(post_wait)

    if normalized_count == 1:
        base = f"Clicked at ({x}, {y})"
    elif normalized_count == 2:
        base = f"Double-clicked at ({x}, {y})"
    elif normalized_count == 3:
        base = f"Triple-clicked at ({x}, {y})"
    else:
        base = f"Clicked {normalized_count} times at ({x}, {y})"
    return format_diagnostic_message(base, diagnostics)


async def perform_right_click(
    pyautogui: Any,
    x: int,
    y: int,
    *,
    raw: dict[str, Any] | None = None,
    config: Any | None = None,
    post_wait: float = POST_CLICK_WAIT,
) -> str:
    """Perform a right click through the resolved backend."""
    backend, diagnostics = get_mouse_backend(pyautogui, config=config)
    attach_click_diagnostics(raw, diagnostics)
    _ensure_click_allowed(diagnostics, config)
    await backend.right_click(x, y)
    await asyncio.sleep(post_wait)
    return format_diagnostic_message(f"Right-clicked at ({x}, {y})", diagnostics)


async def perform_move(
    pyautogui: Any,
    x: int,
    y: int,
    *,
    raw: dict[str, Any] | None = None,
    config: Any | None = None,
) -> str:
    """Move the pointer through the resolved backend."""
    backend, diagnostics = get_mouse_backend(pyautogui, config=config)
    attach_click_diagnostics(raw, diagnostics)
    await backend.move_to(x, y)
    return format_diagnostic_message(f"Moved to ({x}, {y})", diagnostics)


async def perform_drag(
    pyautogui: Any,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    raw: dict[str, Any] | None = None,
    config: Any | None = None,
    duration: float = DEFAULT_DRAG_DURATION,
) -> str:
    """Drag via the resolved backend."""
    backend, diagnostics = get_mouse_backend(pyautogui, config=config)
    attach_click_diagnostics(raw, diagnostics)
    _ensure_click_allowed(diagnostics, config)
    await backend.drag(start, end, duration=duration)
    await asyncio.sleep(POST_CLICK_WAIT)
    return format_diagnostic_message(f"Dragged from {start} to {end}", diagnostics)


async def perform_press(
    pyautogui: Any,
    x: int,
    y: int,
    *,
    raw: dict[str, Any] | None = None,
    config: Any | None = None,
    duration: float = DEFAULT_PRESS_DURATION,
) -> str:
    """Press-and-hold via the resolved backend."""
    backend, diagnostics = get_mouse_backend(pyautogui, config=config)
    attach_click_diagnostics(raw, diagnostics)
    _ensure_click_allowed(diagnostics, config)
    await backend.press(x, y, duration=duration)
    await asyncio.sleep(POST_CLICK_WAIT)
    return format_diagnostic_message(f"Pressed at ({x}, {y})", diagnostics)
