"""UI-TARS GUI automation tool.

Uses a vision-language model (UI-TARS) to interpret screenshots and
execute GUI actions via shared click backends.

Architecture follows UI-TARS-desktop patterns:
- Multi-turn conversation: each screenshot+response appended as user/assistant turns
- Sliding window: only last N screenshots kept with images, older ones text-only
- Three-layer retry (screenshot / model / execute)
- Stuck detection: if same action repeated 3 times, auto call_user
- call_user action for human intervention requests
- Key screenshots collection (first + last)
- LiteLLM for unified provider support
"""

import base64
import inspect
import mimetypes
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.events import Event, EventContent, EventSource, EventStore
from syll.agent.gui_click import (
    normalize_hotkey_sequence,
    open_desktop_app_with_shortcut,
    perform_click_sequence,
    perform_drag,
    perform_right_click,
    resolve_click_count,
    should_open_desktop_app_with_shortcut,
)
from syll.agent.tools.base import Tool, ToolResult

UITARS_SYSTEM_PROMPT = """You are a GUI agent. You are given a screenshot of the current screen.
You need to help the user accomplish their task by performing actions on the screen.

## Output Format

1. Output your reasoning as: Thought: <your reasoning>
2. Output one action per step as: Action: <action>

## Action Space

click(start='(x, y)')
left_click(start='(x, y)')
right_click(start='(x, y)')
double_click(start='(x, y)')
drag(start='(x1, y1)', end='(x2, y2)')
type(content='text')
hotkey(key='ctrl+c')
scroll(start='(x, y)', direction='up|down', amount=3)
wait(seconds=2)
call_user(content='message')
finished(content='summary')

## Rules

1. Use the coordinate system from the screenshot (pixel coordinates)
2. Be precise with click targets
3. Wait after actions that trigger loading
4. If you are stuck, the action is not working, or you need human help, use call_user()
5. When the task is complete, use finished() with a summary
6. If you see a permission/authorization dialog, use call_user() to ask the user
"""

MAX_SCREENSHOT_HISTORY = 5  # sliding window: images for last N screenshots
MAX_REPEAT_ACTIONS = 3  # if same action repeated this many times, auto call_user


@dataclass
class RetryConfig:
    """Retry limits for each phase."""

    screenshot: int = 2
    model: int = 2
    execute: int = 1


@dataclass
class Conversation:
    """A single turn in the UI-TARS conversation."""

    role: str  # "user" or "assistant"
    text: str | None = None
    screenshot_b64: str | None = None  # base64 encoded screenshot
    screenshot_mime: str = "image/png"
    is_icl: bool = False  # True for in-context learning examples


