"""Generate thumbnail previews for documents (PPT/PDF/Doc/Image) on macOS."""

import asyncio
import platform
import tempfile
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.tools.base import Tool, ToolResult


class FilePreviewTool(Tool):
    """Render first-page thumbnails for documents via macOS qlmanage."""

    @property
    def name(self) -> str:
        return "file_preview"

    @property
    def description(self) -> str:
        return (
            "Generate thumbnail images (first page) for one or more local "
            "document files so the user can visually identify them. Supports "
            ".pptx/.ppt/.pdf/.docx/.xlsx/images on macOS (uses qlmanage). "
            "Use this after find_file to show the user what each candidate "
            "looks like before they confirm."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of files to preview.",
                },
                "size": {
                    "type": "integer",
                    "description": "Thumbnail longest edge in px. Default 512.",
                    "minimum": 128,
                    "maximum": 2048,
                },
            },
            "required": ["paths"],
        }

    async def execute(
        self,
        paths: list[str],
        size: int = 512,
        **kwargs: Any,
    ) -> ToolResult:
        if platform.system() != "Darwin":
            return ToolResult(
                text="Error: file_preview currently only supports macOS (qlmanage).",
            )

        out_dir = Path(tempfile.gettempdir()) / "syll_previews" / uuid.uuid4().hex[:8]
        out_dir.mkdir(parents=True, exist_ok=True)

        generated: list[str] = []
        notes: list[str] = []

        for raw in paths:
            src = Path(raw).expanduser()
            if not src.is_file():
                notes.append(f"✗ {raw}: not a file")
                continue

            try:
                proc = await asyncio.create_subprocess_exec(
                    "qlmanage", "-t", "-s", str(size), "-o", str(out_dir), str(src),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=20)
            except asyncio.TimeoutError:
                notes.append(f"✗ {src.name}: qlmanage timeout")
                continue
            except Exception as e:
                notes.append(f"✗ {src.name}: {e}")
                continue

            # qlmanage writes <name>.png into out_dir
            expected = out_dir / f"{src.name}.png"
            if expected.is_file():
                generated.append(str(expected))
                notes.append(f"✓ {src.name}")
            else:
                notes.append(f"✗ {src.name}: no preview produced")

        text = f"Generated {len(generated)}/{len(paths)} previews:\n" + "\n".join(notes)
        logger.debug(f"file_preview: {len(generated)} previews in {out_dir}")
        return ToolResult(text=text, media=generated)
