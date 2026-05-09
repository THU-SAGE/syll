"""Unit tests for cron broadcast wiring.

We avoid real WebSocket clients (TestClient's WS + concurrent HTTP deadlocks)
and instead test the broadcast mechanism directly by attaching fake WS
objects to app.state.ws_clients and asserting the wrapped on_job + on_complete
hooks fire the expected events.
"""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from syll.cron.service import CronService
from syll.cron.types import CronJob, CronPayload, CronSchedule
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


class _FakeWs:
    """A stand-in WebSocket that just records sent JSON events."""

    def __init__(self):
        self.events = []

    async def send_json(self, event):
        self.events.append(event)


def _build_app_with_cron():
    tmp = Path(tempfile.mkdtemp())
    cron = CronService(tmp / "cron.json")
    config = SimpleNamespace(
        workspace_path=tmp,
        tools=_StubTools(),
        models=SimpleNamespace(
            chat=SimpleNamespace(model="stub", api_key=None, api_base=None)
        ),
        gateway=SimpleNamespace(host="127.0.0.1", port=18790),
        agents=SimpleNamespace(defaults=SimpleNamespace(max_tool_iterations=5)),
        channels=SimpleNamespace(),
    )
    agent_loop = SimpleNamespace(
        sessions=SimpleNamespace(),
        context=SimpleNamespace(skills=SimpleNamespace(), memory=SimpleNamespace()),
    )
    app = create_app(
        config=config,
        agent_loop=agent_loop,
        session_manager=agent_loop.sessions,
        skills_loader=agent_loop.context.skills,
        memory_store=agent_loop.context.memory,
        cron_service=cron,
    )
    return app, cron


def _make_job(job_id="j1", schedule=None, enabled=True):
    return CronJob(
        id=job_id,
        name="test-job",
        enabled=enabled,
        schedule=schedule or CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(message="test"),
        created_at_ms=0,
        updated_at_ms=0,
    )


def test_create_app_wraps_on_job():
    """create_app installs a wrapper and on_complete hook."""
    app, cron = _build_app_with_cron()
    assert cron.on_job is not None
    assert cron.on_complete is not None
    # The wrapper should be a coroutine function
    assert asyncio.iscoroutinefunction(cron.on_job)
    assert asyncio.iscoroutinefunction(cron.on_complete)


def test_broadcast_ws_sends_to_all_clients():
    """broadcast_ws hits every registered client and prunes dead ones."""
    app, _ = _build_app_with_cron()

    ws1 = _FakeWs()
    ws2 = _FakeWs()
    app.state.ws_clients.add(ws1)
    app.state.ws_clients.add(ws2)

    asyncio.run(app.state.broadcast_ws({"type": "test", "x": 1}))

    assert ws1.events == [{"type": "test", "x": 1}]
    assert ws2.events == [{"type": "test", "x": 1}]


def test_broadcast_ws_prunes_dead_clients():
    """Clients whose send_json raises are removed from the set."""
    app, _ = _build_app_with_cron()

    class _Dead:
        async def send_json(self, event):
            raise RuntimeError("connection closed")

    alive = _FakeWs()
    dead = _Dead()
    app.state.ws_clients.add(alive)
    app.state.ws_clients.add(dead)

    asyncio.run(app.state.broadcast_ws({"type": "test"}))

    assert alive.events == [{"type": "test"}]
    assert dead not in app.state.ws_clients
    assert alive in app.state.ws_clients


def test_wrapped_on_job_broadcasts_running():
    """The wrapped on_job broadcasts a running event before calling the original."""
    app, cron = _build_app_with_cron()
    ws = _FakeWs()
    app.state.ws_clients.add(ws)

    job = _make_job(job_id="abc")

    asyncio.run(cron.on_job(job))

    running = [e for e in ws.events if e.get("type") == "cron_triggered"]
    assert len(running) == 1
    assert running[0]["status"] == "running"
    assert running[0]["job_id"] == "abc"
    assert running[0]["job_name"] == "test-job"


