"""Keyboard + mouse event listener (pynput) emitting workflow-compatible JSONL."""

from __future__ import annotations

import threading
import time
from typing import Callable

from pynput import keyboard, mouse

from .window_tracker import get_active_window

# ---------------------------------------------------------------------------
# Stop-hotkey definition: Ctrl + Shift + F5
# ---------------------------------------------------------------------------
_STOP_KEY = keyboard.Key.f5


class EventListener:
    """Capture OS-level input events and emit them in the recorder JSONL format.

    Events are delivered to *callback* as dicts::

        {"timestamp": "HH:MM:SS.mmm", "message": "...", "window": "..."}

    The listener also watches for **Ctrl+Shift+F5** and fires *on_stop*
    (filtering that combo out of the event log).
    """

    def __init__(
        self,
        callback: Callable[[dict], None],
        start_time: float,
        on_stop: Callable[[], None] | None = None,
        drag_threshold: int = 5,
        dblclick_threshold: float = 0.4,
    ):
        self._cb = callback
        self._t0 = start_time
        self._on_stop = on_stop
        self._drag_px = drag_threshold
        self._dbl_sec = dblclick_threshold

        # ── mouse state ──
        self._pressed: dict[str, tuple[int, int, float]] = {}
        self._dragging = False
        self._drag_btn: str | None = None
        self._drag_move_ts = 0.0
        self._last_release: dict[str, tuple[int, int, float]] = {}

        # ── keyboard state ──
        self._mods: set[str] = set()

        # ── threading ──
        self._lock = threading.Lock()
        self._ml: mouse.Listener | None = None
        self._kl: keyboard.Listener | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._ml = mouse.Listener(
            on_click=self._on_click,
            on_scroll=self._on_scroll,
            on_move=self._on_move,
        )
        self._kl = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._ml.start()
        self._kl.start()

    def stop(self) -> None:
        if self._ml:
            self._ml.stop()
        if self._kl:
            self._kl.stop()

    # -- helpers --------------------------------------------------------------

    def _ts(self) -> str:
        e = time.monotonic() - self._t0
        h = int(e // 3600)
        m = int((e % 3600) // 60)
        s = int(e % 60)
        ms = int((e % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    def _emit(self, message: str) -> None:
        self._cb({"timestamp": self._ts(), "message": message, "window": get_active_window()})

    @staticmethod
    def _btn(button) -> str:
        if button == mouse.Button.left:
            return "L"
        if button == mouse.Button.right:
            return "R"
        return "M"

    # ========================================================================
    # Mouse
    # ========================================================================

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        b = self._btn(button)
        x, y = int(x), int(y)

        with self._lock:
            if pressed:
                self._pressed[b] = (x, y, time.monotonic())
                self._dragging = False

                # double-click?
                prev = self._last_release.get(b)
                if prev:
                    px, py, pt = prev
                    if (time.monotonic() - pt < self._dbl_sec
                            and abs(x - px) <= 5 and abs(y - py) <= 5):
                        self._emit(f"{b}DblClick at ({x}, {y})")
                        self._last_release.pop(b, None)
                        return

                self._emit(f"{b}Click at ({x}, {y})")
            else:
                # release
                if self._dragging and self._drag_btn == b:
                    self._emit(f"{b}DragEnd at ({x}, {y})")
                    self._dragging = False
                    self._drag_btn = None
                else:
                    self._emit(f"{b}Release at ({x}, {y})")
                    self._last_release[b] = (x, y, time.monotonic())
                self._pressed.pop(b, None)

    def _on_move(self, x: int, y: int) -> None:
        with self._lock:
            if not self._pressed:
                return

            x, y = int(x), int(y)
            # Check the first pressed button only (most common: one button at a time)
            for b, (dx, dy, _) in self._pressed.items():
                if not self._dragging:
                    if max(abs(x - dx), abs(y - dy)) > self._drag_px:
                        self._dragging = True
                        self._drag_btn = b
                        self._emit(f"DragStart at ({dx}, {dy})")

                if self._dragging and b == self._drag_btn:
                    now = time.monotonic()
                    if now - self._drag_move_ts > 0.05:  # ~20 Hz throttle
                        self._emit(f"DragMove at ({x}, {y})")
                        self._drag_move_ts = now
                break

    def _on_scroll(self, x: int, y: int, _dx: int, dy: int) -> None:
        if dy == 0:
            return
        x, y = int(x), int(y)
        direction = "ScrollUp" if dy > 0 else "ScrollDown"
        for _ in range(max(1, abs(dy))):
            self._emit(f"{direction} at ({x}, {y})")

    # ========================================================================
    # Keyboard
    # ========================================================================

    _MOD_MAP: dict = {
        keyboard.Key.shift: "SHIFT",
        keyboard.Key.shift_l: "SHIFT",
        keyboard.Key.shift_r: "SHIFT",
        keyboard.Key.ctrl: "CTRL",
        keyboard.Key.ctrl_l: "CTRL",
        keyboard.Key.ctrl_r: "CTRL",
        keyboard.Key.alt: "ALT",
        keyboard.Key.alt_l: "ALT",
        keyboard.Key.alt_r: "ALT",
        keyboard.Key.cmd: "CMD",
        keyboard.Key.cmd_l: "CMD",
        keyboard.Key.cmd_r: "CMD",
    }

    _SPECIAL: dict = {
        keyboard.Key.enter: "ENTER",
        keyboard.Key.backspace: "BACKSPACE",
        keyboard.Key.delete: "DELETE",
        keyboard.Key.space: "SPACE",
        keyboard.Key.tab: "TAB",
        keyboard.Key.esc: "ESC",
        keyboard.Key.up: "UP",
        keyboard.Key.down: "DOWN",
        keyboard.Key.left: "LEFT",
        keyboard.Key.right: "RIGHT",
        keyboard.Key.home: "HOME",
        keyboard.Key.end: "END",
        keyboard.Key.page_up: "PAGE_UP",
        keyboard.Key.page_down: "PAGE_DOWN",
        keyboard.Key.f1: "F1",
        keyboard.Key.f2: "F2",
        keyboard.Key.f3: "F3",
        keyboard.Key.f4: "F4",
        keyboard.Key.f5: "F5",
        keyboard.Key.f6: "F6",
        keyboard.Key.f7: "F7",
        keyboard.Key.f8: "F8",
        keyboard.Key.f9: "F9",
        keyboard.Key.f10: "F10",
        keyboard.Key.f11: "F11",
        keyboard.Key.f12: "F12",
    }

    def _is_stop_hotkey(self, key) -> bool:
        return key == _STOP_KEY and "CTRL" in self._mods and "SHIFT" in self._mods

    def _key_name(self, key) -> str | None:
        if key in self._SPECIAL:
            return self._SPECIAL[key]
        if hasattr(key, "char") and key.char:
            c = key.char
            # Ctrl+letter arrives as control char (\x01..\x1a)
            if len(c) == 1 and ord(c) < 32 and "CTRL" in self._mods:
                return chr(ord(c) + 64)
            return c
        return None

    def _on_press(self, key) -> None:
        # Track modifier
        if key in self._MOD_MAP:
            self._mods.add(self._MOD_MAP[key])
            return

        # Stop hotkey — filter from log
        if self._is_stop_hotkey(key):
            if self._on_stop:
                self._on_stop()
            return

        name = self._key_name(key)
        if name is None:
            return

        if "CTRL" in self._mods:
            self._emit(f"Hotkey: CTRL+{name.upper()}")
        elif "SHIFT" in self._mods and len(name) == 1:
            self._emit(f"Hotkey: SHIFT+{name.upper()}")
        else:
            self._emit(f"Key Press: {name}")

    def _on_release(self, key) -> None:
        if key in self._MOD_MAP:
            self._mods.discard(self._MOD_MAP[key])
            return

        # Don't log the stop key release
        if key == _STOP_KEY:
            return

        name = self._key_name(key)
        if name is None:
            return

        self._emit(f"Key Release: {name}")
