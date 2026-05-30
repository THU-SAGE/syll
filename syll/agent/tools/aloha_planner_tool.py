"""AlohaPlannerTool: GUI automation with Planner+Actor architecture.

Uses ShowUI-Aloha's planner-actor pattern:
1. Screenshot
2. Planner: trajectory guidance + screenshot + history → next action plan
3. Actor: plan + screenshot → concrete action (UI-TARS or Claude CUA)
4. Executor: shared click backend execution
5. Repeat until Planner sets Action=null or max_steps reached
"""

import base64
import inspect
import json
import platform
import re
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.aloha.act.executor import AlohaExecutor
from syll.agent.aloha.act.planner import AlohaPlanner
from syll.agent.aloha.act.trajectory_manager import TrajectoryManager
from syll.agent.aloha_gui_skill import AlohaSkillStore
from syll.agent.events import Event, EventContent, EventSource, EventStore
from syll.agent.tools.base import Tool, ToolResult


class AlohaPlannerTool(Tool):
    """GUI automation with demonstration-guided Planner+Actor architecture."""

    def __init__(
        self,
        gui_config: Any,
        aloha_skill_store: AlohaSkillStore,
        syll_config: Any = None,
    ):
        self._config = gui_config
        self._aloha_skill_store = aloha_skill_store
        self._syll_config = syll_config
        self._screenshot_dir = Path(tempfile.gettempdir()) / "syll_gui_planner"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._event_store: EventStore | None = None
        self._screen_offset: tuple[int, int] = (0, 0)
        self._model_img_size: tuple[int, int] = (0, 0)

    @property
    def name(self) -> str:
        return "gui_action_planned"

    @property
    def description(self) -> str:
        return (
            "Perform GUI actions using demonstration-guided planning. "
            "Uses a Planner+Actor architecture: the Planner follows a recorded "
            "trajectory to decide the next action, then the Actor executes it. "
            "Requires a recorded Aloha skill as guidance."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "What GUI task to perform",
                },
                "skill_name": {
                    "type": "string",
                    "description": "Name of an Aloha GUI skill to use as guidance trajectory",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Maximum steps (default: from config)",
                    "minimum": 1,
                    "maximum": 50,
                },
                "actor_mode": {
                    "type": "string",
                    "description": "Actor backend: 'ui-tars' or 'claude-cua'",
                    "enum": ["ui-tars", "claude-cua"],
                },
            },
            "required": ["instruction", "skill_name"],
        }

    async def execute(
        self,
        instruction: str,
        skill_name: str,
        max_steps: int | None = None,
        actor_mode: str | None = None,
        progress_callback: Any = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        """Execute a GUI task using Planner+Actor loop with trajectory guidance."""
        steps_limit = max_steps or self._config.max_steps
        os_name = platform.system()

        # Load skill and trajectory
        skill = self._aloha_skill_store.load_skill(skill_name)
        if not skill:
            return ToolResult(text=f"Aloha skill '{skill_name}' not found")

        if not skill.trajectory and not skill.steps:
            return ToolResult(text=f"Skill '{skill_name}' has no trajectory or steps")

        mode = actor_mode or getattr(skill.meta, "actor_mode", "")
        if not mode and self._syll_config:
            actor_model = self._syll_config.resolve_endpoint("actor").model.lower()
            if "claude" in actor_model or "anthropic" in actor_model:
                mode = "claude-cua"
        mode = mode or getattr(self._config, 'execution_mode', 'ui-tars') or "ui-tars"

        # Build trajectory guidance
        traj_manager = TrajectoryManager()
        if skill.trajectory:
            guidance = traj_manager.get_trajectory_in_context(skill.trajectory)
        else:
            # Build guidance from step descriptions
            guidance_steps = []
            for s in skill.steps:
                desc = ""
                if s.trace:
                    desc = s.trace.action
                elif s.action.description:
                    desc = s.action.description
                else:
                    desc = s.action.type
                guidance_steps.append(f"Step [{s.index}]: {desc}")
            guidance = "\n".join(guidance_steps)

        if not guidance:
            guidance = "(No guidance trajectory available)"

        # Resolve planner endpoint via purpose-based config
        if self._syll_config:
            ep = self._syll_config.resolve_endpoint("planner")
            planner_model = ep.litellm_model
            planner_api_key = ep.api_key or None
            planner_api_base = ep.api_base
        else:
            planner_model = "gpt-4o"
            planner_api_key = None
            planner_api_base = None

        planner = AlohaPlanner(
            model=planner_model,
            os_name=os_name,
            api_key=planner_api_key,
            api_base=planner_api_base,
        )

        # Initialize executor
        executor = AlohaExecutor(self._config)

        screenshots: list[str] = []
        action_history: list[str] = []
        steps_log: list[dict] = []

        for step in range(1, steps_limit + 1):
            logger.info(f"Planner step {step}/{steps_limit}: {instruction}")
            await self._emit_progress(progress_callback, {"kind": "gui_step", "step": step, "message": f"Planner step {step}/{steps_limit}"})

            # Take screenshot (Issue 9: uses selected_screen)
            screenshot_path = await self._take_screenshot(step)
            if not screenshot_path:
                return ToolResult(text="Error: Failed to capture screenshot")
            screenshots.append(screenshot_path)
            await self._emit_progress(
                progress_callback,
                {"kind": "screenshot", "step": step, "message": "Captured screenshot", "screenshot": screenshot_path},
            )

            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode()

            # Call Planner
            try:
                plan = await planner.plan(
                    task=instruction,
                    guidance_trajectory=guidance,
                    screenshot_b64=screenshot_b64,
                    action_history=action_history,
                )
            except Exception as e:
                logger.error(f"Planner failed: {e}")
                return ToolResult(
                    text=f"Planner error at step {step}: {e}",
                    media=[screenshot_path],
                )

            plan_action = plan.get("Action")
            plan_observation = plan.get("Observation", "")
            plan_reasoning = plan.get("Reasoning", "")
            current_step = plan.get("Current Step", step)
            original_plan_action = plan_action
            plan_action = self._rewrite_forbidden_first_step_action(plan_action, step, skill)
            if plan_action != original_plan_action:
                logger.info(
                    f"  Rewrote first-step plan action: {original_plan_action!r} -> {plan_action!r}"
                )

            logger.info(f"  Plan: step={current_step}, action={plan_action}")
            logger.info(f"  Reasoning: {plan_reasoning}")
            await self._emit_progress(
                progress_callback,
                {
                    "kind": "gui_thought",
                    "step": step,
                    "message": plan_reasoning or plan_observation or str(plan_action),
                    "thought": plan_reasoning,
                    "action": plan_action,
                },
            )

            # Check if task is complete
            if plan_action is None or plan_action == "null" or plan_action == "":
                key_shots = self._key_screenshots(screenshots)
                return ToolResult(
                    text=f"GUI task completed via planner.\n\n"
                         f"Steps taken: {step}\n"
                         f"Final observation: {plan_observation}\n\n"
                         f"Steps log:\n{json.dumps(steps_log, indent=2)}",
                    media=key_shots,
                )

            # Call Actor to get concrete action
            try:
                action_dict, is_complete = await self._call_actor(
                    mode, plan_action, screenshot_b64, os_name
                )
            except Exception as e:
                logger.error(f"Actor failed: {e}")
                return ToolResult(
                    text=f"Actor error at step {step}: {e}",
                    media=[screenshot_path],
                )

            if is_complete:
                key_shots = self._key_screenshots(screenshots)
                return ToolResult(
                    text=f"GUI task completed by actor.\nSteps taken: {step}\n\n"
                         f"Steps log:\n{json.dumps(steps_log, indent=2)}",
                    media=key_shots,
                )

            model_position = None
            executor_position = None

            # Apply coordinate transform via unified service
            if "position" in action_dict:
                pos = action_dict["position"]
                if isinstance(pos, list) and len(pos) == 2:
                    model_position = [int(pos[0]), int(pos[1])]
                    x, y = self._transform_coords(pos[0], pos[1], mode=mode)
                    executor_position = [x, y]
                    action_dict["position"] = executor_position

            if plan_action:
                action_dict["intent"] = plan_action
                action_dict["plan"] = plan_action
            action_dict["instruction"] = instruction
            await self._emit_progress(
                progress_callback,
                {"kind": "gui_action", "step": step, "message": str(action_dict), "action": str(action_dict)},
            )

            # Preserve explicit planner request for a double-click sequence.
            if action_dict.get("action") == "CLICK" and plan_action:
                normalized = plan_action.lower().replace("-", " ").replace("_", " ")
                if "double click" in normalized or "双击" in plan_action:
                    action_dict["click_count"] = 2
                    logger.info("  Upgraded CLICK → 2 explicit clicks (planner requested)")

            # Execute action
            success, msg = await executor.execute(action_dict)
            await self._emit_progress(
                progress_callback,
                {"kind": "gui_result", "step": step, "message": msg, "result": msg},
            )
            click_backend = action_dict.get("click_backend")
            mac_accessibility = action_dict.get("mac_accessibility")
            event_style = action_dict.get("event_style")
            frontmost_app = action_dict.get("frontmost_app")
            action_history.append(
                self._format_action_history(
                    plan_action,
                    msg,
                    model_position,
                    executor_position,
                    click_backend,
                    mac_accessibility,
                    event_style,
                    frontmost_app,
                )
            )

            # Issue 7: Record step log
            steps_log.append({
                "step": step,
                "plan": plan_action,
                "action": action_dict,
                "reasoning": plan_reasoning,
                "observation": plan_observation,
                "executor_result": msg,
                "model_position": model_position,
                "executor_position": executor_position,
                "click_backend": click_backend,
                "mac_accessibility": mac_accessibility,
                "event_style": event_style,
                "frontmost_app": frontmost_app,
            })

            if not success:
                logger.warning(f"Action failed at step {step}: {msg}")
                # Don't abort — let planner adapt

            # Issue 1: Log event with agent_type="gui_agent"
            if self._event_store:
                event = Event(
                    agent_type="gui_agent",
                    event_type="action",
                    source=EventSource(platform="desktop", chat_id="gui", user_id="system"),
                    content=EventContent(
                        text=f"Instruction: {instruction}\n"
                             f"Plan: {plan_action}\n"
                             f"Reasoning: {plan_reasoning}\n"
                             f"Result: {msg}",
                        media=[screenshot_path],
                        metadata={
                            "step": step,
                            "plan_action": plan_action,
                            "current_step": current_step,
                            "actor_mode": mode,
                            "skill_name": skill_name,
                            "model_position": model_position,
                            "executor_position": executor_position,
                            "click_backend": action_dict.get("click_backend"),
                            "mac_accessibility": action_dict.get("mac_accessibility"),
                            "event_style": action_dict.get("event_style"),
                            "frontmost_app": action_dict.get("frontmost_app"),
                        },
                    ),
                )
                self._event_store.log_event(event)

        key_shots = self._key_screenshots(screenshots)
        return ToolResult(
            text=f"Reached max steps ({steps_limit}). Task may not be complete.\n\n"
                 f"Steps log:\n{json.dumps(steps_log, indent=2)}",
            media=key_shots,
        )

    @staticmethod
    async def _emit_progress(progress_callback: Any, event: dict[str, Any]) -> None:
        if not progress_callback:
            return
        try:
            result = progress_callback(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.debug(f"Aloha planner progress callback failed: {exc}")

    _FIRST_STEP_HIDE_HOTKEY_RE = re.compile(
        r"\b(?:cmd|command)\s*(?:\+|-|\s)\s*h\b", re.IGNORECASE
    )

    @classmethod
    def _rewrite_forbidden_first_step_action(
        cls,
        plan_action: str | None,
        step: int,
        skill: Any,
    ) -> str | None:
        """Prevent step 1 from planning Cmd+H/Command+H for demo execution."""
        if step != 1 or not plan_action:
            return plan_action
        if not cls._FIRST_STEP_HIDE_HOTKEY_RE.search(plan_action):
            return plan_action
        replacement = cls._first_guidance_action(skill)
        return replacement or plan_action

    @staticmethod
    def _first_guidance_action(skill: Any) -> str | None:
        """Extract the first concrete action text from trajectory/steps."""
        trajectory = getattr(skill, "trajectory", None) or []
        if trajectory:
            first = trajectory[0]
            caption = first.get("caption", {}) if isinstance(first, dict) else {}
            action = caption.get("action")
            if action:
                return str(action)

        steps = getattr(skill, "steps", None) or []
        if not steps:
            return None
        first_step = steps[0]
        trace = getattr(first_step, "trace", None)
        if trace and getattr(trace, "action", None):
            return str(trace.action)
        action = getattr(first_step, "action", None)
        if action and getattr(action, "description", None):
            return str(action.description)
        if action and getattr(action, "type", None):
            return str(action.type)
        return None

    @staticmethod
    def _format_action_history(
        plan_action: str | None,
        executor_result: str,
        model_position: list[int] | None = None,
        executor_position: list[int] | None = None,
        click_backend: str | None = None,
        mac_accessibility: str | None = None,
        event_style: str | None = None,
        frontmost_app: str | None = None,
    ) -> str:
        """Format planner history with model/executor coordinates plus execution diagnostics."""
        parts: list[str] = []
        if plan_action:
            parts.append(plan_action)
        if model_position is not None:
            parts.append(f"model_position={model_position}")
        if executor_position is not None:
            parts.append(f"executor_position={executor_position}")
        if click_backend:
            parts.append(f"click_backend={click_backend}")
        if mac_accessibility:
            parts.append(f"mac_accessibility={mac_accessibility}")
        if event_style:
            parts.append(f"event_style={event_style}")
        if frontmost_app:
            parts.append(f"frontmost_app={frontmost_app}")
        prefix = " | ".join(parts) if parts else "action"
        return f"{prefix} → {executor_result}"

    def _transform_coords(self, x: int, y: int, mode: str = "ui-tars") -> tuple[int, int]:
        """Transform model coordinates through the unified pipeline."""
        try:
            from syll.agent.tools.coordinate_transform import (
                ActorSpace,
                ActorType,
                CoordinateTransformService,
            )

            workspace = getattr(self._syll_config, 'workspace_path', None)
            if not workspace:
                # Fallback to manual offset
                return x + self._screen_offset[0], y + self._screen_offset[1]
            service = CoordinateTransformService(workspace / "coord_profiles")
            selected = getattr(self._config, 'selected_screen', 0)
            ctx = service.get_frame_context(selected)
            profile = service.load_profile(selected)

            # Match coordinate-space mapping to the active actor backend.
            if mode == "claude-cua":
                actor = ActorSpace(actor_type=ActorType.CLAUDE_CUA)
            else:
                # UI-TARS: pass model image dims so Step 1 reverse-scales from WXGA
                mw, mh = self._model_img_size
                actor = ActorSpace(
                    actor_type=ActorType.UI_TARS, api_width=mw, api_height=mh
                )
            result = service.model_to_executor(x, y, ctx, actor, profile)
            return result.executor_x, result.executor_y
        except Exception as e:
            logger.debug(f"Coordinate transform fallback (offset only): {e}")
            return x + self._screen_offset[0], y + self._screen_offset[1]

    async def _call_actor(
        self, mode: str, plan_action: str, screenshot_b64: str, os_name: str
    ) -> tuple[dict, bool]:
        """Route to the appropriate actor backend."""
        if mode == "claude-cua":
            return self._call_claude_cua(plan_action, screenshot_b64, os_name)
        else:
            return await self._call_uitars_actor(plan_action, screenshot_b64)

    async def _call_uitars_actor(
        self, plan_action: str, screenshot_b64: str
    ) -> tuple[dict, bool]:
        """Use UI-TARS as actor: send plan action + screenshot, get concrete action."""
        import litellm

        # Issue 8: Load system prompt from template file, with hardcoded fallback
        system_prompt = self._load_actor_system_prompt()

        content: list[dict] = [
            {"type": "text", "text": f"Planned action: {plan_action}\n\nExecute this action:"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
        ]

        # Resolve actor endpoint via purpose-based config
        if self._syll_config:
            ep = self._syll_config.resolve_endpoint("actor")
            model = ep.litellm_model
            api_key = ep.api_key or None
            api_base = ep.api_base
        else:
            model = "ui-tars"
            api_key = None
            api_base = None

        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            api_key=api_key,
            api_base=api_base,
            max_tokens=512,
            temperature=0.1,
        )

        response_text = response.choices[0].message.content
        action_str = self._parse_uitars_action(response_text)

        # Convert UI-TARS action string to action dict
        action_dict = self._uitars_to_action_dict(action_str)
        is_complete = action_dict.get("action") == "FINISHED"

        return action_dict, is_complete

    def _call_claude_cua(
        self, plan_action: str, screenshot_b64: str, os_name: str
    ) -> tuple[dict, bool]:
        """Use Claude Computer Use as actor."""
        # Get API key from models.actor (if Claude CUA is configured as actor)
        claude_api_key = ""
        if self._syll_config:
            actor_ep = self._syll_config.resolve_endpoint("actor")
            claude_api_key = actor_ep.api_key or ""
        if not claude_api_key:
            return {"action": "ERROR", "value": "Claude API key not configured", "position": [0, 0]}, False

        from syll.agent.aloha.act.claude_cua_agent import ClaudeComputerUseAgent

        agent = ClaudeComputerUseAgent(api_key=claude_api_key)
        return agent.execute(instruction=plan_action, screenshot_b64=screenshot_b64, os_name=os_name)

    @staticmethod
    def _load_actor_system_prompt() -> str:
        """Load UI-TARS actor system prompt from template, with hardcoded fallback."""
        template_path = (
            Path(__file__).parent.parent / "aloha/act/prompt_templates/actor/system_ui_tars.txt"
        )
        try:
            if template_path.exists():
                return template_path.read_text(encoding="utf-8")
        except Exception:
            pass
        # Hardcoded fallback
        return (
            "You are a GUI agent. Given a planned action and a screenshot, "
            "output the exact action to execute.\n\n"
            "## Output Format\nAction: <action>\n\n"
            "## Action Space\n"
            "click(start='(x, y)')\nleft_click(start='(x, y)')\n"
            "right_click(start='(x, y)')\ndouble_click(start='(x, y)')\n"
            "drag(start='(x1, y1)', end='(x2, y2)')\n"
            "type(content='text')\nhotkey(key='ctrl+c')\n"
            "scroll(start='(x, y)', direction='up|down', amount=3)\n"
            "wait(seconds=2)\nfinished(content='summary')\n"
        )

    @staticmethod
    def _parse_uitars_action(text: str) -> str:
        """Parse action from UI-TARS response."""
        import re

        m = re.search(r"Action:\s*(.+?)$", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        return text.strip()

    @staticmethod
    def _uitars_to_action_dict(action_str: str) -> dict:
        """Convert UI-TARS action string to executor action dict."""
        import re

        if action_str.startswith("finished"):
            return {"action": "FINISHED", "value": "", "position": [0, 0]}

        if action_str.startswith("call_user"):
            return {"action": "ERROR", "value": "Needs human intervention", "position": [0, 0]}

        # Parse coordinates
        coords = re.findall(r"\((\d+)\s*,\s*(\d+)\)", action_str)

        if action_str.startswith(("click(", "left_click(")):
            if coords:
                return {"action": "CLICK", "value": "", "position": [int(coords[0][0]), int(coords[0][1])]}

        if action_str.startswith("right_click("):
            if coords:
                return {"action": "RIGHT_CLICK", "value": "", "position": [int(coords[0][0]), int(coords[0][1])]}

        if action_str.startswith("double_click("):
            if coords:
                return {"action": "DOUBLE_CLICK", "value": "", "position": [int(coords[0][0]), int(coords[0][1])]}

        if action_str.startswith("drag("):
            if len(coords) >= 2:
                return {
                    "action": "DRAG",
                    "value": [int(coords[0][0]), int(coords[0][1])],
                    "position": [int(coords[1][0]), int(coords[1][1])],
                }

        m = re.match(r"type\((?:content|text)=['\"](.+?)['\"]\)", action_str, re.DOTALL)
        if m:
            return {"action": "TYPE", "value": m.group(1), "position": [0, 0]}

        m = re.match(r"hotkey\((?:key=)?['\"](.+?)['\"]\)", action_str)
        if m:
            return {"action": "KEY", "value": m.group(1), "position": [0, 0]}

        if action_str.startswith("scroll("):
            direction_m = re.search(r"direction=['\"](\w+)['\"]", action_str)
            direction = direction_m.group(1) if direction_m else "down"
            amount_m = re.search(r"amount=(\d+)", action_str)
            amount = int(amount_m.group(1)) if amount_m else 3
            # Issue 2: Fix scroll direction — positive=down for pyautogui
            scroll_val = -amount if direction == "up" else amount
            pos = [int(coords[0][0]), int(coords[0][1])] if coords else [0, 0]
            return {"action": "SCROLL", "value": str(scroll_val), "position": pos}

        m = re.match(r"wait\((?:seconds=)?(\d+(?:\.\d+)?)?\)", action_str)
        if m:
            return {"action": "WAIT", "value": m.group(1) or "2", "position": [0, 0]}

        # Fallback: click
        if coords:
            return {"action": "CLICK", "value": "", "position": [int(coords[0][0]), int(coords[0][1])]}

        return {"action": "ERROR", "value": f"Cannot parse: {action_str}", "position": [0, 0]}

    async def _take_screenshot(self, step: int) -> str | None:
        """Capture the current screen, DPR-resize, then scale to model target."""
        try:
            import mss
            from PIL import Image

            path = str(self._screenshot_dir / f"planner_step_{step}.png")
            with mss.mss() as sct:
                selected = getattr(self._config, 'selected_screen', 0)
                monitor_idx = selected + 1  # mss: 0=all, 1=primary, 2=secondary...
                if monitor_idx >= len(sct.monitors):
                    monitor_idx = 1  # fallback to primary
                monitor = sct.monitors[monitor_idx]
                self._screen_offset = (monitor["left"], monitor["top"])

                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                logical_w = monitor["width"]
                logical_h = monitor["height"]
                if img.width > logical_w or img.height > logical_h:
                    img = img.resize((logical_w, logical_h), Image.LANCZOS)

                # Scale to WXGA/XGA/FWXGA target for model
                model_w, model_h = self._compute_model_size(img.width, img.height)
                if model_w < img.width:
                    img = img.resize((model_w, model_h), Image.LANCZOS)
                self._model_img_size = (img.width, img.height)

                img.save(path)
            return path
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return None

    @staticmethod
    def _compute_model_size(w: int, h: int) -> tuple[int, int]:
        """Match aspect ratio to XGA/WXGA/FWXGA target."""
        from syll.agent.tools.coordinate_transform import SCALING_TARGETS

        ratio = w / h
        for tw, th in SCALING_TARGETS.values():
            if abs(tw / th - ratio) < 0.02 and tw < w:
                return tw, th
        return (1280, 800)  # fallback WXGA

    @staticmethod
    def _key_screenshots(screenshots: list[str]) -> list[str]:
        """Return key screenshots: first + last."""
        if not screenshots:
            return []
        if len(screenshots) == 1:
            return screenshots[:]
        return [screenshots[0], screenshots[-1]]
