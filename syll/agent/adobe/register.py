"""Shared registration for the Adobe conversational tools.

Called from both ``AgentLoop._register_default_tools`` and the web config
hot-reload path so the two cannot drift (toggling ``tools.gui.enabled`` or
installing the app + saving config keeps the tool set in sync).

Registration is best-effort: a missing optional dependency (Pillow /
pyloudnorm) or import error must never take down the load-bearing GUI tools
that share the same gated block, so the whole thing is wrapped in a guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

ADOBE_TOOL_NAMES = ("photoshop_cutout", "clean_audio_in_audition")


def register_adobe_tools(
    registry: Any,
    *,
    agent_loop: Any,
    gui_config: Any,
    syll_config: Any,
    workspace: str | Path,
    skill_store: Any,
    event_store: Any = None,
) -> None:
    """(Re)register the macOS Adobe GUI tools on ``registry``. Idempotent."""
    for name in ADOBE_TOOL_NAMES:
        registry.unregister(name)
    try:
        from syll.agent.tools.clean_audio_in_audition import CleanAudioInAuditionTool
        from syll.agent.tools.photoshop_cutout import PhotoshopCutoutTool

        common = dict(
            registry=registry,
            gui_config=gui_config,
            syll_config=syll_config,
            workspace=Path(workspace),
            skill_store=skill_store,
            agent_loop=agent_loop,
            event_store=event_store,
        )
        registry.register(PhotoshopCutoutTool(**common))
        registry.register(CleanAudioInAuditionTool(**common))
        logger.info("Registered Adobe tools: photoshop_cutout, clean_audio_in_audition")
    except Exception as e:
        logger.warning(f"Adobe tool registration skipped: {e}")


def unregister_adobe_tools(registry: Any) -> None:
    """Remove the Adobe tools (used when GUI is disabled at runtime)."""
    for name in ADOBE_TOOL_NAMES:
        registry.unregister(name)