def test_on_complete_broadcasts_ok_status():
    """on_complete fires after _save_store and broadcasts ok status."""
    app, cron = _build_app_with_cron()
    ws = _FakeWs()
    app.state.ws_clients.add(ws)

    job = _make_job(job_id="xyz")
    job.state.last_status = "ok"
    job.state.last_run_at_ms = 1_700_000_000_000
    job.state.next_run_at_ms = 1_700_000_060_000

    asyncio.run(cron.on_complete(job))

    done = [e for e in ws.events if e.get("type") == "cron_triggered"]
    assert len(done) == 1
    assert done[0]["status"] == "ok"
    assert done[0]["job_id"] == "xyz"
    assert done[0]["last_run_at_ms"] == 1_700_000_000_000
    assert done[0]["next_run_at_ms"] == 1_700_000_060_000
    # Always include a media key so the frontend can safely check length.
    assert done[0]["media"] == []


def test_on_complete_broadcasts_media_when_last_media_set(tmp_path):
    """on_complete embeds base64-encoded audio when last_media is populated."""
    # A minimal valid-enough .mp3 file so mimetypes resolves it to audio/mpeg.
    mp3 = tmp_path / "clip.mp3"
    mp3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-bytes")

    app, cron = _build_app_with_cron()
    ws = _FakeWs()
    app.state.ws_clients.add(ws)

    job = _make_job(job_id="podcast")
    job.state.last_status = "ok"
    job.state.last_run_at_ms = 1_700_000_000_000
    job.state.next_run_at_ms = 1_700_000_060_000
    job.state.last_media = [str(mp3)]

    asyncio.run(cron.on_complete(job))

    ok_event = next(
        e for e in ws.events if e.get("type") == "cron_triggered"
    )
    assert ok_event["status"] == "ok"
    assert isinstance(ok_event["media"], list)
    assert len(ok_event["media"]) == 1
    assert ok_event["media"][0]["mime"] == "audio/mpeg"
    assert ok_event["media"][0]["data"]  # non-empty base64


def test_on_complete_broadcasts_error_status():
    """Error status + error message are propagated."""
    app, cron = _build_app_with_cron()
    ws = _FakeWs()
    app.state.ws_clients.add(ws)

    job = _make_job(job_id="fail")
    job.state.last_status = "error"
    job.state.last_error = "something broke"
    job.state.last_run_at_ms = 1_700_000_000_000

    asyncio.run(cron.on_complete(job))

    events = [e for e in ws.events if e.get("type") == "cron_triggered"]
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["error"] == "something broke"


def test_full_execute_cycle_emits_running_then_complete():
    """End-to-end _execute_job flow: running broadcast → work → ok broadcast.

    Crucially the ok broadcast must come AFTER _save_store so state is fresh.
    """
    app, cron = _build_app_with_cron()

    # Replace inner callback with a simple success; the wrapped on_job will
    # broadcast running then call this.

    async def inner_success(job):
        return "done"

    # Set the inner callback BEFORE wrapping by temporarily reverting the wrap
    # and re-installing. Simpler: monkey-patch the wrapper's closure.
    # Actually, simplest: directly set on_job to a new wrapper that calls inner.
    async def wrapped(job):
        await app.state.broadcast_ws(
            {
                "type": "cron_triggered",
                "job_id": job.id,
                "job_name": job.name,
                "status": "running",
            }
        )
        return await inner_success(job)

    cron.on_job = wrapped

    ws = _FakeWs()
    app.state.ws_clients.add(ws)

    # Build a real job in the store
    job = _make_job(job_id="flow")
    cron._load_store().jobs.append(job)

    asyncio.run(cron._execute_job(job))

    types = [e.get("status") for e in ws.events if e.get("type") == "cron_triggered"]
    # Expect running then ok (order matters)
    assert "running" in types
    assert "ok" in types
    assert types.index("running") < types.index("ok")

    # The ok event must reflect the post-save state
    ok_event = next(e for e in ws.events if e.get("status") == "ok")
    assert ok_event["last_run_at_ms"] is not None
    # For a 60s every-schedule, next_run_at_ms should be set (job is enabled)
    assert ok_event["next_run_at_ms"] is not None


def test_disabled_recurring_job_no_zombie_next_run():
    """v3 guarantee: force-running a disabled job does not set next_run_at_ms."""
    app, cron = _build_app_with_cron()

    async def noop(job):
        return None

    cron.on_job = noop  # bypass broadcast wrapper

    job = _make_job(job_id="disabled", enabled=False)
    cron._load_store().jobs.append(job)

    asyncio.run(cron._execute_job(job))

    assert job.enabled is False
    assert job.state.next_run_at_ms is None
    assert job.state.last_status == "ok"
    assert job.state.last_run_at_ms is not None
