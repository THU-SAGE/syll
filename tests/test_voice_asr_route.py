"""POST /api/v1/voice/asr: upload → provider → text.

Avoids a real FastAPI app-factory by mounting the voice router directly
onto a bare FastAPI app with a stub ``asr_provider`` on ``app.state``.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from syll.web.routes.voice import router as voice_router


def _make_app(provider):
    app = FastAPI()
    app.state.asr_provider = provider
    app.include_router(voice_router, prefix="/api/v1")
    return app


_WAV_BLOB = b"RIFF____WAVEfake" + b"\x00" * 2048  # >1KB to clear guard
_WEBM_BLOB = b"\x1a\x45\xdf\xa3webm-fake-header" + b"\x00" * 2048


@pytest.mark.anyio
async def test_wav_upload_skips_transcode():
    provider = AsyncMock()
    provider.transcribe = AsyncMock(return_value="hello world")

    app = _make_app(provider)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/voice/asr",
            files={"file": ("voice.wav", _WAV_BLOB, "audio/wav")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "hello world"
    assert data["format"] == "wav"
    provider.transcribe.assert_awaited_once()
    # Assert the provider saw the raw .wav path (no transcode).
    called_path = Path(provider.transcribe.await_args.args[0])
    assert called_path.suffix == ".wav"


@pytest.mark.anyio
async def test_webm_upload_triggers_transcode():
    provider = AsyncMock()
    provider.transcribe = AsyncMock(return_value="你好")

    async def fake_transcode(src: Path) -> Path:
        dest = src.with_suffix(".wav")
        dest.write_bytes(b"RIFF____WAVEfake")
        return dest

    app = _make_app(provider)
    with patch(
        "syll.web.routes.voice._transcode_to_wav",
        side_effect=fake_transcode,
    ) as mock_tx:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/voice/asr",
                files={"file": ("voice.webm", _WEBM_BLOB, "audio/webm")},
            )
    assert resp.status_code == 200
    assert resp.json()["text"] == "你好"
    assert resp.json()["format"] == "wav"
    mock_tx.assert_awaited_once()


@pytest.mark.anyio
async def test_tiny_upload_short_circuits_as_empty():
    """A <1KB blob is treated as an empty recording and returns early."""
    provider = AsyncMock()
    provider.transcribe = AsyncMock()
    app = _make_app(provider)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/voice/asr",
            files={"file": ("voice.webm", b"\x1a\x45\xdf\xa3" * 16, "audio/webm")},
        )
    assert resp.status_code == 200
    assert resp.json()["text"] == ""
    provider.transcribe.assert_not_called()


@pytest.mark.anyio
async def test_missing_provider_returns_503():
    app = _make_app(None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/voice/asr",
            files={"file": ("voice.wav", _WAV_BLOB, "audio/wav")},
        )
    assert resp.status_code == 503
