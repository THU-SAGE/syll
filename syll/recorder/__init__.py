"""Recorder package with lazy exports so web mode can boot without deps."""

from __future__ import annotations

from typing import Any

__all__ = ["RecordingSession", "RecordingStats", "RecorderManager"]


def __getattr__(name: str) -> Any:
    if name in {"RecordingSession", "RecordingStats"}:
        from .core import RecordingSession, RecordingStats

        return {
            "RecordingSession": RecordingSession,
            "RecordingStats": RecordingStats,
        }[name]
    if name == "RecorderManager":
        from .manager import RecorderManager

        return RecorderManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
