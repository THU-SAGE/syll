"""CronService persists last_media across save/load and clears it on error."""

import asyncio
from pathlib import Path

from syll.agent.result import AgentResult
from syll.cron.service import CronService
from syll.cron.types import CronJob, CronPayload, CronSchedule


def _make_job(job_id="j1"):
    return CronJob(
        id=job_id,
        name="podcast",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(message="run podcast"),
    )


def test_last_media_round_trip(tmp_path: Path):
    store_path = tmp_path / "cron.json"
    cron = CronService(store_path)

    job = _make_job()
    job.state.last_media = ["/tmp/a.mp3", "/tmp/b.wav"]
    cron._load_store().jobs.append(job)
    cron._save_store()

    # Fresh service must see the persisted value.
    cron2 = CronService(store_path)
    loaded = cron2._load_store().jobs
    assert len(loaded) == 1
    assert loaded[0].state.last_media == ["/tmp/a.mp3", "/tmp/b.wav"]


def test_execute_job_persists_agent_result_media(tmp_path: Path):
    store_path = tmp_path / "cron.json"
    cron = CronService(store_path)

    job = _make_job()
    cron._load_store().jobs.append(job)

    async def fake_on_job(j):
        return AgentResult(text="ok", media=["/tmp/song.mp3"])

    cron.on_job = fake_on_job
    asyncio.run(cron._execute_job(job))

    assert job.state.last_status == "ok"
    assert job.state.last_media == ["/tmp/song.mp3"]


def test_execute_job_clears_stale_media_on_error(tmp_path: Path):
    store_path = tmp_path / "cron.json"
    cron = CronService(store_path)

    job = _make_job()
    job.state.last_media = ["/tmp/stale.mp3"]
    cron._load_store().jobs.append(job)

    async def boom(j):
        raise RuntimeError("provider down")

    cron.on_job = boom
    asyncio.run(cron._execute_job(job))

    assert job.state.last_status == "error"
    assert job.state.last_media == []
