"""Recorded skill routing helper — matches user messages to reusable GUI skills."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from loguru import logger

from syll.utils.language import infer_user_language, localized_text

if TYPE_CHECKING:
    pass


def _normalize_for_match(text: str) -> str:
    """Normalize free-form text for cheap alias matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z\u3400-\u9fff]+", " ", text.lower())).strip()


def match_recorded_skill(agent_loop, content: str) -> dict | None:
    """Match user message content against recorded skill names and aliases.

    Returns the matched skill dict or None.
    """
    if not agent_loop.tools.has("gui_action_planned"):
        return None

    try:
        from syll.agent.recorded_skill import RecordedSkillStore

        store = RecordedSkillStore(agent_loop.workspace)
        skills = store.list_skills()
    except Exception:
        return None

    if not skills:
        return None

    normalized_content = _normalize_for_match(content)
    for s in skills:
        candidates = {
            s["name"],
            s["name"].replace("_", " "),
            s["name"].replace("-", " "),
        }
        candidates.update(s.get("aliases", []))
        for candidate in candidates:
            if not candidate:
                continue
            if _normalize_for_match(candidate) in normalized_content:
                return s

    return None


def inject_skill_hint(agent_loop, content: str) -> str:
    """If the message matches a recorded skill, append a system hint.

    Returns the original or augmented content.
    """
    match = match_recorded_skill(agent_loop, content)
    if not match:
        return content

    name = match["name"]
    desc = match.get("description") or name
    language = infer_user_language(content)
    instruction = (
        "gui_action_planned("
        f"instruction={json.dumps(desc, ensure_ascii=False)}, "
        f"skill_name={json.dumps(name, ensure_ascii=False)})"
    )
    hint_body = localized_text(
        language,
        zh=(
            f"这个请求命中了已录制的 GUI 技能 {json.dumps(name, ensure_ascii=False)}。"
            f"请使用 {instruction} 来执行，不要改用 shell 或 exec。"
        ),
        en=(
            f"This request matches the recorded GUI skill {json.dumps(name)}. "
            f"Use {instruction} to execute it. Do not use shell commands or exec."
        ),
    )
    hint_label = localized_text(language, zh="系统提示", en="System hint")
    hint = f"\n\n[{hint_label}: {hint_body}]"
    logger.debug(f"Injected recorded skill hint for '{name}'")
    return content + hint


def match_aloha_skill(agent_loop, content: str) -> dict | None:
    """Backward-compatible alias for older callers."""
    return match_recorded_skill(agent_loop, content)
