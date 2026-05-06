"""Lightweight heuristics for aligning replies with the user's language."""

from __future__ import annotations

import re
from typing import Literal

LanguageCode = Literal["en", "zh", "mixed", "unknown"]

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_SCRIPT_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z]")


def infer_user_language(text: str) -> LanguageCode:
    """Infer the dominant language for the current user turn.

    The heuristic intentionally favors Chinese when the message is mostly
    Chinese with embedded English product names, while still locking onto
    English for fully-English requests.
    """

    if not text:
        return "unknown"

    stripped = text.strip()
    if not stripped:
        return "unknown"

    cjk = len(_CJK_RE.findall(stripped))
    latin = len(_LATIN_RE.findall(stripped))

    if cjk and not latin:
        return "zh"
    if latin and not cjk:
        return "en"
    if not cjk and not latin:
        return "unknown"

    first_script = next((m.group(0) for m in _SCRIPT_RE.finditer(stripped)), "")
    if _CJK_RE.fullmatch(first_script or "") and cjk >= 2:
        return "zh"
    if _LATIN_RE.fullmatch(first_script or "") and latin >= 4 and cjk <= 2:
        return "en"

    if cjk >= 4 and cjk * 2 >= latin:
        return "zh"
    if latin >= 12 and latin >= cjk * 3:
        return "en"

    return "mixed"


def build_turn_language_note(text: str) -> str:
    """Return a concise per-turn reply-language instruction."""

    language = infer_user_language(text)
    if language == "zh":
        return (
            "Latest user message language: Chinese. "
            "Your final reply for this turn must stay in Chinese unless the user explicitly asks to switch."
        )
    if language == "en":
        return (
            "Latest user message language: English. "
            "Your final reply for this turn must stay in English unless the user explicitly asks to switch."
        )
    if language == "mixed":
        return (
            "Latest user message mixes Chinese and English. "
            "Mirror that mix and do not switch to a different language on your own."
        )
    return ""


def localized_text(
    language: LanguageCode,
    *,
    zh: str,
    en: str,
    mixed: str | None = None,
    unknown: str | None = None,
) -> str:
    """Pick a localized string from a small zh/en set."""

    if language == "zh":
        return zh
    if language == "en":
        return en
    if language == "mixed" and mixed:
        return mixed
    if language == "unknown" and unknown:
        return unknown
    return mixed or en
