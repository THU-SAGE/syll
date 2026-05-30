"""Chat API route — REST and WebSocket."""

import asyncio
import base64
import json
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from syll.web.streaming import process_streaming

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    content: str
    session_key: str = "web:default"


class ChatResponse(BaseModel):
    response: str
    session_key: str
    media: list[str] = []


# MIME → file extension for inbound uploads. Correct extensions are
# load-bearing: _encode_media and the frontend branch on the MIME guessed
# from the extension (<img> vs audio player), and the Audition tool's ffmpeg
# normalization keys off it too.
_MEDIA_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}


def _save_temp_media(data_b64: str, mime: str) -> str:
    """Decode a base64 upload (image or audio) to a temp file, return its path."""
    ext = _MEDIA_EXT.get((mime or "").lower())
    if ext is None:
        subtype = (mime or "").split("/")[-1].split(";")[0].strip()
        ext = f".{subtype}" if subtype.isalnum() else ".bin"

    media_dir = Path(tempfile.gettempdir()) / "syll_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    # uuid4, not id(): id() reuses freed addresses across requests, which could
    # collide and overwrite another request's still-referenced upload.
    path = media_dir / f"upload_{uuid.uuid4().hex}{ext}"
    path.write_bytes(base64.b64decode(data_b64))
    return str(path)


@router.post("/chat/message", response_model=ChatResponse)
async def chat_message(body: ChatRequest, request: Request):
    """Non-streaming chat endpoint."""
    agent_loop = request.app.state.agent_loop
    result = await agent_loop.process_direct(
        content=body.content,
        session_key=body.session_key,
        channel="web",
        chat_id=body.session_key.split(":", 1)[-1] if ":" in body.session_key else "default",
        language_hint_text=body.content,
        inject_skill_hints=True,
    )
    return ChatResponse(
        response=result.text,
        session_key=body.session_key,
        media=list(result.media),
    )


@router.websocket("/chat/ws")
async def chat_ws(websocket: WebSocket, session: str = "web:default"):
    """WebSocket streaming chat endpoint.

    Client sends: {"type": "message", "content": "...", "media": [{"mime": "image/png", "data": "base64..."}]}
    Server sends: {"type": "token"|"tool_call"|"tool_result"|"done"|"error"|"cron_triggered", ...}
    """
    # Auth: WS upgrades skip the AdminGuardMiddleware (they look like GETs),
    # so we apply the same loopback / token / origin checks inline here.
    from syll.web.auth import websocket_check_admin

    if not await websocket_check_admin(websocket):
        return  # close already sent
    await websocket.accept()

    # Register for server-originated broadcasts (e.g. cron_triggered)
    ws_clients = getattr(websocket.app.state, "ws_clients", None)
    if ws_clients is not None:
        ws_clients.add(websocket)

    agent_loop = websocket.app.state.agent_loop

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            if data.get("type") != "message" or not data.get("content"):
                await websocket.send_json({"type": "error", "content": "Expected {type: message, content: ...}"})
                continue

            content = data["content"]
            session_key = data.get("session_key", session)

            # Parse user-uploaded media (base64 inline)
            user_media = data.get("media", [])
            media_paths: list[str] = []
            for item in user_media:
                if isinstance(item, dict) and "data" in item:
                    mime = item.get("mime", "image/png")
                    path = _save_temp_media(item["data"], mime)
                    media_paths.append(path)

            try:
                websocket.app.state.agent_activity = "working"
                websocket.app.state.agent_activity_detail = ""
                async for event in process_streaming(
                    agent_loop,
                    content,
                    session_key,
                    media=media_paths if media_paths else None,
                ):
                    if event.get("type") == "tool_call":
                        websocket.app.state.agent_activity_detail = (
                            f"tool:{event.get('name', '')}"
                        )
                    await websocket.send_json(event)
                websocket.app.state.agent_activity = "idle"
                websocket.app.state.agent_activity_detail = ""
            except Exception as e:
                websocket.app.state.agent_activity = "error"
                websocket.app.state.agent_activity_detail = ""
                await websocket.send_json({"type": "error", "content": str(e)})

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        if ws_clients is not None:
            ws_clients.discard(websocket)
