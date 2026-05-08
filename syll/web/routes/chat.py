"""Chat API route — REST and WebSocket."""

import asyncio
import base64
import json
import tempfile
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


def _save_temp_image(data_b64: str, mime: str) -> str:
    """Decode base64 image and save to a temp file, return file path."""
    ext = ".png"
    if "jpeg" in mime or "jpg" in mime:
        ext = ".jpg"
    elif "gif" in mime:
        ext = ".gif"
    elif "webp" in mime:
        ext = ".webp"

    media_dir = Path(tempfile.gettempdir()) / "syll_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    path = media_dir / f"upload_{id(data_b64)}{ext}"
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
                    path = _save_temp_image(item["data"], mime)
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
        # Client closed the socket OR uvicorn cancelled the task during
        # shutdown; both should drain quietly without a traceback.
        pass
    finally:
        if ws_clients is not None:
            ws_clients.discard(websocket)
