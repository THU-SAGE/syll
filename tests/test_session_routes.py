"""Tests for public session API message redaction."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

from syll.web.routes.sessions import get_session


def test_get_session_redacts_reasoning_content_from_public_messages():
    """DeepSeek replay metadata should not leak through the sessions API."""
    session = SimpleNamespace(
        key="cli:dashboard",
        created_at=datetime(2026, 5, 5, 1, 2, 3),
        updated_at=datetime(2026, 5, 5, 1, 2, 4),
        messages=[
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "private hidden reasoning",
                "tool_calls": [
                    {
                        "id": "call_weather",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    }
                ],
            }
        ],
    )
    session_manager = SimpleNamespace(get_or_create=lambda key: session)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_manager=session_manager)))

    response = asyncio.run(get_session("cli:dashboard", request))

    assert response["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {"name": "weather", "arguments": "{}"},
                }
            ],
        }
    ]
