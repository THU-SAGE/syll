"""RecordingSession — orchestrates screen capture + event listener."""

from __future__ import annotations

import datetime
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .event_listener import EventListener
from .screen_capture import ScreenCapture, ScreenInfo


@dataclass
class RecordingStats:
    """Post-recording summary."""

    duration_s: float
    event_count: int
    video_path: Path
    log_path: Path
    video_size: int  # bytes
    log_size: int  # bytes


class RecordingSession:
    """Record screen video + input events in the workflow-skill compatible format.

    Output structure::

        {output_dir}/
        ├── inputs/
        │   ├── {project_name}.mp4
        │   └── input_log_{project_name}.txt   (JSONL)
        └── (pipeline later adds screenshots/, *_trace.json, etc.)
    """

    def __init__(
        self,
        project_name: str,
        output_dir: str | Path | None = None,
        fps: int = 15,
        monitor_idx: int = 0,
    ):
        self.project_name = project_name
        self.fps = fps
        self.monitor_idx = monitor_idx

        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            self.output_dir = Path.home() / ".syll" / "recordings" / project_name

        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._running = False
        self._stop_time: float = 0.0

        self._capture: ScreenCapture | None = None
        self._listener: EventListener | None = None
        self._log_fh = None
        self._on_stop_cbs: list = []
        self._event_count = 0
        self._screen_info: ScreenInfo | None = None

    # -- public API -----------------------------------------------------------

    def on_stop(self, callback) -> None:
        """Register a callback invoked once when recording stops."""
        self._on_stop_cbs.append(callback)

    def start(self) -> ScreenInfo:
        """Start recording.  Returns ScreenInfo describing the captured display."""
        inputs = self.output_dir / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)

        video_path = inputs / f"{self.project_name}.mp4"
        log_path = inputs / f"input_log_{self.project_name}.txt"

        # 1. Screen capture (blocks until first frame)
        self._capture = ScreenCapture(str(video_path), fps=self.fps, monitor_idx=self.monitor_idx)
        info = self._capture.start()
        self._screen_info = info
        self._event_count = 0

        # 2. Open log and write header + CONFIG
        self._log_fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        self._write_header()
        self._write_config(info)

        # 3. Start event listener (uses capture's start_time for sync)
        self._listener = EventListener(
            callback=self._write_event,
            start_time=self._capture.start_time,
            on_stop=self.stop,
        )
        self._listener.start()
        self._running = True
        self._stopped.clear()
        return info

    def stop(self) -> None:
        """Stop recording idempotently."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._running = False
        self._stop_time = time.monotonic()

        if self._listener:
            self._listener.stop()
        if self._capture:
            self._capture.stop()

        with self._lock:
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None

        for cb in self._on_stop_cbs:
            try:
                cb()
            except Exception:
                pass

    def is_running(self) -> bool:
        return self._running

    def reset(self) -> None:
        """Reset so the session can be started again (new recording)."""
        self.stop()
        self._stopped.clear()
        self._running = False
        self._stop_time = 0.0
        self._capture = None
        self._listener = None
        self._log_fh = None
        self._on_stop_cbs.clear()
        self._event_count = 0
        self._screen_info = None

    def event_count(self) -> int:
        """Return the number of recorded user events (excluding CONFIG)."""
        return self._event_count

    def screen_info(self) -> ScreenInfo | None:
        """Return the captured display info once recording has started."""
        return self._screen_info

    def start_monotonic(self) -> float:
        """Return the capture start timestamp for live duration tracking."""
        if self._capture:
            return self._capture.start_time
        return 0.0

    def screen_info_dict(self) -> dict | None:
        """Return the captured display info as a plain dict."""
        if self._screen_info is None:
            return None
        return asdict(self._screen_info)

    def stats(self) -> RecordingStats:
        """Compute post-recording statistics.  Call after stop()."""
        inputs = self.output_dir / "inputs"
        vp = inputs / f"{self.project_name}.mp4"
        lp = inputs / f"input_log_{self.project_name}.txt"

        duration = 0.0
        if self._capture and self._capture.start_time and self._stop_time:
            duration = self._stop_time - self._capture.start_time

        event_count = 0
        if lp.exists():
            with open(lp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and line != "":
                        event_count += 1
            event_count = max(0, event_count - 1)  # exclude CONFIG line

        return RecordingStats(
            duration_s=max(0.0, duration),
            event_count=event_count,
            video_path=vp,
            log_path=lp,
            video_size=vp.stat().st_size if vp.exists() else 0,
            log_size=lp.stat().st_size if lp.exists() else 0,
        )

    # -- internals ------------------------------------------------------------

    def _write_header(self) -> None:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        lines = [
            "# Input Recording Log",
            f"# Started: {now}",
            "# Recorder: syll-recorder",
            "# Format: JSON per line",
            "# Fields: timestamp, message, window",
            "",
        ]
        self._log_fh.write("\n".join(lines) + "\n")

    def _write_config(self, info: ScreenInfo) -> None:
        config_payload = {
            "0": {
                "x0": info.x0,
                "y0": info.y0,
                "width": info.logical_width,
                "height": info.logical_height,
                "scale_factor": 1.0,
            }
        }
        event = {
            "timestamp": "00:00:00.000",
            "message": json.dumps(config_payload),
            "window": "System Info",
        }
        self._log_fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._log_fh.flush()

    def _write_event(self, event: dict) -> None:
        with self._lock:
            if self._running and self._log_fh:
                self._event_count += 1
                self._log_fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._log_fh.flush()
