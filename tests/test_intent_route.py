"""Route-level tests for POST /api/v1/intent/clarify."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from syll.agent.intent_clarifier import ClarifyResult, CronPrefill
from syll.web.app import create_app
from tests.test_app_factory import _admin_headers, _make_agent_loop, _make_config


class _StubClarifier:
    def __init__(self, provider, result):
        self.provider = provider
        self._result = result
        self.calls: list[tuple[str | None, str]] = []

    async def clarify(self, session_id, text):
        self.calls.append((session_id, text))
        return self._result


def _make_app(clarifier):
    tmp = Path(tempfile.mkdtemp())
    app = create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )
    app.state.intent_clarifier = clarifier
    return app


def test_clarify_503_without_provider():
    # Default agent_loop stub has no .provider, so the real IntentClarifier
    # is installed with provider=None — route should 503.
    tmp = Path(tempfile.mkdtemp())
    app = create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/intent/clarify", json={"text": "hi"})
        assert r.status_code == 503


def test_clarify_422_on_empty_text():
    result = ClarifyResult(session_id="s", reply="ok", status="ready", target=None)
    clarifier = _StubClarifier(provider=SimpleNamespace(), result=result)
    app = _make_app(clarifier)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/intent/clarify", json={"text": "   "})
        assert r.status_code == 422


def test_clarify_200_happy_path():
    result = ClarifyResult(
        session_id="sess-1",
        reply="ok",
        status="ready",
        target="cron",
        cron=CronPrefill(
            name="drink-water",
            message="提醒喝水",
            schedule_mode="daily",
            daily_time="08:00",
            daily_days="every",
        ),
    )
    clarifier = _StubClarifier(provider=SimpleNamespace(), result=result)
    app = _make_app(clarifier)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post(
            "/api/v1/intent/clarify",
            json={"session_id": None, "text": "每天八点提醒喝水"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["target"] == "cron"
        assert data["cron"]["daily_time"] == "08:00"
        assert clarifier.calls == [(None, "每天八点提醒喝水")]


def test_clarify_500_on_clarifier_exception():
    class _Boom:
        provider = SimpleNamespace()

        async def clarify(self, session_id, text):
            raise RuntimeError("upstream dead")

    app = _make_app(_Boom())
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/intent/clarify", json={"text": "hi"})
        assert r.status_code == 500
