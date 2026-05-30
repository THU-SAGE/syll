"""PyQt6 desktop ghost mascot for Syll."""

from __future__ import annotations

import json
import signal
import sys
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer, QUrl
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

_HERE = Path(__file__).parent
_HTML_PATH = _HERE / "ghost.html"
_SVG_DIR = _HERE.parent / "web" / "static" / "ghost"
_PREFS_PATH = Path.home() / ".syll" / "ghost_prefs.json"

SIZES = {"S": 70, "M": 90, "L": 120}
DEFAULT_SIZE = "M"


class _GhostPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        pass


class GhostWindow(QWidget):
    """Transparent always-on-top ghost mascot window."""

    def __init__(self, syll_url: str | None = None):
        super().__init__()
        self._syll_url = syll_url.rstrip("/") if syll_url else None
        self._drag_pos: QPoint | None = None
        self._press_pos: QPoint = QPoint(0, 0)
        self._has_dragged = False
        self._poke_count = 0
        self._poll_timer: QTimer | None = None
        self._prev_state = "idle"
        self._size_key = DEFAULT_SIZE
        self._config_hash = ""
        self._notifications_enabled = True
        self._recorder_status: dict[str, object] = {"status": "idle"}
        self._last_recorder_state = "idle"
        self._tray_capture_action: QAction | None = None
        self._tray_start_capture_action: QAction | None = None
        self._tray_stop_capture_action: QAction | None = None

        prefs = self._load_prefs()
        self._size_key = prefs.get("size", DEFAULT_SIZE)
        ghost_size = SIZES.get(self._size_key, SIZES[DEFAULT_SIZE])

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFixedSize(ghost_size, ghost_size)

        screen = QApplication.primaryScreen().availableGeometry()
        saved_x = prefs.get("x")
        saved_y = prefs.get("y")
        if saved_x is not None and saved_y is not None:
            self.move(int(saved_x), int(saved_y))
        else:
            self.move(screen.width() - ghost_size - 40, screen.height() - ghost_size - 20)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._web = QWebEngineView(self)
        page = _GhostPage(self._web)
        page.setBackgroundColor(QColor(0, 0, 0, 0))
        self._web.setPage(page)
        self._web.setStyleSheet("background: transparent;")
        self._web.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout.addWidget(self._web)

        self._web.installEventFilter(self)
        self._web.loadFinished.connect(self._install_child_event_filter)

        html_content = _HTML_PATH.read_text(encoding="utf-8")
        base_url = QUrl.fromLocalFile(str(_SVG_DIR) + "/")
        self._web.setHtml(html_content, base_url)

        if self._syll_url:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll_remote_state)
            self._poll_timer.start(2000)

        self._js_sync_timer = QTimer(self)
        self._js_sync_timer.timeout.connect(self._sync_js_state)
        self._js_sync_timer.start(1000)

        self._setup_tray()

    def _install_child_event_filter(self, ok: bool):
        """QWebEngineView creates a child render widget after load."""
        for child in self._web.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj, event):
        event_type = event.type()

        if event_type == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_pos = event.globalPosition().toPoint() - self.pos()
                self._press_pos = event.globalPosition().toPoint()
                self._has_dragged = False
                return True
            if event.button() == Qt.MouseButton.RightButton:
                self._show_context_menu(event.globalPosition().toPoint())
                return True

        if event_type == QEvent.Type.MouseMove:
            if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
                cur = event.globalPosition().toPoint()
                if (cur - self._press_pos).manhattanLength() > 4:
                    self._has_dragged = True
                self.move(cur - self._drag_pos)
                return True

        if event_type == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
                was_drag = self._has_dragged
                self._drag_pos = None
                if was_drag:
                    self._save_prefs()
                    if self._prev_state == "sleeping":
                        self.set_state("idle")
                else:
                    if self._prev_state == "sleeping":
                        self._poke_count += 1
                        if self._poke_count >= 2:
                            self._poke_count = 0
                            self.set_state("idle")
                        else:
                            self._poke_sleeping("!")
                            QTimer.singleShot(1500, self._reset_poke_count)
                    else:
                        self._poke_count = 0
                return True

        if event_type == QEvent.Type.Enter:
            if self._prev_state == "sleeping":
                self._poke_sleeping("?")
            return False

        if event_type == QEvent.Type.MouseButtonDblClick:
            if event.button() == Qt.MouseButton.LeftButton:
                self._open_web_ui()
                return True

        return False

    def _show_context_menu(self, pos: QPoint):
        menu = QMenu()

        if self._prev_state == "sleeping":
            wake_action = QAction("Wake Up", self)
            wake_action.triggered.connect(lambda: self.set_state("idle"))
            menu.addAction(wake_action)
            menu.addSeparator()

        if self._syll_url:
            open_action = QAction("Open Web UI", self)
            open_action.triggered.connect(self._open_web_ui)
            menu.addAction(open_action)

            workbench_action = QAction("Open Capture", self)
            workbench_action.triggered.connect(self._open_capture_workbench)
            menu.addAction(workbench_action)
            menu.addSeparator()

            recorder_state = str(self._recorder_status.get("status", "idle"))
            start_action = QAction("Start Capture", self)
            start_action.setEnabled(recorder_state not in {"recording", "starting"})
            start_action.triggered.connect(self._start_capture)
            menu.addAction(start_action)

            stop_action = QAction("Stop Capture", self)
            stop_action.setEnabled(recorder_state in {"recording", "starting"})
            stop_action.triggered.connect(self._stop_capture)
            menu.addAction(stop_action)
            menu.addSeparator()

        size_menu = menu.addMenu("Size")
        for key, px in SIZES.items():
            label = f"{key} ({px}px)"
            if key == self._size_key:
                label += "  *"
            action = QAction(label, self)
            action.triggered.connect(lambda checked, k=key: self._set_size(k))
            size_menu.addAction(action)

        menu.addSeparator()

        top_action = QAction("Always on Top", self)
        top_action.setCheckable(True)
        top_action.setChecked(bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        top_action.triggered.connect(self._toggle_always_on_top)
        menu.addAction(top_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)

        menu.exec(pos)

    def _open_web_ui(self):
        if self._syll_url:
            webbrowser.open(self._syll_url)

    def _open_capture_workbench(self):
        if self._syll_url:
            webbrowser.open(f"{self._syll_url}/?tab=demo&view=record")

    def _toggle_always_on_top(self, checked: bool):
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _set_size(self, key: str):
        if key == self._size_key:
            return
        self._size_key = key
        self._resize_cleanly(SIZES[key])
        self._save_prefs()

    def _resize_cleanly(self, px: int):
        """Resize transparent window cleanly on macOS by hiding + repositioning."""
        was_visible = self.isVisible()
        pos = self.pos()
        self.hide()
        self.setFixedSize(px, px)
        self.move(pos)
        if was_visible:
            self.show()
        self._web.page().runJavaScript("ghostReload()")

    def _show_toast(self, text: str, color: str = "#2dd4a8"):
        """Show a short status toast inside the ghost window."""
        payload_text = json.dumps(text)
        payload_color = json.dumps(color)
        self._web.page().runJavaScript(
            f"ghostToast({payload_text}, {payload_color})"
        )

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(200, 200, 255))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 2, 24, 20)
        painter.drawRect(4, 12, 24, 14)
        painter.setBrush(QColor(40, 40, 40))
        painter.drawEllipse(10, 8, 4, 5)
        painter.drawEllipse(18, 8, 4, 5)
        painter.end()

        self._tray.setIcon(QIcon(pixmap))
        self._tray.setToolTip("Syll")

        menu = QMenu()

        self._tray_info_action = QAction("syll ghost", self)
        self._tray_info_action.setEnabled(False)
        menu.addAction(self._tray_info_action)

        if self._syll_url:
            self._tray_capture_action = QAction("capture: idle", self)
            self._tray_capture_action.setEnabled(False)
            menu.addAction(self._tray_capture_action)
            menu.addSeparator()
        else:
            menu.addSeparator()

        show_action = QAction("Show / Hide", self)
        show_action.triggered.connect(self._toggle_visibility)
        menu.addAction(show_action)

        if self._syll_url:
            open_action = QAction("Open Web UI", self)
            open_action.triggered.connect(self._open_web_ui)
            menu.addAction(open_action)

            workbench_action = QAction("Open Capture", self)
            workbench_action.triggered.connect(self._open_capture_workbench)
            menu.addAction(workbench_action)
            menu.addSeparator()

            self._tray_start_capture_action = QAction("Start Capture", self)
            self._tray_start_capture_action.triggered.connect(self._start_capture)
            menu.addAction(self._tray_start_capture_action)

            self._tray_stop_capture_action = QAction("Stop Capture", self)
            self._tray_stop_capture_action.triggered.connect(self._stop_capture)
            menu.addAction(self._tray_stop_capture_action)
            menu.addSeparator()

        size_menu = menu.addMenu("Size")
        for key, px in SIZES.items():
            action = QAction(f"{key} ({px}px)", self)
            action.triggered.connect(lambda checked, k=key: self._set_size(k))
            size_menu.addAction(action)

        menu.addSeparator()

        state_menu = menu.addMenu("Debug: State")
        for state in ("idle", "working", "sleeping", "error"):
            action = QAction(state, self)
            action.triggered.connect(lambda checked, s=state: self.set_state(s))
            state_menu.addAction(action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        self._tray.show()
        self._update_capture_actions()

    def _toggle_visibility(self):
        self.setVisible(not self.isVisible())

    def set_state(self, state: str):
        """Update ghost state: idle, working, sleeping, error."""
        prev = self._prev_state
        if state != prev and self._notifications_enabled:
            if state == "idle" and prev == "working":
                self._show_toast("Done!", "#2dd4a8")
            elif state == "error":
                self._show_toast("Error!", "#ef4444")
        if state != "sleeping" and prev == "sleeping":
            self._web.page().runJavaScript("ghostWakeEffect()")
        self._prev_state = state
        self._web.page().runJavaScript(f"ghostUpdateState('{state}')")
        if state != prev:
            self._web.update()
            self.update()

    def _poke_sleeping(self, symbol: str = "!"):
        """Shake the ghost and show a bubble without waking."""
        safe = symbol.replace("'", "\\'")
        self._web.page().runJavaScript(f"ghostPoke('{safe}')")

    def _reset_poke_count(self):
        self._poke_count = 0

    def _sync_js_state(self):
        """JS owns auto-sleep transitions; query it back into Python."""
        self._web.page().runJavaScript("ghostState", self._on_js_state)

    def _on_js_state(self, state):
        if state and isinstance(state, str) and state != self._prev_state:
            self._prev_state = state

    def _poll_remote_state(self):
        if not self._syll_url:
            return
        self._poll_agent_state()
        self._sync_config_from_api()
        self._sync_recorder_status()

    def _poll_agent_state(self):
        try:
            data = self._fetch_json("/api/v1/agent/activity")
            state = data.get("state", "idle")
            self.set_state(state)

            detail = data.get("detail", "")
            today = data.get("today_count")
            uptime = data.get("uptime_minutes")
            parts = []
            if today is not None:
                parts.append(f"Today: {today} msgs")
            if uptime is not None:
                hours, mins = divmod(int(uptime), 60)
                parts.append(f"Up: {hours}h{mins:02d}m" if hours else f"Up: {mins}m")
            if detail:
                parts.append(detail)
            self._tray_info_action.setText(" | ".join(parts) if parts else "syll ghost")
        except Exception:
            self._tray_info_action.setText("Syll: offline")

    def _sync_config_from_api(self):
        """Fetch config from the canonical route, then fall back to legacy."""
        import hashlib

        raw = None
        for path in ("/api/v1/syll/config", "/api/v1/ghost/config"):
            try:
                raw = self._fetch_raw(path)
                break
            except Exception:
                continue
        if raw is None:
            return

        new_hash = hashlib.md5(raw.encode("utf-8")).hexdigest()
        if new_hash == self._config_hash:
            return
        self._config_hash = new_hash
        self._apply_remote_config(json.loads(raw))

    def _apply_remote_config(self, cfg: dict):
        new_size = cfg.get("size", self._size_key)
        if new_size != self._size_key and new_size in SIZES:
            self._size_key = new_size
            self._resize_cleanly(SIZES[new_size])

        always_on_top = cfg.get("always_on_top")
        if always_on_top is not None:
            flags = self.windowFlags()
            has_aot = bool(flags & Qt.WindowType.WindowStaysOnTopHint)
            if always_on_top != has_aot:
                if always_on_top:
                    flags |= Qt.WindowType.WindowStaysOnTopHint
                else:
                    flags &= ~Qt.WindowType.WindowStaysOnTopHint
                self.setWindowFlags(flags)
                self.show()

        self._notifications_enabled = cfg.get("notifications_enabled", True)
        js_cfg = json.dumps(
            {
                "state_svg_map": cfg.get("state_svg_map", {}),
                "auto_sleep_seconds": cfg.get("auto_sleep_seconds", 60),
                "error_reset_seconds": cfg.get("error_reset_seconds", 5),
            }
        )
        self._web.page().runJavaScript(f"ghostSetConfig({js_cfg})")

    def _sync_recorder_status(self):
        try:
            data = self._fetch_json("/api/v1/recorder/status")
        except Exception:
            self._recorder_status = {"status": "unavailable"}
            if self._tray_capture_action is not None:
                self._tray_capture_action.setText("capture: unavailable")
            self._update_capture_actions()
            return

        state = str(data.get("status", "idle"))
        project = str(data.get("project", "") or "")
        duration = int(float(data.get("duration_s", 0) or 0))
        mins, secs = divmod(duration, 60)

        if self._notifications_enabled:
            if state in {"recording", "starting"} and self._last_recorder_state not in {"recording", "starting"}:
                self._show_toast("REC", "#ef4444")
            elif state == "stopped" and self._last_recorder_state in {"recording", "starting"}:
                self._show_toast("Saved", "#2dd4a8")

        self._last_recorder_state = state
        self._recorder_status = data

        if self._tray_capture_action is not None:
            if state in {"recording", "starting"}:
                label = f"capture: {state} {mins:02d}:{secs:02d}"
                if project:
                    label += f" · {project}"
            elif state == "stopped":
                label = f"capture: ready · {project or 'latest'}"
            elif state == "error":
                label = "capture: error"
            else:
                label = "capture: idle"
            self._tray_capture_action.setText(label)

        self._update_capture_actions()

    def _update_capture_actions(self):
        state = str(self._recorder_status.get("status", "idle"))
        if self._tray_start_capture_action is not None:
            self._tray_start_capture_action.setEnabled(state not in {"recording", "starting"})
        if self._tray_stop_capture_action is not None:
            self._tray_stop_capture_action.setEnabled(state in {"recording", "starting"})

    def _default_capture_project(self) -> str:
        return datetime.now().strftime("capture-%Y%m%d-%H%M%S")

    def _start_capture(self):
        if not self._syll_url:
            return
        payload = {
            "project": self._default_capture_project(),
            "fps": 15,
            "monitor": 0,
            "output_dir": None,
        }
        try:
            self._post_json("/api/v1/recorder/start", payload)
            self._show_toast("REC", "#ef4444")
            self._open_capture_workbench()
        except Exception:
            self._show_toast("Capture error", "#ef4444")

    def _stop_capture(self):
        if not self._syll_url:
            return
        try:
            self._post_json("/api/v1/recorder/stop", {})
            self._show_toast("Stopped", "#2dd4a8")
            self._open_capture_workbench()
        except Exception:
            self._show_toast("Stop failed", "#ef4444")

    def _fetch_raw(self, path: str) -> str:
        if not self._syll_url:
            raise RuntimeError("No Syll URL configured")
        url = f"{self._syll_url}{path}"
        with urllib.request.urlopen(url, timeout=0.6) as resp:
            return resp.read().decode("utf-8")

    def _fetch_json(self, path: str) -> dict:
        return json.loads(self._fetch_raw(path))

    def _post_json(self, path: str, payload: dict) -> dict:
        if not self._syll_url:
            raise RuntimeError("No Syll URL configured")
        url = f"{self._syll_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _load_prefs(self) -> dict:
        for path in (_PREFS_PATH,):
            try:
                return json.loads(path.read_text())
            except Exception:
                continue
        return {}

    def _save_prefs(self):
        prefs = {
            "x": self.pos().x(),
            "y": self.pos().y(),
            "size": self._size_key,
        }
        _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PREFS_PATH.write_text(json.dumps(prefs))


def run_ghost(port: int | None = None):
    """Entry point for the desktop ghost mascot."""
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Syll")

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    keepalive = QTimer()
    keepalive.start(500)
    keepalive.timeout.connect(lambda: None)

    syll_url = f"http://localhost:{port}" if port else None
    ghost = GhostWindow(syll_url=syll_url)
    ghost.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    run_ghost()
