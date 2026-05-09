"""Intent clarification agent for the Syll panel.

A lightweight one-shot LLM loop that takes natural-language requests like
"create a cron that reminds me to drink water every morning at 8" and
drives a short clarification dialogue until it has enough structured
fields to pre-fill the existing Skills / Schedule create forms.

Design notes:
- Reuses the running ``provider`` instance from ``AgentLoop`` instead of
  reconstructing LiteLLM from raw config, so provider-specific prefixes,
  api_base, and env-var injection stay intact.
- Output schema mirrors the frontend form state (``skillCreateForm`` /
  ``cronJobForm``) to avoid a two-layer conversion.
- v1 only supports ``action_type="message"`` for cron; other action types
  are rejected in the system prompt.
- Sessions are kept in-memory with a 30-minute lazy TTL. Single-process
  only; v2 may swap for a shared store.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

SESSION_TTL_SECONDS = 30 * 60
MAX_TURNS_PER_SESSION = 12


_SYSTEM_PROMPT = """You are Syll's config intent clarifier. Your job is to
turn a user's natural-language request into a structured pre-fill payload for
one of two targets: a "skill" (a markdown capability under syll/skills) or
a "cron" (a scheduled job).

Rules:
- You MUST respond with a single valid JSON object and nothing else. No prose
  outside the JSON, no markdown fences.
- Output schema:
  {
    "reply": string,            // short message shown to the user in THEIR language
    "status": "need_more" | "ready",
    "target": "skill" | "cron" | null,
    "skill": { ... } | null,    // only when target == "skill"
    "cron":  { ... } | null     // only when target == "cron"
  }
- When information is incomplete, set status="need_more" and use "reply" to
  ask ONE concise follow-up question. Don't ask more than 1-2 rounds total.
- Match the user's language in "reply" (Chinese → Chinese, English → English).
- If the request is not about creating a skill or cron, set target=null,
  status="ready", and politely decline in "reply".

Skill schema (when target="skill"):
  {
    "name": string,         // kebab-case ASCII, e.g. "paper-digest"
    "description": string,  // one sentence
    "template": "blank"     // always "blank" in v1
  }

Cron schema (when target="cron"):
  {
    "name": string,
    "action_type": "message",          // v1 ONLY supports "message"
    "message": string,                 // what the agent should do when fired
    "schedule_mode": "daily" | "interval" | "once" | "advanced",
    // daily mode:
    "daily_time": "HH:MM" | null,
    "daily_days": "every" | "weekdays" | "weekends" | "custom" | null,
    "daily_custom_days": [int] | null, // 0=Sun..6=Sat, only when daily_days="custom"
    // interval mode:
    "interval_value": int | null,
    "interval_unit": "second" | "minute" | "hour" | null,
    // once mode:
    "at_local": "YYYY-MM-DDTHH:MM" | null,   // local datetime-local format
    // advanced mode:
    "cron_expr": string | null         // standard 5-field cron expression
  }

Fill only the fields relevant to the chosen schedule_mode; leave the others
null. Never invent values the user didn't provide — ask instead.

