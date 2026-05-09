"""File search tool using macOS mdfind with pathlib fallback."""

import asyncio
import json
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.tools.base import Tool


def _format_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f}{unit}" if unit != "B" else f"{num}B"
        num /= 1024
    return f"{num:.1f}TB"


class FindFileTool(Tool):
    """Search local files by name/extension. Uses Spotlight (mdfind) on macOS."""

    @property
    def name(self) -> str:
        return "find_file"

    @property
    def description(self) -> str:
        return (
            "Search for files on the local filesystem by name keyword and "
            "optional extensions. Returns a numbered list of candidates sorted "
            "by modification time (newest first). Use this when the user asks "
            "you to find a file (e.g. 'find my weekly report ppt')."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search in file name (substring match).",
                },
                "root": {
                    "type": "string",
                    "description": "Directory to search under. Defaults to ~ (home). Common: ~/Desktop, ~/Documents, ~/Downloads.",
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of file extensions without dot (e.g. ['pptx','ppt']).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return. Default 10.",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        root: str = "~",
        extensions: list[str] | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> str:
        root_path = Path(root).expanduser()
        if not root_path.exists():
            return f"Error: root path does not exist: {root}"

        exts = {f".{e.lstrip('.').lower()}" for e in (extensions or [])}

        try:
            if platform.system() == "Darwin":
                candidates = await self._mdfind(query, root_path, exts)
            else:
                candidates = await self._rglob(query, root_path, exts)
        except Exception as e:
            logger.error(f"find_file failed: {e}")
            return f"Error: search failed: {e}"

        # Filter, sort by mtime desc, limit
        results = []
        for p in candidates:
            try:
                stat = p.stat()
            except OSError:
                continue
            results.append((p, stat.st_mtime, stat.st_size))
        results.sort(key=lambda r: r[1], reverse=True)
        results = results[:limit]

        if not results:
            return json.dumps(
                {"query": query, "root": str(root_path), "count": 0, "items": []},
                ensure_ascii=False,
            )

        items = [
            {
                "id": idx + 1,
                "path": str(p),
                "name": p.name,
                "size": _format_size(size),
                "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            }
            for idx, (p, mtime, size) in enumerate(results)
        ]
        return json.dumps(
            {"query": query, "root": str(root_path), "count": len(items), "items": items},
            ensure_ascii=False,
            indent=2,
        )

    async def _mdfind(self, query: str, root: Path, exts: set[str]) -> list[Path]:
        # Build Spotlight query: match display name
        expr = f'kMDItemDisplayName == "*{query}*"cd'
        cmd = ["mdfind", "-onlyin", str(root), expr]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        paths = [Path(line) for line in stdout.decode().splitlines() if line.strip()]
        if exts:
            paths = [p for p in paths if p.suffix.lower() in exts]
        return [p for p in paths if p.is_file()]

    async def _rglob(self, query: str, root: Path, exts: set[str]) -> list[Path]:
        def _scan() -> list[Path]:
            q = query.lower()
            out: list[Path] = []
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if q not in p.name.lower():
                    continue
                if exts and p.suffix.lower() not in exts:
                    continue
                out.append(p)
                if len(out) >= 500:
                    break
            return out

        return await asyncio.get_running_loop().run_in_executor(None, _scan)
