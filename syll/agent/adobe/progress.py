"""Bridge GUI-step progress callbacks to chat clients via ``broadcast_ws``.

The Adobe tools run a multi-minute GUI step inside a single (otherwise atomic)
tool call. To let the user watch it unfold, each GUI step is fanned out to all
connected chat WebSocket clients as a ``tool_progress`` event — the same
out-of-band channel cron and MCP use. Screenshots are base64-encoded inline so
the browser can render them without a media server.

This is best-effort: when no broadcaster is wired (headless / REST / CLI) the
factory returns ``None`` and the GUI tools simply skip emitting.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any


def make_broadcast_progress(agent_loop: Any, *, run_id: str, app: str):
    """Return an async progress callback, or ``None`` if broadcasting is unavailable.

    Args:
        agent_loop: the running :class:`AgentLoop`; its ``broadcast_ws`` is read
            lazily here (not at tool-registration time) so it picks up the
            broadcaster the web app assigns after the loop is constructed.
        run_id: the per-run id, so a client can match progress to a run.
        app: the originating tool name (e.g. ``photoshop_cutout``).
    """
    broadcast = getattr(agent_loop, "broadcast_ws", None)
    if broadcast is None:
        return None

    async def _cb(event: dict[str, Any]) -> None:
        ev = dict(event or {})
        shot = ev.pop("screenshot", None)
        encoded = None
        if isinstance(shot, str):
            p = Path(shot)
            if p.is_file():
                mime = mimetypes.guess_type(shot)[0] or "image/png"
                encoded = {"mime": mime, "data": base64.b64encode(p.read_bytes()).decode()}
        payload: dict[str, Any] = {"type": "tool_progress", "tool": app, "run_id": run_id, **ev}
        if encoded is not None:
            payload["screenshot"] = encoded
        try:
            await broadcast(payload)
        except Exception:
            # Progress is decorative; never let a broadcast failure abort a run.
            pass

    return _cb
