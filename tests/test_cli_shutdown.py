"""Tests for gateway service task shutdown behavior."""

import asyncio

from syll.cli.commands import _drain_gateway_service_tasks


def test_gateway_shutdown_waits_for_graceful_tasks_before_cancelling():
    """uvicorn should get a chance to observe should_exit before task cancellation."""
    events: list[str] = []

    async def graceful_service():
        await asyncio.sleep(0.01)
        events.append("web-finished")

    async def long_running_service():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            events.append("worker-cancelled")
            raise

    async def run_test():
        web_task = asyncio.create_task(graceful_service())
        worker_task = asyncio.create_task(long_running_service())

        await _drain_gateway_service_tasks(
            [worker_task, web_task],
            graceful_tasks={web_task},
            graceful_timeout=0.2,
        )

        assert web_task.done()
        assert not web_task.cancelled()
        assert worker_task.cancelled()

    asyncio.run(run_test())

    assert events == ["web-finished", "worker-cancelled"]