class UITarsTool(Tool):
    """GUI automation tool powered by UI-TARS vision-language model."""

    def __init__(
        self,
        gui_config: Any,
        gui_skill_store: Any = None,
        aloha_skill_store: Any = None,
        syll_config: Any = None,
    ):
        self._config = gui_config
        self._gui_skill_store = gui_skill_store
        self._aloha_skill_store = aloha_skill_store
        self._syll_config = syll_config
        self._screenshot_dir = Path(tempfile.gettempdir()) / "syll_gui"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._retry = RetryConfig()
        self._event_store: EventStore | None = None
        self._model_img_size: tuple[int, int] = (0, 0)  # set by _take_screenshot

    @property
    def name(self) -> str:
        return "gui_action"

    @property
    def description(self) -> str:
        return (
            "Perform GUI actions on the desktop screen. Takes a screenshot, sends it to "
            "UI-TARS vision model for analysis, and executes the recommended action. "
            "Use this for tasks that require interacting with desktop applications."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "What GUI action to perform, e.g. 'click the Submit button' or 'open Chrome and navigate to google.com'",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Maximum number of steps to execute (default: from config)",
                    "minimum": 1,
                    "maximum": 50,
                },
                "skill_name": {
                    "type": "string",
                    "description": "Name of a recorded GUI skill to inject as in-context learning examples",
                },
            },
            "required": ["instruction"],
        }

    async def execute(
        self, instruction: str, max_steps: int | None = None,
        skill_name: str | None = None, progress_callback: Any = None, **kwargs: Any,
    ) -> str | ToolResult:
        """Execute a GUI task using multi-turn screenshot -> UI-TARS -> action loop.

        Following UI-TARS-desktop pattern: each step's screenshot and model response
        are accumulated as conversation turns, so the model has full context.
        """
        steps = max_steps or self._config.max_steps
        conversations: list[Conversation] = []
        screenshots: list[str] = []  # file paths for returning to user
        recent_actions: list[str] = []  # for stuck detection

        # Inject ICL examples from recorded GUI skill
        if skill_name:
            icl_turns = self._build_icl_context(skill_name)
            if icl_turns:
                conversations.extend(icl_turns)
                logger.info(f"Injected {len(icl_turns)} ICL turns from skill '{skill_name}'")

        for step in range(1, steps + 1):
            logger.info(f"GUI step {step}/{steps}: {instruction}")
            await self._emit_progress(progress_callback, {"kind": "gui_step", "step": step, "message": f"GUI step {step}/{steps}"})

            # --- Screenshot with retry ---
            screenshot_path = await self._take_screenshot_with_retry(step)
            if not screenshot_path:
                return ToolResult(text="Error: Failed to capture screenshot after retries")
            screenshots.append(screenshot_path)
            await self._emit_progress(
                progress_callback,
                {"kind": "screenshot", "step": step, "message": "Captured screenshot", "screenshot": screenshot_path},
            )

            # Read screenshot as base64
            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode()

            # Add screenshot as user turn
            conversations.append(Conversation(
                role="user",
                screenshot_b64=screenshot_b64,
                screenshot_mime=self._guess_image_mime(Path(screenshot_path)),
            ))

            # --- Call UI-TARS with retry (multi-turn conversation) ---
            response_text = await self._call_uitars_with_retry(
                instruction, conversations
            )
            if not response_text:
                return ToolResult(
                    text="Error: UI-TARS API call failed after retries",
                    media=[screenshot_path],
                )

            # Parse response and add as assistant turn
            thought, action_str = self._parse_response(response_text)
            conversations.append(Conversation(
                role="assistant",
                text=response_text,
            ))
            logger.info(f"  Thought: {thought}")
            logger.info(f"  Action: {action_str}")
            if thought:
                await self._emit_progress(progress_callback, {"kind": "gui_thought", "step": step, "message": thought, "thought": thought})
            await self._emit_progress(progress_callback, {"kind": "gui_action", "step": step, "message": action_str, "action": action_str})

            # --- Stuck detection ---
            recent_actions.append(action_str)
            if len(recent_actions) >= MAX_REPEAT_ACTIONS:
                last_n = recent_actions[-MAX_REPEAT_ACTIONS:]
                if all(a == last_n[0] for a in last_n):
                    logger.warning(f"Stuck: same action repeated {MAX_REPEAT_ACTIONS} times")
                    return ToolResult(
                        text=f"GUI agent appears stuck — repeated action '{action_str}' "
                             f"{MAX_REPEAT_ACTIONS} times. The action may not be working. "
                             f"Please check the screen and try a different approach.",
                        media=[screenshot_path],
                    )

            # Check for finished
            finished_match = re.match(r"finished\((?:content=)?['\"]?(.+?)['\"]?\)", action_str)
            if finished_match:
                summary = finished_match.group(1)
                key_shots = self._key_screenshots(screenshots)
                return ToolResult(
                    text=f"GUI task completed: {summary}\n\nSteps taken: {step}",
                    media=key_shots,
                )

            # Check for call_user
            call_user_match = re.match(r"call_user\((?:content=)?['\"]?(.+?)['\"]?\)", action_str)
            if call_user_match:
                message = call_user_match.group(1)
                return ToolResult(
                    text=f"GUI agent requests human intervention: {message}",
                    media=[screenshot_path],
                )

            # --- Execute action with retry ---
            intent_text = "\n".join(part for part in (instruction, thought) if part)
            success, msg = await self._execute_action_with_retry(action_str, intent_text=intent_text)
            await self._emit_progress(progress_callback, {"kind": "gui_result", "step": step, "message": msg, "result": msg})
            if not success:
                return ToolResult(
                    text=f"Action failed at step {step}: {msg}",
                    media=[screenshot_path],
                )

            # Log GUI action event
            if self._event_store:
                event = Event(
                    agent_type="gui_agent",
                    event_type="action",
                    source=EventSource(platform="desktop", chat_id="gui", user_id="system"),
                    content=EventContent(
                        text=f"Instruction: {instruction}\nThought: {thought}\nAction: {action_str}",
                        media=[screenshot_path],
                        metadata={
                            "step": step,
                            "action": action_str,
                            "thought": thought,
                            "skill_name": skill_name,
                        },
                    ),
                )
                self._event_store.log_event(event)

        # Max steps reached
        key_shots = self._key_screenshots(screenshots)
        return ToolResult(
            text=f"Reached max steps ({steps}). The task may not be complete.",
            media=key_shots,
        )

    def _key_screenshots(self, screenshots: list[str]) -> list[str]:
        """Return key screenshots: first + last (deduplicated)."""
        if not screenshots:
            return []
        if len(screenshots) == 1:
            return screenshots[:]
        return [screenshots[0], screenshots[-1]]

    @staticmethod
    async def _emit_progress(progress_callback: Any, event: dict[str, Any]) -> None:
        if not progress_callback:
            return
        try:
            result = progress_callback(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.debug(f"UI-TARS progress callback failed: {exc}")

    # ----- Retry wrappers -----

    async def _take_screenshot_with_retry(self, step: int) -> str | None:
        for attempt in range(self._retry.screenshot + 1):
            result = await self._take_screenshot(step)
            if result:
                return result
            logger.warning(f"Screenshot attempt {attempt + 1} failed, retrying...")
        return None

    async def _call_uitars_with_retry(
        self, instruction: str, conversations: list[Conversation]
    ) -> str | None:
        for attempt in range(self._retry.model + 1):
            result = await self._call_uitars(instruction, conversations)
            if result:
                return result
            logger.warning(f"UI-TARS API attempt {attempt + 1} failed, retrying...")
        return None

    async def _execute_action_with_retry(
        self, action_str: str, intent_text: str = ""
    ) -> tuple[bool, str]:
        for attempt in range(self._retry.execute + 1):
            success, msg = await self._execute_action(action_str, intent_text=intent_text)
            if success:
                return success, msg
            logger.warning(f"Action attempt {attempt + 1} failed: {msg}")
        return False, msg  # type: ignore[possibly-undefined]

    # ----- Core methods -----

    async def _take_screenshot(self, step: int) -> str | None:
        """Capture the current screen, DPR-resize, then scale to model target resolution."""
        try:
            import mss
            from PIL import Image

            path = str(self._screenshot_dir / f"step_{step}.png")
            with mss.mss() as sct:
                selected = getattr(self._config, 'selected_screen', 0)
                monitor_idx = selected + 1  # mss: 0=all, 1=primary, 2=secondary...
                if monitor_idx >= len(sct.monitors):
                    monitor_idx = 1  # fallback to primary
                monitor = sct.monitors[monitor_idx]
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

                # DPR-aware: resize Retina screenshots to logical size
                logical_w = monitor["width"]
                logical_h = monitor["height"]
                if img.width > logical_w or img.height > logical_h:
                    img = img.resize((logical_w, logical_h), Image.LANCZOS)

                # Scale to WXGA/XGA/FWXGA target for model (matches ShowUI-Aloha)
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
        """Match aspect ratio to XGA/WXGA/FWXGA target (same logic as ShowUI-Aloha)."""
        from syll.agent.tools.coordinate_transform import SCALING_TARGETS

        ratio = w / h
        for tw, th in SCALING_TARGETS.values():
            if abs(tw / th - ratio) < 0.02 and tw < w:
                return tw, th
        return (1280, 800)  # fallback WXGA

    async def _call_uitars(
        self,
        instruction: str,
        conversations: list[Conversation],
    ) -> str | None:
        """Call UI-TARS via LiteLLM with multi-turn conversation history.

        Following UI-TARS-desktop pattern:
        - First message includes system prompt + instruction as user text
        - Each step adds: user (screenshot image) → assistant (thought+action)
        - Sliding window: only last N screenshots include base64 images
        """
        try:
            import litellm

            messages: list[dict] = []

            # System message
            messages.append({"role": "system", "content": UITARS_SYSTEM_PROMPT})

            # First user message with instruction
            messages.append({"role": "user", "content": f"Task: {instruction}"})

            # Build conversation turns with sliding window for images
            # Only include base64 images for the last MAX_SCREENSHOT_HISTORY screenshots
            # ICL turns are never evicted from the window
            user_turns_with_images = [
                i for i, c in enumerate(conversations)
                if c.role == "user" and c.screenshot_b64 and not c.is_icl
            ]
            image_start_idx = max(0, len(user_turns_with_images) - MAX_SCREENSHOT_HISTORY)
            image_turn_indices = set(user_turns_with_images[image_start_idx:])

            for i, conv in enumerate(conversations):
                if conv.role == "user" and conv.screenshot_b64:
                    if i in image_turn_indices or conv.is_icl:
                        # Include image
                        messages.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{conv.screenshot_mime};base64,{conv.screenshot_b64}"
                                    },
                                }
                            ],
                        })
                    else:
                        # Older screenshot — text placeholder only
                        messages.append({
                            "role": "user",
                            "content": "[Screenshot taken]",
                        })
                elif conv.role == "assistant" and conv.text:
                    messages.append({
                        "role": "assistant",
                        "content": conv.text,
                    })

            # Determine model string from purpose-based config
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
                messages=messages,
                api_key=api_key,
                api_base=api_base,
                max_tokens=1024,
                temperature=0.1,
            )

            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"UI-TARS API call failed: {e}")
            return None

    def _parse_response(self, text: str) -> tuple[str, str]:
        """Parse UI-TARS response into (thought, action)."""
        thought = ""
        action = ""

        thought_match = re.search(r"Thought:\s*(.+?)(?=Action:|$)", text, re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()

        action_match = re.search(r"Action:\s*(.+?)$", text, re.DOTALL)
        if action_match:
            action = action_match.group(1).strip()

        if not action:
            # Fallback: treat entire response as action
            action = text.strip()

        return thought, action

    def _build_icl_context(self, skill_name: str) -> list[Conversation]:
        """Build ICL (in-context learning) turns from a recorded GUI skill.

        Tries AlohaSkillStore first (richer trace data), then falls back to GUISkillStore.
        """
        # Try Aloha skill store first
        if self._aloha_skill_store:
            aloha_skill = self._aloha_skill_store.load_skill(skill_name)
            if aloha_skill and aloha_skill.steps:
                return self._build_aloha_icl(aloha_skill, skill_name)

        # Fall back to original GUISkillStore
        if not self._gui_skill_store:
            logger.warning("No GUI skill store available for ICL injection")
            return []

        skill = self._gui_skill_store.load_skill(skill_name)
        if not skill or not skill.steps:
            logger.warning(f"GUI skill '{skill_name}' not found or has no steps")
            return []

        turns: list[Conversation] = []
        for step in skill.steps:
            # Load keyframe screenshot as base64
            kf_path = self._gui_skill_store.get_keyframe_path(
                skill_name, step.screenshot_before
            )
            if not kf_path:
                logger.warning(
                    f"Keyframe {step.screenshot_before} not found for skill '{skill_name}'"
                )
                continue

            screenshot_b64 = base64.b64encode(kf_path.read_bytes()).decode()

            # User turn: screenshot
            turns.append(Conversation(
                role="user",
                screenshot_b64=screenshot_b64,
                screenshot_mime=self._guess_image_mime(kf_path),
                is_icl=True,
            ))

            # Assistant turn: thought + action
            action_str = self._format_action(step.action)
            thought = step.action.description or f"Perform {step.action.type}"
            response_text = f"Thought: {thought}\nAction: {action_str}"
            turns.append(Conversation(
                role="assistant",
                text=response_text,
                is_icl=True,
            ))

        return turns

    def _build_aloha_icl(self, skill, skill_name: str) -> list[Conversation]:
        """Build ICL turns from an AlohaSkill with richer trace context."""
        turns: list[Conversation] = []

        for step in skill.steps:
            # Prefer crop image (smaller, more focused), fallback to full
            kf_filename = step.screenshot_crop or step.screenshot
            if not kf_filename:
                logger.warning(
                    f"Aloha skill '{skill_name}' step {step.index} has no keyframe filenames for ICL"
                )
                continue

            kf_path = self._aloha_skill_store.get_keyframe_path(skill_name, kf_filename)
            if not kf_path:
                # Try the other image
                logger.warning(
                    f"Aloha skill '{skill_name}' step {step.index} missing keyframe '{kf_filename}', "
                    "trying fallback"
                )
                alt_filename = step.screenshot if kf_filename == step.screenshot_crop else step.screenshot_crop
                if alt_filename:
                    kf_path = self._aloha_skill_store.get_keyframe_path(skill_name, alt_filename)
                if not kf_path:
                    logger.warning(
                        f"Aloha skill '{skill_name}' step {step.index} has no usable keyframe for ICL"
                    )
                    continue

            screenshot_b64 = base64.b64encode(kf_path.read_bytes()).decode()

            # User turn: screenshot
            turns.append(Conversation(
                role="user",
                screenshot_b64=screenshot_b64,
                screenshot_mime=self._guess_image_mime(kf_path),
                is_icl=True,
            ))

            # Assistant turn: use trace data for richer context if available
            if step.trace:
                thought = step.trace.think or step.trace.observation or step.action.description
            else:
                thought = step.action.description or f"Perform {step.action.type}"
                logger.warning(
                    f"Aloha skill '{skill_name}' step {step.index} has no trace; using action description"
                )

            action_str = self._format_aloha_icl_action(step)
            response_text = f"Thought: {thought}\nAction: {action_str}"
            turns.append(Conversation(
                role="assistant",
                text=response_text,
                is_icl=True,
            ))

        return turns

    def _format_aloha_icl_action(self, step) -> str:
        """Format Aloha demo steps for richer ICL examples."""
        intent_text = "\n".join(
            part
            for part in (
                getattr(step.trace, "action", "") if step.trace else "",
                getattr(step.trace, "think", "") if step.trace else "",
                step.action.description,
            )
            if part
        )
        if step.action.type in ("click", "left_click"):
            click_count = resolve_click_count("CLICK", {"intent": intent_text})
            if click_count >= 2 and step.action.coordinates:
                x, y = step.action.coordinates
                return f"double_click(start='({x}, {y})')"
        return self._format_action(step.action)

    @staticmethod
    def _guess_image_mime(path: Path) -> str:
        """Guess image MIME type from a keyframe or screenshot path."""
        mime, _ = mimetypes.guess_type(str(path))
        if mime and mime.startswith("image/"):
            return mime
        return "image/png"

    @staticmethod
    def _format_action(action) -> str:
        """Format a GUIAction into a UI-TARS action string."""
        if action.type in ("click", "left_click"):
            if action.coordinates:
                return f"click(start='({action.coordinates[0]}, {action.coordinates[1]})')"
            return "click()"
        elif action.type == "double_click":
            if action.coordinates:
                return f"double_click(start='({action.coordinates[0]}, {action.coordinates[1]})')"
            return "double_click()"
        elif action.type == "right_click":
            if action.coordinates:
                return f"right_click(start='({action.coordinates[0]}, {action.coordinates[1]})')"
            return "right_click()"
        elif action.type == "drag":
            start = action.coordinates or [0, 0]
            end = action.end_coordinates or [0, 0]
            return f"drag(start='({start[0]}, {start[1]})', end='({end[0]}, {end[1]})')"
        elif action.type == "type":
            return f"type(content='{action.content}')"
        elif action.type == "hotkey":
            return f"hotkey(key='{action.content}')"
        elif action.type == "scroll":
            if action.coordinates:
                return f"scroll(start='({action.coordinates[0]}, {action.coordinates[1]})', direction='{action.content or 'down'}', amount=3)"
            return f"scroll(direction='{action.content or 'down'}', amount=3)"
        elif action.type == "wait":
            return f"wait(seconds={action.content or '2'})"
        return f"{action.type}()"

    @staticmethod
    def _parse_coords(s: str) -> tuple[int, int] | None:
        """Extract (x, y) coordinates from various formats.

        Supports:
        - <point>x y</point>
        - (x, y) or (x,y)
        """
        # Format: <point>x y</point>
        m = re.search(r"<point>(\d+)\s+(\d+)</point>", s)
        if m:
            return int(m.group(1)), int(m.group(2))
        # Format: (x, y) or (x,y)
        m = re.search(r"\((\d+)\s*,\s*(\d+)\)", s)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None

    async def _execute_action(
        self, action_str: str, intent_text: str = ""
    ) -> tuple[bool, str]:
        """Execute a parsed GUI action via shared click backends.

        Supports both UI-TARS v1 format (<point>x y</point>) and
        UI-TARS v1.5 format ((x, y)).
        """
        try:
            import pyautogui

            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.3

            if re.match(r"(?:left_)?click\(", action_str):
                coords = self._parse_coords(action_str)
                if coords:
                    x, y = self._transform_coords(*coords)
                    raw = {"intent": intent_text, "action_text": action_str}
                    if should_open_desktop_app_with_shortcut("CLICK", raw):
                        message = await open_desktop_app_with_shortcut(
                            pyautogui,
                            x,
                            y,
                            raw=raw,
                            config=self._config,
                        )
                        return True, message
                    click_count = resolve_click_count("CLICK", raw)
                    message = await perform_click_sequence(
                        pyautogui,
                        x,
                        y,
                        click_count,
                        raw=raw,
                        config=self._config,
                    )
                    return True, message
                return False, f"Cannot parse coordinates: {action_str}"

            if re.match(r"right_click\(", action_str):
                coords = self._parse_coords(action_str)
                if coords:
                    x, y = self._transform_coords(*coords)
                    raw = {"intent": intent_text, "action_text": action_str}
                    message = await perform_right_click(
                        pyautogui,
                        x,
                        y,
                        raw=raw,
                        config=self._config,
                    )
                    return True, message
                return False, f"Cannot parse coordinates: {action_str}"

            if re.match(r"double_click\(", action_str):
                coords = self._parse_coords(action_str)
                if coords:
                    x, y = self._transform_coords(*coords)
                    raw = {"intent": intent_text, "action_text": action_str}
                    if should_open_desktop_app_with_shortcut("DOUBLE_CLICK", raw):
                        message = await open_desktop_app_with_shortcut(
                            pyautogui,
                            x,
                            y,
                            raw=raw,
                            config=self._config,
                        )
                        return True, message
                    message = await perform_click_sequence(
                        pyautogui,
                        x,
                        y,
                        2,
                        raw=raw,
                        config=self._config,
                    )
                    return True, message
                return False, f"Cannot parse coordinates: {action_str}"

            if re.match(r"drag\(", action_str):
                all_coords = re.findall(r"\((\d+)\s*,\s*(\d+)\)", action_str)
                if not all_coords:
                    all_coords = re.findall(r"<point>(\d+)\s+(\d+)</point>", action_str)
                if len(all_coords) >= 2:
                    x1, y1 = self._transform_coords(
                        int(all_coords[0][0]), int(all_coords[0][1])
                    )
                    x2, y2 = self._transform_coords(
                        int(all_coords[1][0]), int(all_coords[1][1])
                    )
                    raw = {"intent": intent_text, "action_text": action_str}
                    message = await perform_drag(
                        pyautogui,
                        (x1, y1),
                        (x2, y2),
                        raw=raw,
                        config=self._config,
                    )
                    return True, message
                return False, f"Cannot parse drag coordinates: {action_str}"

            m = re.match(r'type\((?:content|text)=[\'"](.+?)[\'"]\)', action_str, re.DOTALL)
            if m:
                text = m.group(1)
                if text.isascii():
                    pyautogui.typewrite(text, interval=0.02)
                else:
                    self._type_unicode(text)
                return True, f"Typed: {text[:50]}..."

            m = re.match(r'hotkey\((?:key=)?[\'"](.+?)[\'"]\)', action_str)
            if m:
                keys = normalize_hotkey_sequence(m.group(1))
                if len(keys) >= 2:
                    pyautogui.hotkey(*keys)
                    return True, f"Pressed: {'+'.join(keys)}"
                if len(keys) == 1:
                    pyautogui.press(keys[0])
                    return True, f"Pressed: {keys[0]}"
                return False, f"Cannot parse hotkey: {action_str}"

            if re.match(r"scroll\(", action_str):
                coords = self._parse_coords(action_str)
                if coords:
                    x, y = self._transform_coords(*coords)
                else:
                    x, y = 0, 0
                direction_m = re.search(r'direction=[\'"](\w+)[\'"]', action_str)
                direction = direction_m.group(1) if direction_m else "down"
                amount_m = re.search(r"amount=(\d+)", action_str)
                amount = int(amount_m.group(1)) if amount_m else 3
                clicks = amount if direction == "up" else -amount
                pyautogui.scroll(clicks, x=x, y=y)
                return True, f"Scrolled {direction} {amount} at ({x}, {y})"

            m = re.match(r"wait\(seconds=(\d+(?:\.\d+)?)\)", action_str)
            if m:
                import asyncio

                seconds = float(m.group(1))
                await asyncio.sleep(seconds)
                return True, f"Waited {seconds}s"

            return False, f"Unknown action: {action_str}"

        except Exception as e:
            logger.error(f"Action execution error: {e}")
            return False, str(e)

    def _transform_coords(self, x: int, y: int) -> tuple[int, int]:
        """Transform model coordinates through the unified pipeline."""
        try:
            from syll.agent.tools.coordinate_transform import (
                ActorSpace,
                ActorType,
                CoordinateTransformService,
            )

            workspace = getattr(self._syll_config, 'workspace_path', None)
            if not workspace:
                return x, y
            service = CoordinateTransformService(workspace / "coord_profiles")
            selected = getattr(self._config, 'selected_screen', 0)
            ctx = service.get_frame_context(selected)
            profile = service.load_profile(selected)
            # Pass model image dimensions so Step 1 reverse-scales from WXGA
            mw, mh = self._model_img_size
            actor = ActorSpace(actor_type=ActorType.UI_TARS, api_width=mw, api_height=mh)
            result = service.model_to_executor(x, y, ctx, actor, profile)
            return result.executor_x, result.executor_y
        except Exception as e:
            logger.debug(f"Coordinate transform fallback (no-op): {e}")
            return x, y

    @staticmethod
    def _type_unicode(text: str) -> None:
        """Type unicode text using pyperclip + paste shortcut."""
        try:
            import platform

            import pyautogui
            import pyperclip

            pyperclip.copy(text)
            if platform.system() == "Darwin":
                pyautogui.hotkey("command", "v")
            else:
                pyautogui.hotkey("ctrl", "v")
        except ImportError:
            import pyautogui

            # Fallback: type char by char (may not work for all unicode)
            for ch in text:
                pyautogui.press(ch) if ch.isascii() else None
