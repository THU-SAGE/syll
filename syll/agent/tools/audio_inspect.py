"""Audio inspection tool for comparing local audio files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from syll.agent.tools.base import Tool
from syll.audio.metrics import compare_audio, inspect_audio


class AudioInspectTool(Tool):
    """Inspect one audio file or compare before/after WAV files."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir.resolve() if allowed_dir else None

    @property
    def name(self) -> str:
        return "audio_inspect"

    @property
    def description(self) -> str:
        return (
            "Inspect a normalized WAV audio file, or compare before/after WAV files. "
            "Returns duration, loudness, noise floor, sibilance, and clipping metrics."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "before_path": {"type": "string", "description": "Path to the original WAV file"},
                "after_path": {"type": "string", "description": "Optional path to the cleaned WAV file"},
            },
            "required": ["before_path"],
        }

    async def execute(self, before_path: str, after_path: str = "", **kwargs: Any) -> str:
        before = self._resolve(before_path)
        if after_path:
            after = self._resolve(after_path)
            result = compare_audio(before, after).to_dict()
        else:
            result = inspect_audio(before).to_dict()
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _resolve(self, path: str) -> Path:
        p = Path(path).expanduser().resolve()
        if self._allowed_dir and not self._is_relative_to(p, self._allowed_dir):
            raise PermissionError(f"Path {p} is outside allowed directory {self._allowed_dir}")
        return p

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
