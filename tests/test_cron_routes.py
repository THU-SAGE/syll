"""Tests for the unified cron REST API and CronService lifecycle."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from syll.cron.service import CronService, _now_ms
from syll.cron.types import CronJob, CronPayload, CronSchedule
from syll.web.app import create_app

# ---------- Stubs ----------


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


def _make_config(tmp_dir: Path):
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


def _build_app(channel_manager=None):
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
    app.state.channel_manager = channel_manager
    return app, cron, tmp


# ---------- Lifecycle idempotency ----------


def test_cron_lifecycle_idempotent():
    """Calling start() twice must not create a second timer task."""
    tmp = Path(tempfile.mkdtemp())
    cron = CronService(tmp / "cron.json")

    async def _run():
        await cron.start()
        assert cron._running is True
        first_task = cron._timer_task
        # Second call should be a no-op
        await cron.start()
        assert cron._timer_task is first_task
        cron.stop()
        assert cron._running is False
        # Second stop is a no-op
        cron.stop()

    asyncio.run(_run())


# ---------- Capabilities ----------


def test_capabilities_without_channel_manager():
    app, _, _ = _build_app(channel_manager=None)
    with TestClient(app) as client:
        r = client.get("/api/v1/cron/capabilities")
        assert r.status_code == 200
        data = r.json()
        assert data["deliver_available"] is False
        assert data["enabled_channels"] == []


def test_capabilities_with_channel_manager():
    cm = SimpleNamespace(enabled_channels=["telegram", "feishu"])
    app, _, _ = _build_app(channel_manager=cm)
    with TestClient(app) as client:
        r = client.get("/api/v1/cron/capabilities")
        assert r.status_code == 200
        data = r.json()
        assert data["deliver_available"] is True
        assert set(data["enabled_channels"]) == {"telegram", "feishu"}


# ---------- CRUD ----------


def test_cron_crud_flow():
    app, cron, _ = _build_app()
    with TestClient(app) as client:
        # Empty list
        r = client.get("/api/v1/cron/jobs")
        assert r.status_code == 200
        assert r.json()["jobs"] == []

        # Create valid
        body = {
            "name": "ping",
            "action_type": "message",
            "message": "hello",
            "schedule_type": "every",
            "every_seconds": 60,
        }
        r = client.post("/api/v1/cron/jobs", json=body)
        assert r.status_code == 200
        job = r.json()["job"]
        assert job["payload"]["metadata"]["action_type"] == "message"
        assert job["state"]["next_run_at_ms"] is not None
        assert job["schedule"]["every_ms"] == 60_000
        job_id = job["id"]

        # List shows it
        r = client.get("/api/v1/cron/jobs")
        assert len(r.json()["jobs"]) == 1

        # Disable
        r = client.patch(f"/api/v1/cron/jobs/{job_id}", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["job"]["enabled"] is False

        # Delete
        r = client.delete(f"/api/v1/cron/jobs/{job_id}")
        assert r.status_code == 200
        r = client.get("/api/v1/cron/jobs")
        assert r.json()["jobs"] == []


def test_create_rejects_invalid_cron():
    app, _, _ = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/cron/jobs",
            json={
                "name": "bad",
                "action_type": "message",
                "message": "hi",
                "schedule_type": "cron",
                "cron_expr": "not-a-cron",
            },
        )
        assert r.status_code == 400
        assert "Invalid" in r.json()["detail"] or "schedule" in r.json()["detail"].lower()


def test_create_rejects_expired_at():
    app, _, _ = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/cron/jobs",
            json={
                "name": "past",
                "action_type": "message",
                "message": "hi",
                "schedule_type": "at",
                "at_ms": 1,
            },
        )
        assert r.status_code == 400


def test_delete_missing_returns_404():
    app, _, _ = _build_app()
    with TestClient(app) as client:
        r = client.delete("/api/v1/cron/jobs/does-not-exist")
        assert r.status_code == 404


# ---------- Deliver validation ----------


def test_deliver_rejected_without_channel_manager():
    app, _, _ = _build_app(channel_manager=None)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/cron/jobs",
            json={
                "name": "deliver",
                "action_type": "message",
                "message": "hi",
                "schedule_type": "every",
                "every_seconds": 60,
                "deliver": True,
                "channel": "telegram",
                "to": "123",
            },
        )
        assert r.status_code == 400
        assert "Delivery" in r.json()["detail"]


def test_deliver_rejected_for_unknown_channel():
    cm = SimpleNamespace(enabled_channels=["telegram"])
    app, _, _ = _build_app(channel_manager=cm)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/cron/jobs",
            json={
                "name": "deliver",
                "action_type": "message",
                "message": "hi",
                "schedule_type": "every",
                "every_seconds": 60,
                "deliver": True,
                "channel": "discord",
                "to": "123",
            },
        )
        assert r.status_code == 400
        assert "discord" in r.json()["detail"]


def test_deliver_accepted_with_enabled_channel():
    cm = SimpleNamespace(enabled_channels=["telegram"])
    app, _, _ = _build_app(channel_manager=cm)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/cron/jobs",
            json={
                "name": "deliver",
                "action_type": "message",
                "message": "hi",
                "schedule_type": "every",
                "every_seconds": 60,
                "deliver": True,
                "channel": "telegram",
                "to": "123",
            },
        )
        assert r.status_code == 200


# ---------- Legacy metadata fallback ----------


def test_legacy_metadata_fallback():
    """Jobs without metadata should be classified from name prefix."""
    app, cron, tmp = _build_app()

    # Inject a legacy-style job directly into the store
    async def _inject():
        await cron.start()
        store = cron._load_store()
        store.jobs.append(
            CronJob(
                id="legacy1",
                name="gui-skill:demo",
                enabled=True,
                schedule=CronSchedule(kind="every", every_ms=60_000),
                payload=CronPayload(message="replay", metadata=None),
                created_at_ms=_now_ms(),
                updated_at_ms=_now_ms(),
            )
        )
        cron._save_store()

    asyncio.run(_inject())

    with TestClient(app) as client:
        r = client.get("/api/v1/cron/jobs")
        jobs = r.json()["jobs"]
        legacy = [j for j in jobs if j["id"] == "legacy1"]
        assert len(legacy) == 1
        assert legacy[0]["payload"]["metadata"]["action_type"] == "gui_skill"
        assert legacy[0]["payload"]["metadata"]["skill_name"] == "demo"


# ---------- PATCH re-enable validation ----------


def test_patch_reenable_expired_at_rolls_back():
    """Re-enabling an expired at-job must roll back and 400."""
    app, cron, _ = _build_app()

    async def _setup():
        await cron.start()
        # Directly inject an at-job with at_ms in the past
        store = cron._load_store()
        store.jobs.append(
            CronJob(
                id="expired",
                name="past-task",
                enabled=False,  # Manually disabled
                schedule=CronSchedule(kind="at", at_ms=1),
                payload=CronPayload(message="old"),
                created_at_ms=_now_ms(),
                updated_at_ms=_now_ms(),
            )
        )
        cron._save_store()

    asyncio.run(_setup())

    with TestClient(app) as client:
        r = client.patch("/api/v1/cron/jobs/expired", json={"enabled": True})
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()

        # Verify the job is still disabled (rolled back)
        r = client.get("/api/v1/cron/jobs")
        job = next(j for j in r.json()["jobs"] if j["id"] == "expired")
        assert job["enabled"] is False


# ---------- Run Now semantics ----------


def test_run_now_disabled_recurring_no_zombie_next_run():
    """Force-running a disabled recurring job must NOT set next_run_at_ms."""
    app, cron, _ = _build_app()

    executed = []

    async def _on_job(job):
        executed.append(job.id)
        return "done"

    # The create_app wraps cron.on_job for broadcast. We need to set the
    # original AFTER create_app so the wrapper picks it up on next assignment.
    # But since the wrapper is already installed, we need to swap the original
    # via a closure trick. Simpler: directly call run_job on the service.

    async def _scenario():
        # Manually set the inner on_job (bypass the wrapper for this test)

        async def noop(job):
            return None  # no broadcast in unit test

        cron.on_job = noop

        await cron.start()

        # Create an every-60s job
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/cron/jobs",
                json={
                    "name": "recur",
                    "action_type": "message",
                    "message": "hi",
                    "schedule_type": "every",
                    "every_seconds": 60,
                },
            )
            jid = r.json()["job"]["id"]

            # Disable
            r = client.patch(f"/api/v1/cron/jobs/{jid}", json={"enabled": False})
            assert r.status_code == 200
            assert r.json()["job"]["state"]["next_run_at_ms"] is None

            # Force run
            r = client.post(f"/api/v1/cron/jobs/{jid}/run")
            assert r.status_code == 200
            assert r.json()["ok"] is True

            # State check: still disabled, next_run_at_ms still None
            r = client.get("/api/v1/cron/jobs")
            job = next(j for j in r.json()["jobs"] if j["id"] == jid)
            assert job["enabled"] is False
            assert job["state"]["next_run_at_ms"] is None
            assert job["state"]["last_status"] == "ok"

    asyncio.run(_scenario())


def test_run_now_returns_error_on_exception():
    """Run-now endpoint should surface inner exceptions as ok=false."""
    app, cron, _ = _build_app()

    async def _scenario():
        async def failing(job):
            raise RuntimeError("boom")

        cron.on_job = failing
        await cron.start()

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/cron/jobs",
                json={
                    "name": "fail",
                    "action_type": "message",
                    "message": "hi",
                    "schedule_type": "every",
                    "every_seconds": 60,
                },
            )
            jid = r.json()["job"]["id"]

            r = client.post(f"/api/v1/cron/jobs/{jid}/run")
            assert r.status_code == 200
            data = r.json()
            assert data["ok"] is False
            assert "boom" in data["error"]

    asyncio.run(_scenario())
