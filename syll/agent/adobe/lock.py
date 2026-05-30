"""Single-flight lock shared by the Adobe GUI tools.

Only ONE Adobe GUI run may touch the screen at a time. Because the GUI
tools are driven from async code, we deliberately avoid a module-level
``asyncio.Lock`` created at import time: such a lock binds to whatever
event loop happens to be running when the module is first imported, and
later acquisitions from a different loop raise. Instead we keep a tiny
module-level state object (holder label + monotonic acquired-at stamp)
guarded by a plain :class:`threading.Lock`, and expose a non-blocking
``try_acquire`` semantic.

The lock is single-flight: a second caller is rejected immediately
(returns ``None``) unless the current holder has gone stale past a
time-to-live, in which case the stale lease is reclaimed and a fresh one
is granted.

TTL reclaim assumes the prior holder is DEAD — it renumbers ownership but
does not abort a still-running GUI step. Callers must therefore pass a
``ttl_seconds`` that comfortably exceeds the largest wall-clock their run
can take (GUI step + any export wait); otherwise a slow-but-alive run
could be reclaimed and two runs would fight for the screen. The default
is deliberately generous for this reason.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import TracebackType


@dataclass
class _LockState:
    """Mutable module-level holder record guarded by ``guard``."""

    guard: threading.Lock = field(default_factory=threading.Lock)
    holder: str | None = None
    acquired_at: float | None = None
    # Monotonically increasing token so a reclaimed/re-granted lease can
    # tell whether it still owns the lock when ``release`` is called.
    token: int = 0


_STATE = _LockState()


class AdobeLease:
    """Handle representing ownership of the Adobe single-flight lock.

    Acts as both a synchronous (``with``) and asynchronous (``async
    with``) context manager. ``release`` is idempotent: calling it more
    than once, or after the lease has already been reclaimed by another
    caller via the stale TTL, is a no-op.
    """

    def __init__(self, label: str, token: int) -> None:
        self._label = label
        self._token = token
        self._released = False

    @property
    def label(self) -> str:
        return self._label

    def release(self) -> None:
        """Release the lock if this lease still owns it. Idempotent."""
        if self._released:
            return
        self._released = True
        with _STATE.guard:
            # Only clear the holder if our token is still the live one;
            # otherwise a stale-TTL reclaim already handed it to someone
            # else and we must not stomp on their lease.
            if _STATE.token == self._token and _STATE.holder is not None:
                _STATE.holder = None
                _STATE.acquired_at = None

    # -- sync context manager --
    def __enter__(self) -> AdobeLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # -- async context manager --
    async def __aenter__(self) -> AdobeLease:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def try_acquire_adobe(label: str, *, ttl_seconds: float = 1800.0) -> AdobeLease | None:
    """Attempt to acquire the Adobe single-flight lock without blocking.

    Returns a fresh :class:`AdobeLease` if the lock was free, or if the
    current holder is older than ``ttl_seconds`` (in which case the stale
    lease is reclaimed). Returns ``None`` if a live holder is present.
    """
    now = time.monotonic()
    with _STATE.guard:
        if _STATE.holder is not None and _STATE.acquired_at is not None:
            age = now - _STATE.acquired_at
            if age < ttl_seconds:
                # A live holder is in possession; reject immediately.
                return None
            # Holder is stale past the TTL: reclaim it below.
        _STATE.token += 1
        _STATE.holder = label
        _STATE.acquired_at = now
        return AdobeLease(label, _STATE.token)


def is_busy() -> bool:
    """Return ``True`` if a holder currently owns the lock."""
    with _STATE.guard:
        return _STATE.holder is not None


def current_holder() -> str | None:
    """Return the label of the current holder, or ``None`` if free."""
    with _STATE.guard:
        return _STATE.holder
