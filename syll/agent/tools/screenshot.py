"""Standalone screenshot tool for capturing the desktop screen."""

import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.tools.base import Tool, ToolResult


class ScreenshotTool(Tool):
    """Capture a screenshot of the current desktop screen."""

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return "Capture a screenshot of the current desktop screen and return it as an image."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Take a DPR-aware screenshot and return it."""
        try:
            import mss
            from PIL import Image

            path = Path(tempfile.gettempdir()) / "syll_gui" / "screenshot.png"
            path.parent.mkdir(parents=True, exist_ok=True)

            with mss.mss() as sct:
                monitor = sct.monitors[0]  # full virtual screen
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

                # DPR-aware: if logical size differs from pixel size, resize
                logical_w = monitor["width"]
                logical_h = monitor["height"]
                if img.width > logical_w or img.height > logical_h:
                    img = img.resize((logical_w, logical_h), Image.LANCZOS)

                img.save(str(path))

            logger.debug(f"Screenshot saved to {path}")
            return ToolResult(text="Screenshot captured.", media=[str(path)])
        except ImportError:
            return ToolResult(text="Error: mss or Pillow not installed. Run: pip install mss Pillow")
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return ToolResult(text=f"Error: Failed to capture screenshot: {e}")
