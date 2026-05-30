"""Single-flight lock tests for the Adobe conversational integration.

CI-safe and cross-platform: exercises the in-process lock state machine only.
No Adobe app and no GUI are involved. The module keeps a shared module-level
holder, so each test releases what it acquires to avoid leaking state.

Author: zhangbo <226653803@qq.com>
"""

import time

import pytest

from syll.agent.adobe.lock import (
    AdobeLease,
    current_holder,
    is_busy,
    try_acquire_adobe,
)


@pytest.fixture(autouse=True)
def _reset_lock():
    """Ensure a clean lock before and after each test by reclaiming via a 0 TTL."""
    stale = try_acquire_adobe("reset", ttl_seconds=0.0)
    if stale is not None:
        stale.release()
    yield
    stale = try_acquire_adobe("reset", ttl_seconds=0.0)
    if stale is not None:
        stale.release()


def test_second_acquire_is_rejected_while_held():
    first = try_acquire_adobe("photoshop_cutout")
    assert first is not None
    assert is_busy() is True
    assert current_holder() == "photoshop_cutout"

    second = try_acquire_adobe("clean_audio_in_audition")
    assert second is None  # single-flight: live holder rejects

    first.release()


def test_release_lets_a_later_caller_in():
    first = try_acquire_adobe("photoshop_cutout")
    assert first is not None
    assert try_acquire_adobe("clean_audio_in_audition") is None

    first.release()
    assert is_busy() is False

    third = try_acquire_adobe("clean_audio_in_audition")
    assert third is not None
    assert current_holder() == "clean_audio_in_audition"
    third.release()


def test_stale_ttl_reclaim_with_tiny_ttl():
    first = try_acquire_adobe("photoshop_cutout")
    assert first is not None

    time.sleep(0.02)
    # A tiny TTL means the prior lease is now stale and gets reclaimed.
    reclaimed = try_acquire_adobe("clean_audio_in_audition", ttl_seconds=0.01)
    assert reclaimed is not None
    assert current_holder() == "clean_audio_in_audition"

    # The original lease must NOT stomp on the reclaimed one when released.
    first.release()
    assert current_holder() == "clean_audio_in_audition"
    reclaimed.release()


def test_release_is_idempotent():
    lease = try_acquire_adobe("photoshop_cutout")
    assert lease is not None
    lease.release()
    lease.release()  # no-op, must not raise
    assert is_busy() is False


def test_sync_with_releases():
    lease = try_acquire_adobe("photoshop_cutout")
    assert lease is not None
    with lease:
        assert is_busy() is True
    assert is_busy() is False
    assert isinstance(lease, AdobeLease)


async def test_async_with_releases():
    lease = try_acquire_adobe("clean_audio_in_audition")
    assert lease is not None
    async with lease:
        assert is_busy() is True
    assert is_busy() is False
