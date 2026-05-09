"""Tests for quiet WebSocket shutdown handling."""

import asyncio
from types import SimpleNamespace

from syll.web.routes.chat import chat_ws


class _FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent = []
        # Phase 1a: chat_ws now runs websocket_check_admin first; provide a
        # loopback client so the auth check passes before reaching accept().
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers = {}
        self.query_params = {}
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                ws_clients=set(),
                agent_loop=SimpleNamespace(),
                config=SimpleNamespace(
                    gateway=SimpleNamespace(
                        host="127.0.0.1",
                        port=18790,
                        allow_remote_admin=False,
                        allow_origins=[],
                    )
                ),
            )
        )

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        raise asyncio.CancelledError()

    async def send_json(self, event):
        self.sent.append(event)

    async def close(self, code=1000, reason=""):
        pass


def test_chat_ws_treats_shutdown_cancellation_as_disconnect():
    """ASGI task cancellation during shutdown should not be logged as an error."""
    websocket = _FakeWebSocket()

    asyncio.run(chat_ws(websocket))

    assert websocket.accepted is True
    assert websocket not in websocket.app.state.ws_clients
    assert websocket.sent == []
