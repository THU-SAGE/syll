"""SpeakTool returns a ToolResult with the synthesized audio file in media."""

import asyncio
from unittest.mock import AsyncMock

from syll.agent.tools.base import ToolResult
from syll.agent.tools.speak import SpeakTool
from syll.web.streaming import _encode_media


def test_speak_tool_execute_returns_media(tmp_path):
    out = tmp_path / "clip.mp3"
    out.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00")

    provider = AsyncMock()
    provider.synthesize = AsyncMock(return_value=out)

    tool = SpeakTool(provider)
    result = asyncio.run(tool.execute(text="你好世界"))

    assert isinstance(result, ToolResult)
    assert result.media == [str(out)]
    provider.synthesize.assert_awaited_once()

    # And when flowed through the WS encoder, the MIME must be audio/mpeg —
    # not the pre-fix image/png default.
    encoded = _encode_media(result.media)
    assert encoded[0]["mime"] == "audio/mpeg"


def test_speak_tool_empty_text_short_circuits():
    provider = AsyncMock()
    tool = SpeakTool(provider)
    out = asyncio.run(tool.execute(text="   "))
    assert isinstance(out, str)
    assert "empty" in out.lower()
    provider.synthesize.assert_not_awaited()


def test_speak_tool_provider_error_returns_string():
    provider = AsyncMock()
    provider.synthesize = AsyncMock(side_effect=RuntimeError("boom"))
    tool = SpeakTool(provider)
    out = asyncio.run(tool.execute(text="hello"))
    assert isinstance(out, str)
    assert "TTS" in out
