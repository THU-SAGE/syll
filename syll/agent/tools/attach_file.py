"""Attach a local file to the outbound message by surfacing it as ToolResult.media."""

from pathlib import Path
from typing import Any

from syll.agent.tools.base import Tool, ToolResult


def _format_size(num: int) -> str:
    f = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.1f}{unit}" if unit != "B" else f"{int(f)}B"
        f /= 1024
    return f"{f:.1f}TB"


class AttachFileTool(Tool):
    """Attach a local file so it gets sent to the user as a channel attachment."""

    # Feishu single-file limit is 30MB; most channels cap around there.
    MAX_SIZE_BYTES = 30 * 1024 * 1024

    @property
    def name(self) -> str:
        return "attach_file"

    @property
    def description(self) -> str:
        return (
            "Attach a local file so it will be delivered to the user as a "
            "channel attachment (document/image). Call this once per file the "
            "user asked for; your final text reply will be sent alongside. "
            "Supports any file type the channel allows (Feishu/Telegram/Discord)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path of the file to attach.",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> ToolResult:
        p = Path(path).expanduser()
        if not p.exists():
            return ToolResult(text=f"Error: file not found: {path}")
        if not p.is_file():
            return ToolResult(text=f"Error: not a regular file: {path}")
        try:
            size = p.stat().st_size
        except OSError as e:
            return ToolResult(text=f"Error: cannot stat file: {e}")

        if size > self.MAX_SIZE_BYTES:
            return ToolResult(
                text=(
                    f"Error: file too large ({_format_size(size)}); "
                    f"channel limit ~{_format_size(self.MAX_SIZE_BYTES)}. "
                    "Ask the user how to proceed."
                )
            )

        return ToolResult(
            text=f"Attached: {p.name} ({_format_size(size)})",
            media=[str(p.resolve())],
        )
