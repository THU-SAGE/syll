"""Unit tests for IntentClarifier and normalize_cron_prefill."""

from __future__ import annotations

import json

import pytest

from syll.agent.intent_clarifier import (
    ClarifyResult,
    CronPrefill,
    IntentClarifier,
    SkillPrefill,
    _extract_json,
    normalize_cron_prefill,
)

# ───────────────────────── normalize_cron_prefill ──────────────────────


def test_normalize_daily_basic():
    p = normalize_cron_prefill({
        "name": "drink-water",
        "message": "提醒我喝水",
        "schedule_mode": "daily",
        "daily_time": "08:00",
    })
    assert p.schedule_mode == "daily"
    assert p.daily_time == "08:00"
    assert p.daily_days == "every"
    assert p.interval_value is None
    assert p.cron_expr is None


def test_normalize_daily_accepts_chinese_time():
    p = normalize_cron_prefill({
        "name": "x",
        "message": "y",
        "schedule_mode": "daily",
        "daily_time": "8点30分",
    })
    assert p.daily_time == "08:30"


def test_normalize_daily_trims_seconds():
    p = normalize_cron_prefill({
        "name": "x",
        "message": "y",
        "schedule_mode": "daily",
        "daily_time": "08:00:00",
    })
    assert p.daily_time == "08:00"


def test_normalize_daily_custom_weekday_words():
    p = normalize_cron_prefill({
        "name": "x",
        "message": "y",
        "schedule_mode": "daily",
        "daily_time": "09:00",
        "daily_days": "custom",
        "daily_custom_days": ["mon", "Wednesday", "周五", 5],  # dedup the 5
    })
    assert p.daily_days == "custom"
    assert p.daily_custom_days == [1, 3, 5]


def test_normalize_daily_missing_time_raises():
    with pytest.raises(ValueError):
        normalize_cron_prefill({
            "name": "x",
            "message": "y",
            "schedule_mode": "daily",
        })


def test_normalize_daily_custom_missing_days_raises():
    with pytest.raises(ValueError):
        normalize_cron_prefill({
            "name": "x",
            "message": "y",
            "schedule_mode": "daily",
            "daily_time": "09:00",
            "daily_days": "custom",
            "daily_custom_days": [],
        })


def test_normalize_interval_ok():
    p = normalize_cron_prefill({
        "name": "x",
        "message": "y",
        "schedule_mode": "interval",
        "interval_value": 30,
        "interval_unit": "minutes",
    })
    assert p.interval_value == 30
    assert p.interval_unit == "minute"


def test_normalize_interval_chinese_unit():
    p = normalize_cron_prefill({
        "name": "x",
        "message": "y",
        "schedule_mode": "interval",
        "interval_value": 2,
        "interval_unit": "小时",
    })
    assert p.interval_unit == "hour"


def test_normalize_interval_rejects_zero():
    with pytest.raises(ValueError):
        normalize_cron_prefill({
            "name": "x",
            "message": "y",
            "schedule_mode": "interval",
            "interval_value": 0,
            "interval_unit": "minute",
        })


def test_normalize_once_ok_space_separator():
    p = normalize_cron_prefill({
        "name": "x",
        "message": "y",
        "schedule_mode": "once",
        "at_local": "2026-04-14 09:30",
    })
    assert p.at_local == "2026-04-14T09:30"


def test_normalize_once_rejects_bad_format():
    with pytest.raises(ValueError):
        normalize_cron_prefill({
            "name": "x",
            "message": "y",
            "schedule_mode": "once",
            "at_local": "tomorrow morning",
        })


def test_normalize_advanced_ok():
    p = normalize_cron_prefill({
        "name": "x",
        "message": "y",
        "schedule_mode": "advanced",
        "cron_expr": "*/5 * * * *",
    })
    assert p.cron_expr == "*/5 * * * *"


def test_normalize_advanced_rejects_bad_cron():
    with pytest.raises(ValueError):
        normalize_cron_prefill({
            "name": "x",
            "message": "y",
            "schedule_mode": "advanced",
            "cron_expr": "every five minutes",
        })


def test_normalize_unknown_mode_raises():
    with pytest.raises(ValueError):
        normalize_cron_prefill({
            "name": "x",
            "message": "y",
            "schedule_mode": "weekly",
        })


# ───────────────────────── _extract_json ──────────────────────────


def test_extract_json_strict():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_prose():
    raw = 'Sure! Here is the result:\n{"status": "ready", "target": null}\nhope that helps'
    assert _extract_json(raw) == {"status": "ready", "target": None}


