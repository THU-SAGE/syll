"""Small startup profiler for ``syll wake``.

The ``sml`` suffix is intentional: startup-speed diagnostics use it consistently
for helper modules, environment variables, and output artifacts.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

STARTUP_PROFILE_ENV_SML = "SYLL_STARTUP_PROFILE_SML"
STARTUP_PROFILE_OUTPUT_ENV_SML = "SYLL_STARTUP_PROFILE_OUTPUT_SML"
STARTUP_PROFILE_OUTPUT_FILE_SML = "startup_profile_sml.json"

ClockSml = Callable[[], float]


def _enabled_from_env_sml(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class StartupProfilerSml:
    """Collect lightweight wake startup timings behind an env flag."""

    def __init__(
        self,
        *,
        enabled: bool,
        output_path: Path | None = None,
        clock: ClockSml = time.perf_counter,
    ):
        self.enabled = enabled
        self.output_path = output_path or Path(STARTUP_PROFILE_OUTPUT_FILE_SML)
        self._clock = clock
        self.started_at_sml = self._clock()
        self.phases_sml: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls) -> "StartupProfilerSml":
        enabled = _enabled_from_env_sml(os.environ.get(STARTUP_PROFILE_ENV_SML))
        output = os.environ.get(STARTUP_PROFILE_OUTPUT_ENV_SML)
        return cls(enabled=enabled, output_path=Path(output) if output else None)

    def start_phase_sml(self) -> float:
        return self._clock()

    def record_sml(self, name: str, phase_started_at_sml: float) -> None:
        if not self.enabled:
            return
        now_sml = self._clock()
        self.phases_sml.append(
            {
                "name": name,
                "duration_sml": now_sml - phase_started_at_sml,
                "elapsed_sml": now_sml - self.started_at_sml,
            }
        )

    def write_sml(self) -> None:
        if not self.enabled:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "started_at_sml": self.started_at_sml,
            "total_sml": self._clock() - self.started_at_sml,
            "phases": self.phases_sml,
        }
        self.output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
