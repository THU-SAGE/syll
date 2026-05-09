"""Tests for Aloha planner retry behavior."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from syll.agent.aloha.act.planner import AlohaPlanner


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_planner_retries_after_provider_error():
    """Transient provider errors should be retried before failing the step."""
    calls = {"count": 0}

    async def fake_acompletion(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("OpenrouterException - Unable to get json response")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"Observation":"desktop visible",'
                            '"Reasoning":"step one",'
                            '"Current Step in Guidance Trajectory":"(1, \'start\')",'
                            '"Action":"Double-click the Stardew Valley icon",'
                            '"Expectation":"Game launches"}'
                        )
                    )
                )
            ]
        )

    planner = AlohaPlanner(
        model="openrouter/anthropic/claude-sonnet-4-20250514",
        retry_attempts=1,
        retry_delay_seconds=0,
    )

    with patch.dict(sys.modules, {"litellm": SimpleNamespace(acompletion=fake_acompletion)}), patch(
        "syll.agent.aloha.act.planner.asyncio.sleep", new=AsyncMock()
    ):
        result = await planner.plan(task="Launch Stardew Valley")

    assert calls["count"] == 2
    assert result["Action"] == "Double-click the Stardew Valley icon"
    assert result["Current Step"] == 1
