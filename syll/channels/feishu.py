"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import shutil
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from syll.bus.events import OutboundMessage
from syll.bus.queue import MessageBus
from syll.channels.base import BaseChannel
from syll.config.schema import FeishuConfig

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        Emoji,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
    )
    from lark_oapi.api.im.v1 import CreateImageRequest as CreateImImageRequest
    from lark_oapi.api.im.v1 import CreateImageRequestBody as CreateImImageRequestBody

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}

# Extensions treated as images (sent via image API); everything else uses file API
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# Extensions we try to deliver as native Feishu audio bubbles (msg_type="audio").
# Feishu only accepts opus-ogg, so anything else is transcoded via ffmpeg first.
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}

# Feishu file_type mapping (https://open.feishu.cn/document/server-docs/im-v1/file/create)
_FEISHU_FILE_TYPE = {
    ".opus": "opus", ".mp4": "mp4", ".pdf": "pdf",
    ".doc": "doc", ".docx": "doc",
    ".xls": "xls", ".xlsx": "xls",
    ".ppt": "ppt", ".pptx": "ppt",
}


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"

    def __init__(self, config: FeishuConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        # Create event handler (only register message receive, ignore other events)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )

        # Start WebSocket client in a separate thread
        def run_ws():
            try:
                self._ws_client.start()
            except Exception as e:
                logger.error(f"Feishu WebSocket error: {e}")

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu (text + images + files)."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            # Determine receive_id_type based on chat_id format
            # open_id starts with "ou_", chat_id starts with "oc_"
            if msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"

            # Send media (images via image API, other files via file API)
            for path in msg.media or []:
                p = Path(path)
                if not p.is_file():
                    logger.warning(f"Media file not found: {path}")
                    continue

                if p.suffix.lower() in _IMAGE_EXTS:
                    await self._send_image(path, receive_id_type, msg.chat_id)
                elif p.suffix.lower() in _AUDIO_EXTS:
                    await self._send_audio(path, receive_id_type, msg.chat_id)
                else:
                    await self._send_file(path, receive_id_type, msg.chat_id)

            # Send text message
            if msg.content:
                content = json.dumps({"text": msg.content})

                request = CreateMessageRequest.builder() \
                    .receive_id_type(receive_id_type) \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(msg.chat_id)
                        .msg_type("text")
                        .content(content)
                        .build()
                    ).build()

                response = self._client.im.v1.message.create(request)

                if not response.success():
                    logger.error(
                        f"Failed to send Feishu message: code={response.code}, "
                        f"msg={response.msg}, log_id={response.get_log_id()}"
                    )
                else:
                    logger.debug(f"Feishu message sent to {msg.chat_id}")

        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")

    def _upload_image_sync(self, path: str) -> str | None:
        """Sync helper to upload image to Feishu and return image_key."""
        try:
            body = CreateImImageRequestBody.builder() \
                .image_type("message") \
                .image(open(path, "rb")) \
                .build()
            req = CreateImImageRequest.builder().request_body(body).build()
            resp = self._client.im.v1.image.create(req)
            if resp.success():
                return resp.data.image_key
            logger.error(f"Feishu image upload failed: code={resp.code}, msg={resp.msg}")
            return None
        except Exception as e:
            logger.error(f"Feishu image upload error: {e}")
            return None

    async def _upload_image(self, path: str) -> str | None:
        """Upload image to Feishu (non-blocking)."""
        if not self._client:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._upload_image_sync, path)

    def _upload_file_sync(self, path: str) -> str | None:
        """Sync helper to upload a file to Feishu and return file_key."""
        try:
            p = Path(path)
            file_type = _FEISHU_FILE_TYPE.get(p.suffix.lower(), "stream")
            body = CreateFileRequestBody.builder() \
                .file_type(file_type) \
                .file_name(p.name) \
                .file(open(path, "rb")) \
                .build()
            req = CreateFileRequest.builder().request_body(body).build()
            resp = self._client.im.v1.file.create(req)
            if resp.success():
                return resp.data.file_key
            logger.error(f"Feishu file upload failed: code={resp.code}, msg={resp.msg}")
            return None
        except Exception as e:
            logger.error(f"Feishu file upload error: {e}")
            return None

    async def _upload_file(self, path: str) -> str | None:
        """Upload file to Feishu (non-blocking)."""
        if not self._client:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._upload_file_sync, path)

    async def _send_image(
        self, path: str, receive_id_type: str, chat_id: str
    ) -> None:
        """Upload and send an image message."""
        image_key = await self._upload_image(path)
        if not image_key:
            return
        content = json.dumps({"image_key": image_key})
        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("image")
                .content(content)
                .build()
            ).build()
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                f"Failed to send Feishu image: code={response.code}, msg={response.msg}"
            )
        else:
            logger.debug(f"Feishu image sent to {chat_id}")

    async def _send_file(
        self, path: str, receive_id_type: str, chat_id: str
    ) -> None:
        """Upload and send a file message."""
        file_key = await self._upload_file(path)
        if not file_key:
            return
        file_name = Path(path).name
        content = json.dumps({"file_key": file_key, "file_name": file_name})
        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(content)
                .build()
            ).build()
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                f"Failed to send Feishu file: code={response.code}, msg={response.msg}"
            )
        else:
            logger.debug(f"Feishu file '{file_name}' sent to {chat_id}")

    async def _send_audio(
        self, path: str, receive_id_type: str, chat_id: str
    ) -> None:
        """Upload and send a native Feishu voice-bubble message.

        Feishu's ``msg_type="audio"`` requires an opus-ogg upload. Anything
        else (mp3, wav, m4a, ...) is transcoded via ffmpeg first. If ffmpeg
        is missing or transcoding fails, we fall back to the plain file
        path so the audio at least reaches the user as an attachment.
        """
        src = Path(path)
        to_upload: Path = src
        cleanup: Path | None = None

        if src.suffix.lower() != ".opus":
            transcoded = await self._transcode_to_opus(src)
            if transcoded is None:
                logger.warning(
                    f"Feishu audio: cannot transcode {src.name} to opus — "
                    "sending as plain file attachment instead"
                )
                await self._send_file(path, receive_id_type, chat_id)
                return
            to_upload = transcoded
            cleanup = transcoded

        try:
            file_key = await self._upload_file(str(to_upload))
            if not file_key:
                return
            content = json.dumps({"file_key": file_key})
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("audio")
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    f"Failed to send Feishu audio: code={response.code}, "
                    f"msg={response.msg} — falling back to file attachment"
                )
                # Fallback: deliver as file so audio still reaches the user.
                await self._send_file(path, receive_id_type, chat_id)
            else:
                logger.debug(f"Feishu audio sent to {chat_id}")
        finally:
            if cleanup and cleanup.is_file():
                try:
                    cleanup.unlink()
                except Exception:
                    pass

    async def _transcode_to_opus(self, src: Path) -> Path | None:
        """Transcode an audio file to opus-ogg for Feishu. Returns path or None."""
        if shutil.which("ffmpeg") is None:
            logger.warning("ffmpeg not installed — cannot transcode audio for Feishu")
            return None
        dest = Path(tempfile.gettempdir()) / f"feishu_{src.stem}_{uuid4().hex}.opus"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-c:a", "libopus",
                "-b:a", "32k",
                "-ac", "1",
                "-ar", "48000",
                "-f", "ogg",
                str(dest),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                tail = stderr.decode(errors="replace").strip().splitlines()[-4:]
                logger.error(
                    f"ffmpeg opus transcode failed (rc={proc.returncode}, "
                    f"src={src.name}): {' | '.join(tail)[:240]}"
                )
                if dest.is_file():
                    try:
                        dest.unlink()
                    except Exception:
                        pass
                return None
            return dest
        except Exception as e:
            logger.error(f"ffmpeg opus transcode error: {e}")
            return None

    def _download_image_sync(self, message_id: str, image_key: str) -> str | None:
        """Sync helper to download image from Feishu and save to temp file."""
        try:
            req = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            resp = self._client.im.v1.message_resource.get(req)
            if resp.success():
                media_dir = Path(tempfile.gettempdir()) / "syll_media"
                media_dir.mkdir(parents=True, exist_ok=True)
                path = media_dir / f"feishu_{image_key}.png"
                path.write_bytes(resp.file.read())
                return str(path)
            logger.error(f"Feishu image download failed: code={resp.code}, msg={resp.msg}")
            return None
        except Exception as e:
            logger.error(f"Feishu image download error: {e}")
            return None

    async def _download_image(self, message_id: str, image_key: str) -> str | None:
        """Download image from Feishu (non-blocking)."""
        if not self._client:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_image_sync, message_id, image_key)

    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache: keep most recent 500 when exceeds 1000
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            sender_type = sender.sender_type
            if sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type

            # Add reaction to indicate "seen"
            await self._add_reaction(message_id, "THUMBSUP")

            # Parse message content
            media_paths: list[str] = []
            if msg_type == "text":
                try:
                    content = json.loads(message.content).get("text", "")
                except json.JSONDecodeError:
                    content = message.content or ""
            elif msg_type == "image":
                try:
                    image_key = json.loads(message.content).get("image_key")
                    if image_key:
                        path = await self._download_image(message_id, image_key)
                        if path:
                            media_paths.append(path)
                except (json.JSONDecodeError, KeyError):
                    pass
                content = "[User sent an image]"
            else:
                content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

            if not content:
                return

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths if media_paths else None,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                }
            )

        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")