If the user asks to schedule a GUI / recorded workflow skill, reply that v1 only supports
plain message reminders for now (status="ready", target=null).
"""


# ───────────────────────── Pydantic models ──────────────────────────


class SkillPrefill(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    template: str = "blank"


class CronPrefill(BaseModel):
    name: str = Field(..., min_length=1)
    action_type: Literal["message"] = "message"
    message: str = Field(..., min_length=1)
    schedule_mode: Literal["daily", "interval", "once", "advanced"]
    # daily
    daily_time: str | None = None
    daily_days: Literal["every", "weekdays", "weekends", "custom"] | None = None
    daily_custom_days: list[int] | None = None
    # interval
    interval_value: int | None = None
    interval_unit: Literal["second", "minute", "hour"] | None = None
    # once
    at_local: str | None = None
    # advanced
    cron_expr: str | None = None


class ClarifyResult(BaseModel):
    session_id: str
    reply: str
    status: Literal["need_more", "ready"]
    target: Literal["skill", "cron"] | None = None
    skill: SkillPrefill | None = None
    cron: CronPrefill | None = None


# ───────────────────────── Normalization ────────────────────────────


_WEEKDAY_WORDS = {
    "sun": 0, "sunday": 0, "周日": 0, "星期日": 0, "星期天": 0,
    "mon": 1, "monday": 1, "周一": 1, "星期一": 1,
    "tue": 2, "tues": 2, "tuesday": 2, "周二": 2, "星期二": 2,
    "wed": 3, "weds": 3, "wednesday": 3, "周三": 3, "星期三": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4, "周四": 4, "星期四": 4,
    "fri": 5, "friday": 5, "周五": 5, "星期五": 5,
    "sat": 6, "saturday": 6, "周六": 6, "星期六": 6,
}

_CRON_FIELD_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")


def _normalize_time(value: Any) -> str | None:
    """Accept 'HH:MM', 'HH:MM:SS', '8:00', '08点', etc. → 'HH:MM' or None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Strip Chinese time markers
    s = s.replace("点", ":").replace("时", ":").replace("分", "")
    # Pick first H(H):M(M) pattern
    m = re.search(r"(\d{1,2}):(\d{1,2})", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def _normalize_custom_days(raw: Any) -> list[int] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            # bool is int subclass — reject explicitly
            continue
        if isinstance(item, int) and 0 <= item <= 6:
            out.append(item)
            continue
        if isinstance(item, str):
            key = item.strip().lower()
            if key.isdigit():
                n = int(key)
                if 0 <= n <= 6:
                    out.append(n)
                    continue
            if key in _WEEKDAY_WORDS:
                out.append(_WEEKDAY_WORDS[key])
    # Dedup preserving order
    seen: set[int] = set()
    dedup: list[int] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            dedup.append(d)
    return dedup or None


def _normalize_interval_unit(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower().rstrip("s")
    if s in ("second", "minute", "hour"):
        return s
    # Map Chinese units
    mapping = {"秒": "second", "分": "minute", "分钟": "minute", "小时": "hour", "时": "hour"}
    return mapping.get(str(raw).strip())


def _normalize_at_local(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # datetime-local wants "YYYY-MM-DDTHH:MM". Accept space separator too.
    s = s.replace(" ", "T")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{1,2}):(\d{1,2})", s)
    if not m:
        return None
    date = m.group(1)
    hh = int(m.group(2))
    mm = int(m.group(3))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{date}T{hh:02d}:{mm:02d}"


def normalize_cron_prefill(raw: dict[str, Any]) -> CronPrefill:
    """Normalize a raw LLM cron payload into a validated ``CronPrefill``.

    Raises ``ValidationError`` if required fields for the chosen
    ``schedule_mode`` are missing after normalization.
    """
    data = dict(raw)  # shallow copy, we'll rewrite fields

    mode = data.get("schedule_mode")
    if mode == "daily":
        data["daily_time"] = _normalize_time(data.get("daily_time"))
        days = data.get("daily_days") or "every"
        if days not in ("every", "weekdays", "weekends", "custom"):
            days = "every"
        data["daily_days"] = days
        data["daily_custom_days"] = (
            _normalize_custom_days(data.get("daily_custom_days"))
            if days == "custom"
            else None
        )
        if not data.get("daily_time"):
            raise ValueError("daily mode requires daily_time (HH:MM)")
        if days == "custom" and not data.get("daily_custom_days"):
            raise ValueError("daily_days=custom requires at least one weekday")

    elif mode == "interval":
        try:
            data["interval_value"] = int(data.get("interval_value") or 0)
        except (TypeError, ValueError):
            raise ValueError("interval_value must be an integer") from None
        data["interval_unit"] = _normalize_interval_unit(data.get("interval_unit"))
        if data["interval_value"] <= 0:
            raise ValueError("interval_value must be > 0")
        if data["interval_unit"] not in ("second", "minute", "hour"):
            raise ValueError("interval_unit must be second/minute/hour")

    elif mode == "once":
        data["at_local"] = _normalize_at_local(data.get("at_local"))
        if not data["at_local"]:
            raise ValueError("once mode requires at_local (YYYY-MM-DDTHH:MM)")

    elif mode == "advanced":
        expr = (data.get("cron_expr") or "").strip()
        if not expr or not _CRON_FIELD_RE.match(expr):
            raise ValueError("advanced mode requires a valid 5-field cron_expr")
        data["cron_expr"] = expr

    else:
        raise ValueError(f"unknown schedule_mode: {mode!r}")

    # Clear fields unrelated to the chosen mode so Pydantic gets a clean shape.
    if mode != "daily":
        data["daily_time"] = None
        data["daily_days"] = None
        data["daily_custom_days"] = None
    if mode != "interval":
        data["interval_value"] = None
        data["interval_unit"] = None
    if mode != "once":
        data["at_local"] = None
    if mode != "advanced":
        data["cron_expr"] = None

    return CronPrefill(**data)


# ───────────────────────── Parsing helpers ──────────────────────────


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first top-level JSON object out of an LLM reply.

    Handles strict JSON mode (whole-string parse) and the sloppy case
    where the model wraps the object in prose or markdown fences.
    """
    if not text:
        raise ValueError("empty LLM response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to find the first '{' that starts a balanced object.
    depth = 0
    start: int | None = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start : i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    start = None
                    continue
    raise ValueError("no JSON object found in LLM response")


# ───────────────────────── IntentClarifier ──────────────────────────


class IntentClarifier:
    """In-memory, single-process intent clarifier.

    Construct with a live ``provider`` (from ``AgentLoop.provider``). If
    ``provider`` is ``None`` the clarifier can still be instantiated but
    ``clarify()`` will raise — the route layer is expected to check and
    return a 503.
    """

    def __init__(self, provider: Any):
        self._provider = provider
        self._sessions: dict[str, list[dict[str, str]]] = {}
        self._touched: dict[str, float] = {}

    @property
    def provider(self) -> Any:
        return self._provider

    def _gc(self) -> None:
        cutoff = time.time() - SESSION_TTL_SECONDS
        stale = [sid for sid, ts in self._touched.items() if ts < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)
            self._touched.pop(sid, None)

    async def clarify(self, session_id: str | None, user_text: str) -> ClarifyResult:
        if self._provider is None:
            raise RuntimeError("IntentClarifier has no LLM provider configured")
        if not user_text or not user_text.strip():
            raise ValueError("user_text must be non-empty")

        self._gc()

        sid = session_id or uuid.uuid4().hex
        messages = self._sessions.get(sid)
        if messages is None:
            messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        messages.append({"role": "user", "content": user_text.strip()})

        if len(messages) > MAX_TURNS_PER_SESSION * 2 + 1:
            # Hard cap — drop the session and force the user to restart.
            self._sessions.pop(sid, None)
            self._touched.pop(sid, None)
            return ClarifyResult(
                session_id=sid,
                reply="Conversation length limit reached — please restart and re-describe your request.",
                status="need_more",
                target=None,
            )

        response = await self._provider.chat(
            messages=messages,
            tools=None,
            max_tokens=1024,
            temperature=0.2,
        )
        raw = getattr(response, "content", None) or ""
        logger.debug(f"intent clarifier raw response: {raw[:500]}")

        try:
            parsed = _extract_json(raw)
        except ValueError as e:
            logger.warning(f"intent clarifier could not parse JSON: {e}")
            return ClarifyResult(
                session_id=sid,
                reply="Sorry, I couldn't parse that response. Could you try again?",
                status="need_more",
                target=None,
            )

        result = self._build_result(sid, parsed)

        # Persist the turn only after we have a valid assistant reply.
        messages.append({"role": "assistant", "content": raw})
        self._sessions[sid] = messages
        self._touched[sid] = time.time()

        # ready → session is done, drop it to save memory
        if result.status == "ready":
            self._sessions.pop(sid, None)
            self._touched.pop(sid, None)

        return result

    def _build_result(self, sid: str, parsed: dict[str, Any]) -> ClarifyResult:
        """Translate a parsed LLM payload into a validated ClarifyResult.

        Hard guardrails: if the model claims ``status=ready`` but the
        target payload is incomplete or invalid, downgrade to
        ``need_more`` with an explanatory reply so the UI keeps the
        conversation going instead of pre-filling garbage.
        """
        reply = str(parsed.get("reply") or "").strip()
        status = parsed.get("status")
        target = parsed.get("target")

        if status not in ("need_more", "ready"):
            status = "need_more"
        if target not in ("skill", "cron", None):
            target = None

        if status == "need_more" or target is None:
            return ClarifyResult(
                session_id=sid,
                reply=reply or "Please share a bit more detail.",
                status="need_more" if target is not None else "ready",
                target=target if target in ("skill", "cron") else None,
            )

        # status=ready, target in (skill, cron) — validate the payload
        try:
            if target == "skill":
                skill = SkillPrefill(**(parsed.get("skill") or {}))
                return ClarifyResult(
                    session_id=sid,
                    reply=reply or "Skill create form is ready.",
                    status="ready",
                    target="skill",
                    skill=skill,
                )
            # target == "cron"
            cron = normalize_cron_prefill(parsed.get("cron") or {})
            return ClarifyResult(
                session_id=sid,
                reply=reply or "Cron create form is ready.",
                status="ready",
                target="cron",
                cron=cron,
            )
        except (ValidationError, ValueError) as e:
            logger.info(f"intent clarifier guardrail downgrade: {e}")
            return ClarifyResult(
                session_id=sid,
                reply=(
                    f"{reply}\n(Still missing a few fields: {e}. Could you add them?)".strip()
                    if reply
                    else f"Still missing a few fields: {e}"
                ),
                status="need_more",
                target=target,  # type: ignore[arg-type]
            )
