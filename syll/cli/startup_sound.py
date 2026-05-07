"""Non-blocking startup sound playback for ``syll wake``."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_STARTUP_SOUND = Path(__file__).resolve().parents[1] / "assets" / "startup_sound.wav"


def resolve_startup_sound_path(configured_path: str | None) -> Path:
    """Return the configured sound path, or the packaged default when empty."""
    if configured_path and configured_path.strip():
        return Path(configured_path).expanduser()
    return DEFAULT_STARTUP_SOUND


def _player_command(sound_path: Path, platform: str) -> list[str] | None:
    if platform == "darwin":
        player = shutil.which("afplay")
        return [player, str(sound_path)] if player else None

    if platform.startswith("linux"):
        for command in ("paplay", "pw-play", "aplay"):
            player = shutil.which(command)
            if player:
                return [player, str(sound_path)]

        ffplay = shutil.which("ffplay")
        if ffplay:
            return [
                ffplay,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                str(sound_path),
            ]

    return None


def play_startup_sound(sound_config: Any, *, platform: str | None = None) -> None:
    """Play the startup sound once without blocking the wake flow."""
    if not getattr(sound_config, "enabled", True):
        return

    sound_path = resolve_startup_sound_path(getattr(sound_config, "path", ""))
    if not sound_path.exists():
        logger.warning(f"Startup sound not found: {sound_path}")
        return

    runtime_platform = platform or sys.platform

    try:
        if runtime_platform == "win32":
            import winsound

            winsound.PlaySound(str(sound_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return

        command = _player_command(sound_path, runtime_platform)
        if command is None:
            logger.debug(f"No startup sound player found for platform: {runtime_platform}")
            return

        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.debug(f"Startup sound playback skipped: {exc}")