def test_extract_json_with_fence():
    raw = '```json\n{"a": 1, "b": "}"}\n```'
    assert _extract_json(raw) == {"a": 1, "b": "}"}


def test_extract_json_empty_raises():
    with pytest.raises(ValueError):
        _extract_json("")


# ───────────────────────── IntentClarifier with fake provider ─────────


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeProvider:
    """Scripted provider — pops replies off a queue in order."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.last_messages: list[dict] = []

    async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        self.last_messages = list(messages)
        if not self._replies:
            raise RuntimeError("fake provider exhausted")
        return _FakeResponse(self._replies.pop(0))


@pytest.mark.asyncio
async def test_clarify_rejects_empty_text():
    clarifier = IntentClarifier(_FakeProvider([]))
    with pytest.raises(ValueError):
        await clarifier.clarify(None, "   ")


@pytest.mark.asyncio
async def test_clarify_no_provider_raises():
    clarifier = IntentClarifier(None)
    with pytest.raises(RuntimeError):
        await clarifier.clarify(None, "hi")


@pytest.mark.asyncio
async def test_clarify_two_turn_need_more_then_ready():
    turn1 = json.dumps({
        "reply": "几点提醒你？",
        "status": "need_more",
        "target": "cron",
    })
    turn2 = json.dumps({
        "reply": "好的，已准备好定时任务。",
        "status": "ready",
        "target": "cron",
        "cron": {
            "name": "drink-water",
            "action_type": "message",
            "message": "提醒我喝水",
            "schedule_mode": "daily",
            "daily_time": "08:00",
        },
    })
    provider = _FakeProvider([turn1, turn2])
    clarifier = IntentClarifier(provider)

    r1 = await clarifier.clarify(None, "帮我创建一个提醒喝水的定时任务")
    assert r1.status == "need_more"
    assert r1.target == "cron"
    sid = r1.session_id
    assert sid in clarifier._sessions
    # system + user + assistant
    assert len(clarifier._sessions[sid]) == 3

    r2 = await clarifier.clarify(sid, "早上八点")
    assert r2.status == "ready"
    assert r2.target == "cron"
    assert r2.cron is not None
    assert r2.cron.daily_time == "08:00"
    # Ready drops the session
    assert sid not in clarifier._sessions


@pytest.mark.asyncio
async def test_clarify_guardrail_downgrades_when_fields_missing():
    raw = json.dumps({
        "reply": "已准备好。",
        "status": "ready",
        "target": "cron",
        "cron": {
            "name": "x",
            "action_type": "message",
            "message": "y",
            "schedule_mode": "daily",
            # daily_time missing — should downgrade to need_more
        },
    })
    clarifier = IntentClarifier(_FakeProvider([raw]))
    result = await clarifier.clarify(None, "create cron")
    assert result.status == "need_more"
    assert result.target == "cron"
    assert result.cron is None


@pytest.mark.asyncio
async def test_clarify_ready_skill():
    raw = json.dumps({
        "reply": "已准备好 skill",
        "status": "ready",
        "target": "skill",
        "skill": {
            "name": "paper-digest",
            "description": "帮我整理论文",
            "template": "blank",
        },
    })
    clarifier = IntentClarifier(_FakeProvider([raw]))
    result = await clarifier.clarify(None, "create skill paper-digest")
    assert result.status == "ready"
    assert result.target == "skill"
    assert isinstance(result.skill, SkillPrefill)
    assert result.skill.name == "paper-digest"


@pytest.mark.asyncio
async def test_clarify_unrecognized_target_is_graceful():
    raw = json.dumps({
        "reply": "抱歉，我只能创建 skill 或定时任务。",
        "status": "ready",
        "target": None,
    })
    clarifier = IntentClarifier(_FakeProvider([raw]))
    result = await clarifier.clarify(None, "order me a pizza")
    assert result.target is None


@pytest.mark.asyncio
async def test_clarify_bad_json_graceful_downgrade():
    clarifier = IntentClarifier(_FakeProvider(["this is not JSON"]))
    result = await clarifier.clarify(None, "create something")
    assert result.status == "need_more"
    assert result.target is None


def test_models_roundtrip():
    # Sanity check: ClarifyResult serialises cleanly.
    cr = ClarifyResult(
        session_id="abc",
        reply="hi",
        status="ready",
        target="cron",
        cron=CronPrefill(
            name="x",
            message="y",
            schedule_mode="daily",
            daily_time="09:00",
            daily_days="every",
        ),
    )
    dumped = cr.model_dump()
    assert dumped["cron"]["daily_time"] == "09:00"
