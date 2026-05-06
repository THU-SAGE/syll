"""Trace generator: generates natural-language step descriptions via LLM.

Adapted from ShowUI-Aloha/Aloha_Learn/trace_generator.py.
Uses litellm for unified LLM access instead of direct OpenAI/Claude API calls.
"""

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger


class TraceGenerator:
    """Generate step-by-step traces from GUI actions with crop+full screenshots."""

    def __init__(
        self,
        default_prompt: dict | None = None,
        model: str = "gpt-4o",
        api_key: str | None = None,
        api_base: str | None = None,
    ):
        if default_prompt is None:
            prompt_path = Path(__file__).parent / "default_prompt.json"
            with open(prompt_path, "r", encoding="utf-8") as f:
                self.default_prompt = json.load(f)
        else:
            self.default_prompt = default_prompt

        self.model = model
        self.api_key = api_key
        self.api_base = api_base

    @staticmethod
    def _val(d: dict[str, Any], *keys, default=""):
        """Get dictionary value by key (case-insensitive)."""
        for k in keys:
            if k in d:
                return d[k]
            for dk in d:
                if dk.lower() == k.lower():
                    return d[dk]
        return default

    @staticmethod
    def _encode_image(path: str) -> str | None:
        """Read image file and return base64 data URI."""
        if not path or not Path(path).exists():
            return None
        with open(path, "rb") as f:
            return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("utf-8")

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Extract first valid JSON object from a string."""
        if not text:
            return {}
        blocks = re.findall(r"\{.*\}", text, flags=re.S)
        for b in blocks:
            try:
                return json.loads(b)
            except Exception:
                continue
        try:
            return json.loads(text)
        except Exception:
            return {}

    @staticmethod
    def _sanitize_caption(cap: dict[str, str]) -> dict[str, str]:
        """Clean LLM output: strip coordinates, enforce crop-first observation."""
        patterns = [
            r"\bcoordinates?\b.*?\[[^\]]*\]",
            r"\bcoordinates?\b",
            r"\b(x\s*[:=]?\s*\d+|y\s*[:=]?\s*\d+)",
        ]

        def scrub(s: str) -> str:
            if not isinstance(s, str):
                return s
            for p in patterns:
                s = re.sub(p, "", s, flags=re.I)
            s = re.sub(r"\[\s*\d+\s*,\s*\d+\s*\]", "", s)
            return re.sub(r"\s{2,}", " ", s).strip()

        for k in ("observation", "think", "action", "expectation"):
            cap[k] = scrub(cap.get(k, ""))

        if re.search(r"\brelease\b.*\b(title\s*bar|top[-\s]*left)\b", cap.get("action", ""), re.I):
            cap["action"] = "Click the control shown in the cropped image"
            cap["think"] = "The cropped image shows a single actionable control; clicking it fulfills the intent."

        if not cap.get("observation", "").lower().startswith("cropped image shows"):
            cap["observation"] = ("Cropped image shows " + cap.get("observation", "")).strip()

        return cap

    def _prompt(
        self,
        action: dict[str, Any],
        overall_task: str,
        step_idx: int,
        recent_steps: list[dict],
    ) -> str:
        """Build the step prompt."""
        base = self.default_prompt.get("Base Prompt", "")
        deltas_cfg = self.default_prompt.get("Deltas", {})
        modifier_guide = self.default_prompt.get("Modifier_Guide", "")
        sw = action.get("current_software") or ""
        ts = action.get("timestamp")
        act = (action.get("action") or "").strip()
        recent_json = json.dumps(recent_steps, ensure_ascii=False, indent=2)

        delta = self._action_delta(act, action, deltas_cfg, modifier_guide)

        return f"""{base}

Recent Steps (most recent first, up to 3):
{recent_json}

Step Index: {step_idx}
Action: {act}
Software: {sw}
Timestamp: {ts}s
Overall Task: {overall_task}

{delta}

