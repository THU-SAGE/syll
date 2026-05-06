"""Speak tool: synthesize speech via a configured TTS provider."""

from typing import Any

from loguru import logger

from syll.agent.tools.base import Tool, ToolResult


class SpeakTool(Tool):
    """Synthesize spoken audio from text, returned as a ``ToolResult``.

    The audio travels through ``ToolResult.media`` to the web channel,
    where ``_encode_media`` base64-packs it and the frontend renders an
    ``<audio>`` element. A single ``speak`` call per turn gives the
    best prosody — don't split long text across multiple calls.
    """

    name = "speak"
    description = (
        "Synthesize natural spoken audio from text and return it as an "
        "audio attachment. Use this when the user asks for a voice "
        "reply, a podcast-style briefing, or any content that should "
        "be heard. Prefer one call with the full script — multiple "
        "short calls produce choppier prosody. Supports Chinese and "
        "English out of the box."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to synthesize. No markdown or URLs.",
            },
            "voice": {
                "type": "string",
                "description": (
                    "Optional speaker id (e.g. "
                    "'zh_female_shuangkuaisisi_moon_bigtts'). "
                    "Falls back to the configured default."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["mp3", "wav"],
                "description": "Audio container. Defaults to mp3.",
            },
        },
        "required": ["text"],
    }

    def __init__(self, tts_provider: Any):
        self.tts = tts_provider

    async def execute(
        self,
        text: str,
        voice: str | None = None,
        format: str = "mp3",
        **_: Any,
    ) -> str | ToolResult:
        if not text or not text.strip():
            return "Error: text is empty"
        try:
            path = await self.tts.synthesize(text, voice=voice, fmt=format)
        except Exception as e:
            # str(e) is empty for bare ConnectionResetError / ConnectionClosedError,
            # which is exactly what Bytedance's WS does when auth or resource_id is
            # wrong. Always include the exception class name so the operator can
            # tell "empty" apart from "no error at all".
            etype = type(e).__name__
            detail = str(e) or repr(e)
            logger.exception("SpeakTool synthesis failed")
            return f"Error: TTS synthesis failed ({etype}): {detail}"
        return ToolResult(
            text=f"Synthesized {len(text)} chars → {path.name}",
            media=[str(path)],
        )
