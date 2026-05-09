"""Aloha import pipeline: imports ShowUI-Aloha recordings as AlohaSkills.

Handles the full pipeline:
1. LogProcessor: raw event log → processed actions
2. ScreenshotProcessor: video → screenshots (full + crop)
3. Convert actions → AlohaStep + AlohaAction
4. Copy keyframes to skill directory
5. (Optional) TraceGenerator: generate semantic traces
6. Save AlohaSkill
"""

import json
from pathlib import Path

from loguru import logger

from syll.agent.aloha.learn.log_processor import LogProcessor
from syll.agent.aloha.learn.screenshot_processor import VideoScreenshotExtractor
from syll.agent.aloha.learn.trace_generator import TraceGenerator
from syll.agent.aloha_gui_skill import (
    AlohaAction,
    AlohaSkill,
    AlohaSkillMeta,
    AlohaSkillStore,
    AlohaStep,
    AlohaTrace,
)


class AlohaImporter:
    """Import ShowUI-Aloha recordings into AlohaSkill format."""

    def __init__(
        self,
        store: AlohaSkillStore,
        trace_model: str = "gpt-4o",
        api_key: str | None = None,
        api_base: str | None = None,
    ):
        self.store = store
        self.trace_model = trace_model
        self.api_key = api_key
        self.api_base = api_base

    async def import_recording(
        self,
        project_path: str | Path,
        skill_name: str,
        description: str = "",
        auto_trace: bool = False,
        processed_actions_override: list[dict] | None = None,
    ) -> AlohaSkill:
        """Import a ShowUI-Aloha recording project.

        Args:
            project_path: Path to the recording project directory (containing inputs/).
            skill_name: Name for the new skill.
            description: Skill description.
            auto_trace: Whether to auto-generate traces via LLM.
            processed_actions_override: Optional edited processed actions to import
                instead of reparsing the raw log.

        Returns:
            The created AlohaSkill.
        """
        project_path = Path(project_path).resolve()
        logger.info(f"Importing Aloha recording from {project_path} as '{skill_name}'")

        inputs_dir = project_path / "inputs"
        if not inputs_dir.exists():
            inputs_dir = project_path

        processed_log_path = project_path / f"{project_path.name}_processed_log.json"
        if processed_actions_override is not None:
            logger.info(
                f"Using caller-provided processed actions override ({len(processed_actions_override)} actions)"
            )
            processed_actions = processed_actions_override
            processed_log_path.write_text(
                json.dumps(processed_actions, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            log_processor = LogProcessor()
            log_files = list(inputs_dir.glob("*.txt")) + list(inputs_dir.glob("*.log"))
            if not log_files:
                raise FileNotFoundError(f"No log files found in {inputs_dir}")

            log_file = log_files[0]
            logger.info(f"Processing log file: {log_file}")
            processed_actions = log_processor.process_log_file(
                log_file, output_path=str(processed_log_path)
            )

        # Step 2: Extract screenshots from video
        screen_resolution = ""
        screenshots_dir = project_path / "screenshots"

        try:
            extractor = VideoScreenshotExtractor()
            actions_with_screenshots, screenshots_dir, meta = extractor.process_project(
                str(project_path), processed_log=processed_actions
            )
            screen_resolution = meta.get("target_resolution", "")
            logger.info(f"Extracted {len(actions_with_screenshots)} screenshots")
        except Exception as e:
            logger.warning(f"Screenshot extraction failed (video may not exist): {e}")
            actions_with_screenshots = processed_actions

        # Step 3: Convert to AlohaSteps
        steps: list[AlohaStep] = []
        for idx, act in enumerate(actions_with_screenshots, 1):
            action = self._convert_action(act)
            full_fn = act.get("screenshot_full", "")
            crop_fn = act.get("screenshot_crop", "")

            # Copy keyframes to skill directory
            saved_full = ""
            saved_crop = ""
            if full_fn:
                src_full = self._resolve_screenshot_path(screenshots_dir, full_fn)
                if src_full and src_full.exists():
                    saved_full = self.store.save_keyframe(
                        skill_name, idx, src_full.read_bytes(), "_full.jpg"
                    )

            if crop_fn:
                src_crop = self._resolve_screenshot_path(screenshots_dir, crop_fn)
                if src_crop and src_crop.exists():
                    saved_crop = self.store.save_keyframe(
                        skill_name, idx, src_crop.read_bytes(), "_crop.jpg"
                    )

            steps.append(AlohaStep(
                index=idx,
                screenshot=saved_full,
                screenshot_crop=saved_crop,
                action=action,
            ))

        # Step 4: Create skill
        skill = AlohaSkill(
            meta=AlohaSkillMeta(
                name=skill_name,
                description=description,
                screen_resolution=screen_resolution,
                source="syll-recorder",
                trace_generated=False,
            ),
            steps=steps,
        )

        # Step 5: Auto-generate traces if requested
        if auto_trace and actions_with_screenshots:
            try:
                skill = await self._generate_and_attach_trace(
                    skill, actions_with_screenshots, screenshots_dir
                )
            except Exception as e:
                logger.error(f"Auto-trace generation failed: {e}")

        # Step 6: Save
        self.store.save_skill(skill)
        logger.info(f"Imported Aloha skill '{skill_name}' with {len(steps)} steps")
        return skill

    async def generate_trace(self, skill_name: str) -> AlohaSkill:
        """Generate or regenerate traces for an existing skill.

        Requires that the original recording data is still accessible.
        """
        skill = self.store.load_skill(skill_name)
        if not skill:
            raise ValueError(f"Aloha skill '{skill_name}' not found")

        # Build actions from steps for trace generation
        # We need screenshots — check keyframe paths
        skill_dir = self.store.skills_dir / skill_name / "keyframes"
        if not skill_dir.exists() or not skill.steps:
            raise ValueError(f"No keyframes found for skill '{skill_name}'")

        # Clear existing trace data before regenerating
        for step in skill.steps:
            step.trace = None
        skill.trajectory = []
        skill.meta.trace_generated = False
        self.store.save_skill(skill)

        # Generate traces using the skill's own keyframes
        tg = TraceGenerator(model=self.trace_model, api_key=self.api_key, api_base=self.api_base)
        trajectory = []

        for step in skill.steps:
            crop_path = self.store.get_keyframe_path(skill_name, step.screenshot_crop)
            full_path = self.store.get_keyframe_path(skill_name, step.screenshot)

            crop_b64 = tg._encode_image(str(crop_path)) if crop_path else None
            full_b64 = tg._encode_image(str(full_path)) if full_path else None

            if not crop_b64 and not full_b64:
                continue

            action_dict = {
                "action": step.action.description or step.action.type,
                "current_software": skill.meta.app_context,
                "timestamp": step.index,
            }

            recent = []
            for prev in trajectory[-3:]:
                cap = prev.get("caption", {})
                recent.append({
                    "step_idx": prev["step_idx"],
                    "Observation": cap.get("observation", ""),
                    "Think": cap.get("think", ""),
                    "Action": cap.get("action", ""),
                    "Expectation": cap.get("expectation", ""),
                })

            prompt = tg._prompt(action_dict, skill.meta.description, step.index, recent)
            txt = await tg._call_llm(prompt, crop_b64, full_b64)
            data = tg._extract_json(txt)

            cap = {
                "observation": tg._val(data, "Observation", default=""),
                "think": tg._val(data, "Think", default=""),
                "action": tg._val(data, "Action", default=""),
                "expectation": tg._val(data, "Expectation", default=""),
            }
            cap = tg._sanitize_caption(cap)

            trajectory.append({"step_idx": step.index, "caption": cap})

            # Attach trace to step
            step.trace = AlohaTrace(
                observation=cap["observation"],
                think=cap["think"],
                action=cap["action"],
                expectation=cap["expectation"],
            )

        skill.trajectory = trajectory
        skill.meta.trace_generated = True
        self.store.save_skill(skill)

        logger.info(f"Generated traces for '{skill_name}' ({len(trajectory)} steps)")
        return skill

    async def _generate_and_attach_trace(
        self,
        skill: AlohaSkill,
        actions_with_screenshots: list[dict],
        screenshots_dir: Path,
    ) -> AlohaSkill:
        """Generate traces and attach to skill steps."""
        tg = TraceGenerator(model=self.trace_model, api_key=self.api_key, api_base=self.api_base)
        trajectory = await tg.generate_trace(
            actions_with_screenshots, str(screenshots_dir), skill.meta.description
        )

        skill.trajectory = trajectory
        skill.meta.trace_generated = True

        # Attach trace to corresponding steps
        for traj_step in trajectory:
            idx = traj_step.get("step_idx", 0)
            cap = traj_step.get("caption", {})
            for step in skill.steps:
                if step.index == idx:
                    step.trace = AlohaTrace(
                        observation=cap.get("observation", ""),
                        think=cap.get("think", ""),
                        action=cap.get("action", ""),
                        expectation=cap.get("expectation", ""),
                    )
                    break

        return skill

    @staticmethod
    def _convert_action(act_dict: dict) -> AlohaAction:
        """Convert a processed log action dict to an AlohaAction."""
        raw_action = (act_dict.get("action") or "").strip()
        coords = act_dict.get("coords")
        path = act_dict.get("path")

        # Determine action type
        action_lower = raw_action.lower()
        if "type:" in action_lower or "type " in action_lower:
            action_type = "type"
            content = raw_action.split(":", 1)[1].strip() if ":" in raw_action else ""
        elif "press enter" in action_lower:
            action_type = "hotkey"
            content = "enter"
        elif "press " in action_lower:
            action_type = "hotkey"
            content = raw_action.replace("Press ", "").strip()
        elif "hotkey:" in action_lower:
            action_type = "hotkey"
            content = raw_action.split(":", 1)[1].strip() if ":" in raw_action else ""
        elif "dragstart" in action_lower:
            action_type = "drag"
            content = ""
        elif "dblclick" in action_lower or "double" in action_lower:
            action_type = "double_click"
            content = ""
        elif "rclick" in action_lower or "right" in action_lower:
            action_type = "right_click"
            content = ""
        elif "scrolldown" in action_lower:
            action_type = "scroll"
            content = "down"
        elif "scrollup" in action_lower:
            action_type = "scroll"
            content = "up"
        elif "click" in action_lower:
            action_type = "click"
            content = ""
        else:
            action_type = "click"
            content = raw_action

        # Extract coordinates
        coordinates = None
        end_coordinates = None
        if coords and isinstance(coords, list) and len(coords) > 0:
            c = coords[0]
            if isinstance(c, dict):
                coordinates = [int(c.get("x", 0)), int(c.get("y", 0))]

        if action_type == "drag" and path and len(path) >= 2:
            start = path[0]
            end = path[-1]
            if isinstance(start, dict):
                coordinates = [int(start.get("x", 0)), int(start.get("y", 0))]
            if isinstance(end, dict):
                end_coordinates = [int(end.get("x", 0)), int(end.get("y", 0))]

        return AlohaAction(
            type=action_type,
            coordinates=coordinates,
            end_coordinates=end_coordinates,
            content=content,
            description=raw_action,
        )

    @staticmethod
    def _resolve_screenshot_path(screenshots_dir: Path, filename: str) -> Path | None:
        """Resolve screenshot path, handling relative paths."""
        if not filename:
            return None
        # Strip "screenshots/" prefix if present
        if filename.startswith("screenshots/"):
            filename = filename[len("screenshots/"):]
        path = screenshots_dir / filename
        if path.exists():
            return path
        return None
