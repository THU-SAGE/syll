"""Tests for create_app() lifecycle and cron wiring."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from syll.cron.service import CronService
from syll.web.app import create_app


class _StubTools:
    class exec:
        max_timeout = 30
        default_timeout = 10
        restrict_to_workspace = False

    class web:
        class search:
            api_key = None

    class gui:
        pass

    restrict_to_workspace = False


def _make_config(tmp_dir):
    return SimpleNamespace(
        workspace_path=tmp_dir,
        tools=_StubTools(),
        models=SimpleNamespace(
            chat=SimpleNamespace(model="stub", api_key=None, api_base=None)
        ),
        gateway=SimpleNamespace(host="127.0.0.1", port=18790),
        agents=SimpleNamespace(defaults=SimpleNamespace(max_tool_iterations=5)),
        channels=SimpleNamespace(),
    )


def _make_agent_loop():
    return SimpleNamespace(
        sessions=SimpleNamespace(),
        context=SimpleNamespace(skills=SimpleNamespace(), memory=SimpleNamespace()),
    )


def test_create_app_starts_cron_via_lifespan():
    """TestClient enter/exit triggers lifespan events — cron should be running."""
    tmp = Path(tempfile.mkdtemp())
    cron = CronService(tmp / "cron.json")
    app = create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=cron,
    )

    assert cron._running is False

    with TestClient(app) as client:
        # After context entry, lifespan startup should have run
        assert cron._running is True
        # Basic health: can we hit a cheap endpoint?
        r = client.get("/api/v1/cron/jobs")
        assert r.status_code == 200

    # After context exit, lifespan shutdown should have run
    assert cron._running is False


def test_create_app_without_cron_service():
    """create_app should work fine when cron_service is None."""
    tmp = Path(tempfile.mkdtemp())
    app = create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )
    with TestClient(app) as client:
        # Cron routes should still be registered, but return 503 without service
        r = client.get("/api/v1/cron/jobs")
        assert r.status_code == 503


def test_create_app_idempotent_with_already_started_cron():
    """If cron is pre-started (e.g. gateway path), lifespan's start is a no-op."""
    tmp = Path(tempfile.mkdtemp())
    cron = CronService(tmp / "cron.json")

    async def _setup():
        await cron.start()

    asyncio.run(_setup())
    assert cron._running is True
    first_task = cron._timer_task

    app = create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=cron,
    )

    with TestClient(app):
        # Still running, no duplicate timer
        assert cron._running is True
        assert cron._timer_task is first_task


def test_create_app_wraps_on_job_after_assignment():
    """cron.on_job set before create_app is wrapped; wrapper is new object."""
    tmp = Path(tempfile.mkdtemp())
    cron = CronService(tmp / "cron.json")

    original_calls = []

    async def original(job):
        original_calls.append(job.id)
        return "ok"

    cron.on_job = original
    before_wrap = cron.on_job
    assert before_wrap is original

    create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=cron,
    )

    # After create_app, the callback is wrapped — not the original object
    assert cron.on_job is not original
    # on_complete installed
    assert cron.on_complete is not None
