"""Voice API route — ASR upload → transcription text.

Consumed by the frontend's "right-click ghost = push-to-talk" flow in
``index.html``. The browser posts a ``MediaRecorder`` blob (ogg/opus
when supported, webm otherwise) and receives the transcript, which it
then re-submits through the normal chat WebSocket.

Volcengine ASR only accepts pcm / wav / mp3 / ogg_opus, so anything
outside that whitelist is transcoded through ffmpeg before being sent
upstream.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, UploadFile
from loguru import logger

router = APIRouter(prefix="/voice", tags=["voice"])


_VOLC_OK_EXT = {".wav", ".pcm", ".mp3", ".ogg", ".opus"}
_VOLC_OK_MIME_PREFIXES = (
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/pcm",
)


async def _transcode_to_wav(src: Path) -> Path:
    """Convert ``src`` (any container ffmpeg can decode) to 16k mono wav.

    Returns the path to the new wav file. Raises ``HTTPException(501)``
    when ffmpeg is not installed so the frontend can surface a useful
    message instead of a generic 500.
    """
    if shutil.which("ffmpeg") is None:
        raise HTTPException(
            501,
            "ffmpeg not installed on server — cannot transcode "
            f"{src.suffix or 'audio'} for Volcengine ASR. Record as "
            "ogg/opus in the browser or install ffmpeg.",
        )
    dest = src.with_suffix(".wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-ac", "1",
        "-ar", "16000",
        "-f", "wav",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Keep the TAIL of stderr — ffmpeg prints the real error last.
        err = stderr.decode(errors="replace").strip().splitlines()
        tail = " | ".join(err[-6:]) if err else "(no stderr)"
        src_size = src.stat().st_size if src.is_file() else -1
        logger.error(
            f"ffmpeg transcode failed (rc={proc.returncode}, src={src.name}, "
            f"{src_size}B): {tail}"
        )
        raise HTTPException(500, f"ffmpeg transcode failed: {tail[:200]}")
    logger.info(f"transcoded {src.suffix or 'audio'}→wav: {src.name}")
    return dest


@router.post("/asr")
async def transcribe_upload(file: UploadFile, request: Request):
    """Transcribe a single audio upload and return the text."""
    provider = getattr(request.app.state, "asr_provider", None)
    if provider is None:
        raise HTTPException(503, "ASR provider not configured")

    media_dir = Path(tempfile.gettempdir()) / "syll_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    original_name = file.filename or "voice.bin"
    raw = media_dir / f"{uuid4().hex}_{Path(original_name).name}"
    payload = await file.read()
    raw.write_bytes(payload)
    # A MediaRecorder blob that never actually fired ondataavailable is
    # just the container preamble (~200B for webm, a few hundred for
    # ogg). Anything under ~1KB is almost certainly a no-op release.
    if len(payload) < 1024:
        logger.info(
            f"ASR upload too small ({len(payload)}B) — treating as empty recording"
        )
        return {"text": "", "format": raw.suffix.lstrip(".")}

    ext = raw.suffix.lower()
    ctype = (file.content_type or "").lower()
    needs_transcode = ext not in _VOLC_OK_EXT and not any(
        ctype.startswith(m) for m in _VOLC_OK_MIME_PREFIXES
    )
    audio_path = await _transcode_to_wav(raw) if needs_transcode else raw

    try:
        text = await provider.transcribe(audio_path)
    except Exception as e:
        logger.error(f"ASR provider failed: {e}")
        raise HTTPException(500, f"ASR failed: {e}")

    return {
        "text": text or "",
        "format": audio_path.suffix.lstrip("."),
    }
