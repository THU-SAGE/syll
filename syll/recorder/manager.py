"""Thread-safe recorder session manager for the web UI."""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class RecorderConflict(RuntimeError):
    """Raised when an operation conflicts with the active recorder state."""


class RecorderManager:
    """Manage a single active RecordingSession and expose status snapshots."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session = None
        self._state = "idle"
        self._project = ""
        self._output_dir = ""
        self._fps = 15
        self._monitor = 0
        self._screen_info: dict[str, Any] | None = None
        self._summary: dict[str, Any] | None = None
        self._error = ""
        self._version = 0
        self._updated_at = time.time()

    def start(
        self,
        project: str,
        output_dir: str | None = None,
        fps: int = 15,
        monitor: int = 0,
    ) -> dict[str, Any]:
        """Create and start a new recording session."""
        project = (project or "").strip()
        if not project:
            raise ValueError("project is required")

        with self._lock:
            if self._state in {"starting", "recording"}:
                raise RecorderConflict("A recording session is already active")

            session = self._build_session(
                project=project,
                output_dir=output_dir,
                fps=fps,
                monitor=monitor,
            )
            session.on_stop(self._handle_session_stop)
            self._session = session
            self._project = project
            self._output_dir = str(session.output_dir)
            self._fps = fps
            self._monitor = monitor
            self._screen_info = None
            self._summary = None
            self._error = ""
            self._state = "starting"
            self._touch_locked()

        try:
            info = session.start()
        except Exception as exc:
            try:
                session.stop()
            except Exception:
                pass
            with self._lock:
                if self._session is session:
                    self._state = "error"
                    self._summary = None
                    self._screen_info = None
                    self._error = str(exc)
                    self._touch_locked()
            raise RuntimeError(str(exc)) from exc

        with self._lock:
            if self._session is session:
                self._state = "recording"
                self._screen_info = session.screen_info_dict() or self._serialize_screen_info(info)
                self._error = ""
                self._touch_locked()

        return self.get_status()

    def stop(self) -> dict[str, Any]:
        """Stop the active recording session and return the latest snapshot."""
        with self._lock:
            session = self._session
            state = self._state

        if session is None or state not in {"starting", "recording"}:
            raise RecorderConflict("No active recording session to stop")

        session.stop()
        return self.get_status()

    def get_status(self) -> dict[str, Any]:
        """Return the latest recorder snapshot."""
        with self._lock:
            session = self._session
            state = self._state
            project = self._project
            output_dir = self._output_dir
            fps = self._fps
            monitor = self._monitor
            screen_info = dict(self._screen_info) if self._screen_info else None
            summary = dict(self._summary) if self._summary else None
            error = self._error
            version = self._version
            updated_at = self._updated_at

        duration_s = 0.0
        event_count = 0
        if state == "recording" and session is not None:
            started = session.start_monotonic()
            if started:
                duration_s = max(0.0, time.monotonic() - started)
            event_count = session.event_count()
        elif summary:
            duration_s = float(summary.get("duration_s", 0.0))
            event_count = int(summary.get("event_count", 0))

        return {
            "status": state,
            "project": project,
            "output_dir": output_dir,
            "fps": fps,
            "monitor": monitor,
            "screen_info": screen_info,
            "duration_s": duration_s,
            "event_count": event_count,
            "summary": summary,
            "error": error,
            "version": version,
            "updated_at": updated_at,
        }

    def get_project_path(self) -> Path:
        """Return the most recent recording project directory."""
        with self._lock:
            if self._state != "stopped" or not self._output_dir:
                raise RecorderConflict("No completed recording is available to import")
            return Path(self._output_dir)

    def open_folder(self) -> dict[str, Any]:
        """Open the current or last recording folder in the OS file browser."""
        with self._lock:
            output_dir = self._output_dir

        if not output_dir:
            raise RecorderConflict("No recording folder is available yet")

        _open_folder(Path(output_dir))
        return self.get_status()

    def _handle_session_stop(self) -> None:
        """Capture a summary when the session stops, including hotkey stops."""
        with self._lock:
            session = self._session

        if session is None:
            return

        summary = self._serialize_summary(session.stats())

        with self._lock:
            if self._session is not session:
                return
            self._summary = summary
            if self._screen_info is None:
                self._screen_info = session.screen_info_dict()
            if self._state != "error":
                self._state = "stopped"
                self._error = ""
            self._touch_locked()

    def _build_session(
        self,
        project: str,
        output_dir: str | None,
        fps: int,
        monitor: int,
    ):
        # Lazy import so the web app still boots even if recorder deps are missing.
        from syll.recorder.core import RecordingSession

        return RecordingSession(
            project_name=project,
            output_dir=output_dir,
            fps=fps,
            monitor_idx=monitor,
        )

    def _touch_locked(self) -> None:
        self._version += 1
        self._updated_at = time.time()

    @staticmethod
    def _serialize_screen_info(info) -> dict[str, Any]:
        return {
            "logical_width": info.logical_width,
            "logical_height": info.logical_height,
            "physical_width": info.physical_width,
            "physical_height": info.physical_height,
            "scale_factor": info.scale_factor,
            "x0": info.x0,
            "y0": info.y0,
        }

    @staticmethod
    def _serialize_summary(stats) -> dict[str, Any]:
        return {
            "duration_s": stats.duration_s,
            "event_count": stats.event_count,
            "video_path": str(stats.video_path),
            "log_path": str(stats.log_path),
            "video_size": stats.video_size,
            "log_size": stats.log_size,
        }


def _open_folder(path: Path) -> None:
    """Reveal a path in the native file browser."""
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])
