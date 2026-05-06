"""Planner: plans the next concrete UI action using LLM with trajectory guidance.

Adapted from ShowUI-Aloha/Aloha_Act/ui_aloha/act/gui_agent/planner/ui_aloha_planner.py.
Uses litellm instead of direct OpenAI calls.
"""

import asyncio
import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger

PROMPT_TEMPLATES_DIR = Path(__file__).parent / "prompt_templates"


class AlohaPlanner:
    """Plans the next UI action based on task, screenshot, trajectory guidance."""

    def __init__(
        self,
        model: str,
        os_name: str = "macOS",
        max_tokens: int = 8000,
        retry_attempts: int = 2,
        retry_delay_seconds: float = 1.0,
        api_key: str | None = None,
        api_base: str | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.os_name = os_name
        self.retry_attempts = retry_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.api_key = api_key
        self.api_base = api_base

        self._jinja_env = Environment(
            loader=FileSystemLoader(str(PROMPT_TEMPLATES_DIR)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    async def plan(
        self,
        task: str,
        guidance_trajectory: str = "",
        screenshot_b64: str = "",
        action_history: list[str] | None = None,
    ) -> dict:
        """Generate a plan for the next action.

        Args:
            task: The overall task description.
            guidance_trajectory: Formatted trajectory guidance string.
            screenshot_b64: Base64 encoded screenshot.
            action_history: List of previous action descriptions.

        Returns:
            Dict with Observation, Reasoning, Current Step, Action, Expectation.
        """
        import litellm

        system_prompt = self._get_system_prompt(guidance_trajectory)

        action_history = action_history or []
        action_history_str = ""
        if action_history:
            for i, action in enumerate(action_history):
                action_history_str += f"step {i + 1}: {action}\n"

        user_text = self._jinja_env.get_template("planner/user.txt").render(
            task=task,
            guidance_trajectory_example=guidance_trajectory,
            max_history_length=len(action_history),
            action_history_str=action_history_str,
        )

        # Build messages
        content: list[dict] = [{"type": "text", "text": user_text}]
        if screenshot_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
            })

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]

        kwargs = dict(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=0,
        )
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base

        response = await self._call_model_with_retry(litellm, kwargs)

        llm_response = response.choices[0].message.content

        # Parse response
        try:
            llm_response_json = self._extract_data(llm_response)
            parsed_dict = json.loads(llm_response_json)
            if not isinstance(parsed_dict, dict):
                raise ValueError("Not a JSON object")
        except Exception as e:
            logger.error(f"Failed to parse planner JSON: {e}")
            parsed_dict = {}

        # Parse current step
        current_step_raw = parsed_dict.get('Current Step in Guidance Trajectory')
        if isinstance(current_step_raw, str) and current_step_raw.strip():
            try:
                current_step_tuple = self._safer_parse_step_response(current_step_raw)
                parsed_dict['Current Step'] = current_step_tuple[0]
                parsed_dict['Current Step Explanation'] = current_step_tuple[1]
            except Exception:
                parsed_dict['Current Step'] = 1
                parsed_dict['Current Step Explanation'] = "Error parsing step"
        else:
            parsed_dict.setdefault('Current Step', 1)
            parsed_dict.setdefault('Current Step Explanation', "No step information")

        # Ensure required fields
        for field in ("Action", "Reasoning", "Observation", "Expectation"):
            parsed_dict.setdefault(field, "")

        return parsed_dict

    async def _call_model_with_retry(self, litellm: object, kwargs: dict) -> object:
        """Call the planner model with a small retry budget for flaky provider errors."""
        last_error: Exception | None = None

        for attempt in range(1, self.retry_attempts + 2):
            try:
                request_kwargs = dict(kwargs)
                if attempt > 1:
                    request_kwargs["messages"] = self._messages_with_json_reminder(
                        request_kwargs["messages"]
                    )
                return await litellm.acompletion(**request_kwargs)
            except Exception as exc:  # pragma: no cover - provider-specific subclasses
                last_error = exc
                if attempt > self.retry_attempts:
                    break
                logger.warning(
                    f"Planner model attempt {attempt} failed: {exc}. Retrying..."
                )
                await asyncio.sleep(self.retry_delay_seconds)

        assert last_error is not None
        raise last_error

    def _get_system_prompt(self, guidance_trajectory: str = "") -> str:
        return self._jinja_env.get_template("planner/system.txt").render(
            os_name=self.os_name,
            guidance_trajectory_example=guidance_trajectory,
        )

    @staticmethod
    def _messages_with_json_reminder(messages: list[dict]) -> list[dict]:
        """Add a terse retry hint asking for raw JSON only."""
        copied = [dict(message) for message in messages]
        if copied and copied[0].get("role") == "system":
            copied[0]["content"] = (
                f"{copied[0]['content']}\n\nReturn ONLY one raw JSON object. "
                "Do not use markdown fences or any extra prose."
            )
        return copied

    @staticmethod
    def _extract_data(text: str) -> str:
        """Extract JSON block from markdown-wrapped response."""
        # Try ```json ... ``` blocks first
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # Try raw JSON
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return m.group(0)
        return text

    @staticmethod
    def _safer_parse_step_response(s: str) -> tuple[int, str]:
        """Parse a string like '(4, explanation text)' into (int, str)."""
        s = s.strip()
        if s.startswith("(") and s.endswith(")"):
            content = s[1:-1]
        else:
            content = s

        match = re.match(r"\s*(\d+)\s*,\s*(.+)", content)
        if not match:
            raise ValueError(f"Could not parse step: {s}")

        return int(match.group(1)), match.group(2).strip().strip("'\"")
