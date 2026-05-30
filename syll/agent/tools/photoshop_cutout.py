"""Conversational Adobe Photoshop background-cutout tool.

Runs the real Photoshop app on a macOS host: opens the uploaded image, drives
the GUI to remove the background, then deterministically exports a transparent
PNG (and an editable .psd) and verifies the alpha channel. The backend — not
the GUI agent — owns file I/O, and the result is *measured*, never taken on the
app's word: the returned verdict comes from the alpha-coverage check, so a poor
cutout reads as "review", not "done".

The tool seizes the mouse and keyboard for the GUI step, so it is gated behind
an explicit consent turn: the first call (``confirmed`` unset/false) returns a
takeover prompt and touches nothing; the model calls again with
``confirmed=true`` only after the user agrees.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.adobe import photoshop_core as pc
from syll.agent.adobe.lock import try_acquire_adobe
from syll.agent.adobe.preflight import photoshop_preflight
from syll.agent.adobe.progress import make_broadcast_progress
from syll.agent.tools.base import Tool, ToolResult


class PhotoshopCutoutTool(Tool):
    """Remove an image background via real Adobe Photoshop and verify the cutout."""

    def __init__(
        self,
        *,
        registry: Any,
        gui_config: Any,
        workspace: str | Path,
        skill_store: Any,
        syll_config: Any = None,
        agent_loop: Any = None,
        event_store: Any = None,
    ):
        self._registry = registry
        self._gui_config = gui_config
        self._workspace = Path(workspace)
        self._skill_store = skill_store
        self._syll_config = syll_config
        self._agent_loop = agent_loop
        self._event_store = event_store

    @property
    def name(self) -> str:
        return "photoshop_cutout"

    @property
    def description(self) -> str:
        return (
            "Remove the background from an image using the real Adobe Photoshop app "
            "(macOS only) and return a transparent-PNG cutout with before/after previews. "
            "Use for: remove background, cut out / knock out the subject, isolate the subject, "
            "make a transparent PNG; 抠图、去背景、扣出主体、透明PNG. "
            "This takes over the mouse and keyboard while it runs: call once with confirmed=false "
            "to get a consent prompt for the user, then call again with confirmed=true after they agree."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Local path to the source image (the photo the user attached).",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set true ONLY after the user agreed to let Syll take over the mouse and "
                        "keyboard. Leave unset/false on the first call to get a consent prompt."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "skill", "zero_shot"],
                    "description": (
                        "Automation mode. 'auto' uses the recorded cutout skill when it matches the "
                        "screen, else falls back to zero-shot GUI control."
                    ),
                },
            },
            "required": ["image_path"],
        }

    async def execute(
        self,
        image_path: str,
        confirmed: bool = False,
        mode: str = "auto",
        **kwargs: Any,
    ) -> str | ToolResult:
        src = Path(image_path).expanduser()
        if not src.is_file():
            return ToolResult(text=f"I can't find an image at {image_path} — re-attach it and tell me again.")

        coord_dir = self._workspace / "coord_profiles"
        pf = photoshop_preflight(
            workspace_path=self._workspace,
            gui_config=self._gui_config,
            tool_registry=self._registry,
            skill_store=self._skill_store,
            coord_profiles_dir=coord_dir,
        )
        if not pf.ready:
            return ToolResult(
                text="I can't run the Photoshop cutout here:\n- " + "\n- ".join(pf.blockers)
            )

        if not confirmed:
            extra = ("\n\nHeads up:\n- " + "\n- ".join(pf.warnings)) if pf.warnings else ""
            return ToolResult(
                text=(
                    f"This opens {pf.app_name or 'Adobe Photoshop'} and takes over your mouse and "
                    f"keyboard for ~30–60s to remove the background. Reply to confirm and I'll run it."
                    + extra
                )
            )

        lease = try_acquire_adobe("photoshop_cutout")
        if lease is None:
            return ToolResult(
                text="Another Adobe task is already running on the screen; let it finish before starting this one."
            )

        try:
            run_id = uuid.uuid4().hex[:16]
            run_dir = self._workspace / "photoshop_runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)

            input_png = run_dir / "input.png"
            pc.normalize_image_to_png(src, input_png)

            chosen = mode
            if mode == "auto":
                chosen = "skill" if pf.skill_mode_available else "zero_shot"
            elif mode == "skill" and not pf.skill_mode_available:
                chosen = "zero_shot"

            work_psd = run_dir / "work.psd"
            await pc.prepare_photoshop_document(
                input_png=input_png,
                work_psd=work_psd,
                script_path=run_dir / "prepare.jsx",
                app_path=pf.app_path,
                timeout=120.0,
            )

            instruction = pc.build_photoshop_instruction(
                run_id=run_id,
                input_png=input_png,
                work_psd=work_psd,
                output_dir=run_dir,
            )
            progress_cb = make_broadcast_progress(self._agent_loop, run_id=run_id, app="photoshop_cutout")
            gui_params: dict[str, Any] = {"instruction": instruction, "max_steps": 50}
            if progress_cb is not None:
                gui_params["progress_callback"] = progress_cb
            if chosen == "skill":
                gui_tool = "gui_action_planned"
                gui_params["skill_name"] = pc.PHOTOSHOP_SKILL_NAME
                gui_params["actor_mode"] = "ui-tars"
            else:
                gui_tool = "gui_action"
            # The GUI agent only manipulates the visible document; the deterministic
            # export below owns the artifacts, so its text/media are not needed here.
            await self._registry.execute(gui_tool, gui_params)

            cutout_png = run_dir / "cutout.png"
            await pc.export_photoshop_outputs(
                work_psd=work_psd,
                output_psd=run_dir / "editable.psd",
                cutout_png=cutout_png,
                script_path=run_dir / "export.jsx",
                app_path=pf.app_path,
                timeout=120.0,
            )

            checker = run_dir / "preview_checker.png"
            white = run_dir / "preview_on_white.png"
            metrics = pc.verify_cutout(cutout_png, checker, white)
            files = {
                "input": str(input_png),
                "cutout": str(cutout_png),
                "preview_checker": str(checker),
                "preview_on_white": str(white),
            }
            report = pc.render_cutout_report(metrics, files, chosen)

            media = [p for p in (input_png, cutout_png, checker, white) if p.is_file()]
            return ToolResult(text=report, media=[str(p) for p in media])
        except Exception as e:
            logger.warning(f"photoshop_cutout failed: {e}")
            return ToolResult(text=f"The Photoshop cutout run failed: {e}")
        finally:
            lease.release()