Respond with JSON only. If your first attempt is not valid JSON, immediately re-emit a corrected JSON."""

    def _action_delta(
        self, act_str: str, action: dict, deltas_cfg: dict, modifier_guide: str
    ) -> str:
        a = (act_str or "").lower()
        key = None
        if "dragstart" in a or a.startswith("drag"):
            key = "Drag"
        elif "rclick" in a or "right click" in a:
            key = "RClick"
        elif "dbl" in a or "double" in a:
            key = "DblClick"
        elif "wheel" in a or "scroll" in a:
            key = "MouseWheel"
        elif "type" in a or "input" in a or "key" in a:
            key = "Type"
        elif "click" in a:
            key = "Click"

        if not key:
            return "ActionTypeDelta: (none)"

        text = deltas_cfg.get(key, "")
        mod_txt = self._modifiers_text(action, modifier_guide)
        return text.replace("<MODIFIER_GUIDE>", mod_txt)

    @staticmethod
    def _modifiers_text(action: dict, modifier_guide: str) -> str:
        mods = action.get("modifiers") or action.get("modifier") or []
        if isinstance(mods, str):
            mods = [mods]
        mods = [m.capitalize() for m in mods if isinstance(m, str)]
        present = [m for m in ["Shift", "Ctrl", "Alt"] if m in mods]
        if not present:
            return ""
        guide_map = {
            "Shift": "Shift—add to selection / constrain proportions or direction",
            "Ctrl": "Ctrl—multi-select / special function / precise control",
            "Alt": "Alt—alternate mode / temporary tool / special functions",
        }
        tips = "; ".join(guide_map[m] for m in present)
        return f"Modifier: {tips}. "

    async def _call_llm(
        self, prompt: str, crop_b64: str | None, full_b64: str | None
    ) -> str:
        """Call LLM via litellm with images."""
        import litellm

        content: list[dict] = [{"type": "text", "text": prompt}]
        if crop_b64:
            content.append({"type": "image_url", "image_url": {"url": crop_b64}})
        if full_b64:
            content.append({"type": "image_url", "image_url": {"url": full_b64}})

        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base

        response = await litellm.acompletion(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            temperature=0.2,
            max_tokens=1200,
            **kwargs,
        )
        return response.choices[0].message.content

    async def generate_trace(
        self,
        actions_with_screenshots: list[dict],
        screenshots_dir: str | Path,
        overall_task: str = "",
    ) -> list[dict]:
        """Generate traces for actions that have screenshots.

        Args:
            actions_with_screenshots: List of action dicts with screenshot_crop/screenshot_full.
            screenshots_dir: Base directory for screenshots.
            overall_task: Overall task description for context.

        Returns:
            List of trajectory step dicts with step_idx and caption.
        """
        screenshots_dir = Path(screenshots_dir)
        traj: list[dict] = []
        step_idx = 1

        for it in actions_with_screenshots:
            act_str = (it.get("action") or "").strip()
            if act_str.upper() == "CONFIG" or act_str.startswith("Active Window"):
                continue

            crop = it.get("screenshot_crop") or ""
            full = it.get("screenshot_full") or ""
            if not crop and not full:
                continue

            if isinstance(crop, str) and crop.startswith("screenshots/"):
                crop = crop.replace("screenshots/", "")
            if isinstance(full, str) and full.startswith("screenshots/"):
                full = full.replace("screenshots/", "")

            crop_path = str(screenshots_dir / crop) if crop else None
            full_path = str(screenshots_dir / full) if full else None
            crop_b64 = self._encode_image(crop_path) if crop_path else None
            full_b64 = self._encode_image(full_path) if full_path else None
            if not crop_b64 and not full_b64:
                continue

            logger.info(f"Generating trace for step {step_idx}: {act_str}")

            recent = []
            for prev in reversed(traj[-3:]):
                cap = prev.get("caption", {}) or {}
                recent.append({
                    "step_idx": prev.get("step_idx"),
                    "Observation": cap.get("observation", ""),
                    "Think": cap.get("think", ""),
                    "Action": cap.get("action", ""),
                    "Expectation": cap.get("expectation", ""),
                })

            prompt = self._prompt(it, overall_task, step_idx, recent)
            txt = await self._call_llm(prompt, crop_b64, full_b64)

            data = self._extract_json(txt)
            cap = {
                "observation": self._val(data, "Observation", "observation", default=""),
                "think": self._val(data, "Think", "think", default=""),
                "action": self._val(data, "Action", "action", default=""),
                "expectation": self._val(data, "Expectation", "expectation", default=""),
            }
            cap = self._sanitize_caption(cap)

            traj.append({"step_idx": step_idx, "caption": cap})
            step_idx += 1
            time.sleep(0.1)

        return traj
