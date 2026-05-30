"""Conversational Adobe Audition voice-cleanup tool.

Drives the real Audition app on a macOS host to repair a spoken-audio clip
(noise / hum / sibilance), waits for the exported ``cleaned.wav`` to land in a
watched folder, then re-measures it with the deterministic audio metrics
engine and reports an honest verdict. The metrics engine never trusts the
app's word: if the change looks like a pure volume shift it is labelled
``gain_only`` and reported as "repair not proven".

Like the Photoshop tool, the GUI step seizes the mouse and keyboard, so it is
gated behind an explicit consent turn (first call returns a takeover prompt;
the model re-calls with ``confirmed=true`` only after the user agrees).
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.adobe import audio_core as ac
from syll.agent.adobe.lock import try_acquire_adobe
from syll.agent.adobe.preflight import audition_preflight
from syll.agent.adobe.progress import make_broadcast_progress
from syll.agent.tools.base import Tool, ToolResult
from syll.audio.metrics import compare_audio, finalize_loudness, render_report_markdown


class CleanAudioInAuditionTool(Tool):
    """Repair a voice recording via real Adobe Audition and prove the result."""

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
        return "clean_audio_in_audition"

    @property
    def description(self) -> str:
        return (
            "Clean up a voice recording (remove hiss, hum, background noise, harsh sibilance) using "
            "the real Adobe Audition app (macOS only), then return the cleaned audio with a measured "
            "before/after verdict. Use for: clean up audio, denoise, remove hiss/hum/noise, de-ess, "
            "repair voice; 降噪、去底噪、去杂音、人声清理、去齿音. "
            "This takes over the mouse and keyboard while it runs: call once with confirmed=false to "
            "get a consent prompt for the user, then call again with confirmed=true after they agree."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "audio_path": {
                    "type": "string",
                    "description": "Local path to the voice recording the user attached.",
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
                    "description": "Automation mode. 'auto' uses the recorded cleanup skill when it matches, else zero-shot.",
                },
                "export_timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 900,
                    "description": "How long to wait for Audition to export cleaned.wav (default 300).",
                },
            },
            "required": ["audio_path"],
        }

    async def execute(
        self,
        audio_path: str,
        confirmed: bool = False,
        mode: str = "auto",
        export_timeout_seconds: float = 300.0,
        **kwargs: Any,
    ) -> str | ToolResult:
        src = Path(audio_path).expanduser()
        if not src.is_file():
            return ToolResult(text=f"I can't find an audio file at {audio_path} — re-attach it and tell me again.")

        coord_dir = self._workspace / "coord_profiles"
        pf = audition_preflight(
            workspace_path=self._workspace,
            gui_config=self._gui_config,
            tool_registry=self._registry,
            skill_store=self._skill_store,
            coord_profiles_dir=coord_dir,
        )
        if not pf.ready:
            return ToolResult(
                text="I can't run the Audition cleanup here:\n- " + "\n- ".join(pf.blockers)
            )

        if not confirmed:
            extra = ("\n\nHeads up:\n- " + "\n- ".join(pf.warnings)) if pf.warnings else ""
            return ToolResult(
                text=(
                    f"This opens {pf.app_name or 'Adobe Audition'} and takes over your mouse and "
                    f"keyboard for ~1–2 min to repair the audio. Reply to confirm and I'll run it."
                    + extra
                )
            )

        # TTL must exceed this run's worst-case wall-clock (GUI step + the
        # configurable export wait) so a slow-but-alive run is never reclaimed.
        lease = try_acquire_adobe(
            "clean_audio_in_audition", ttl_seconds=float(export_timeout_seconds) + 900.0
        )
        if lease is None:
            return ToolResult(
                text="Another Adobe task is already running on the screen; let it finish before starting this one."
            )

        try:
            run_id = uuid.uuid4().hex[:16]
            run_dir = self._workspace / "audio_runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)

            input_wav = run_dir / "input.wav"
            await ac.normalize_to_wav(src, input_wav)

            chosen = mode
            if mode == "auto":
                chosen = "skill" if pf.skill_mode_available else "zero_shot"
            elif mode == "skill" and not pf.skill_mode_available:
                chosen = "zero_shot"

            # Watched export folders. Clear any stale cleaned.wav first so a
            # previous run's file can't be mistaken for this one's export.
            pending_dir = self._workspace / "audio_demos" / ac.PENDING_DIR_NAME
            pending_dir.mkdir(parents=True, exist_ok=True)
            ac.SHORT_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            final_wav = run_dir / "cleaned.wav"
            pending_file = pending_dir / "cleaned.wav"
            short_file = ac.SHORT_EXPORT_DIR / "cleaned.wav"
            for stale in (final_wav, pending_file, short_file):
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass

            await ac.set_clipboard_text(str(short_file))
            await ac.preopen_audition(input_wav)

            instruction = ac.build_audio_instruction(
                run_id=run_id,
                input_wav=input_wav,
                run_dir=run_dir,
                pending=pending_dir,
                short_export_dir=ac.SHORT_EXPORT_DIR,
                final_wav=final_wav,
                mode=chosen,
            )
            progress_cb = make_broadcast_progress(self._agent_loop, run_id=run_id, app="clean_audio_in_audition")
            gui_params: dict[str, Any] = {"instruction": instruction, "max_steps": 50}
            if progress_cb is not None:
                gui_params["progress_callback"] = progress_cb
            if chosen == "skill":
                gui_tool = "gui_action_planned"
                gui_params["skill_name"] = ac.AUDIO_SKILL_NAME
                gui_params["actor_mode"] = "ui-tars"
            else:
                gui_tool = "gui_action"
            await self._registry.execute(gui_tool, gui_params)

            candidates = ac.export_candidates(run_dir, pending_file, short_file, final_wav)
            exported = await ac.wait_for_export(candidates, final_wav, float(export_timeout_seconds))
            if not exported:
                return ToolResult(
                    text=(
                        f"Audition didn't export cleaned.wav into a watched folder within "
                        f"{int(export_timeout_seconds)}s, so there's nothing to verify yet. "
                        f"Check that the repair finished and the file was saved."
                    )
                )

            report = compare_audio(input_wav, final_wav)
            note = ""
            if ac.should_finalize_loudness(report):
                try:
                    shutil.copy2(final_wav, run_dir / "cleaned_audition.wav")
                except Exception:
                    pass
                fin = finalize_loudness(final_wav)
                report = compare_audio(input_wav, final_wav)
                if fin.get("applied"):
                    note = f"\n\n_Applied a deterministic loudness pass toward {report.target_lufs} LUFS._"

            text = render_report_markdown(report) + note
            return ToolResult(text=text, media=[str(input_wav), str(final_wav)])
        except Exception as e:
            logger.warning(f"clean_audio_in_audition failed: {e}")
            return ToolResult(text=f"The Audition cleanup run failed: {e}")
        finally:
            lease.release()
