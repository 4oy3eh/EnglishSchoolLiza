"""Per-attempt finalize locks — serialize a regrade against a background finalize.

Both the submit-time background task and the teacher's explicit regrade run
`ResultsService.finalize` for the same attempt, possibly at the same moment (the
teacher clicks "re-grade" while the submit finalize is still talking to Gemini).
Without a lock the two interleave on `upsert_report` and the slower one wins
arbitrarily. With it, the second caller waits for the first and then overwrites,
so the stored report is always the result of one complete, ordered finalize.

The lock is a `threading.RLock` (re-entrant): `finalize` / `record_failure` may
be nested by the same thread (e.g. a future "finalize, and on failure record it"
inside one held lock) without deadlocking. `is_locked` lets read paths (the
dashboard's status chip, the recovery sweep) see that a finalize is in flight and
neither report "missing" nor start a duplicate paid LLM run.

Limitation: this is an in-process registry. It protects one uvicorn worker (the
current single-process deployment); a multi-worker/multi-host deployment would
need a DB-level lock (e.g. `SELECT ... FOR UPDATE` on the attempt row) or a Redis
lock keyed the same way. The registry never evicts — one small lock object per
finalized attempt for the life of the process.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from app.core.logging import get_logger

log = get_logger(__name__)

_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def lock_for(attempt_id: str) -> threading.RLock:
    """The one lock object for this attempt (created on first use)."""
    with _registry_guard:
        lock = _locks.get(attempt_id)
        if lock is None:
            lock = threading.RLock()
            _locks[attempt_id] = lock
        return lock


def is_locked(attempt_id: str) -> bool:
    """True while some thread holds the attempt's finalize lock (work in flight).

    Does not create a registry entry for an attempt never finalized. Python 3.14+
    gives `RLock.locked()` (held by *any* thread, this one included) and that is
    used when present. Older interpreters (the project allows >=3.12) fall back to
    a non-blocking acquire probe, which succeeds when the lock is free *or* already
    ours — i.e. there it means "held by another thread".
    """
    with _registry_guard:
        lock = _locks.get(attempt_id)
    if lock is None:
        return False
    locked = getattr(lock, "locked", None)
    if locked is not None:
        return bool(locked())
    if lock.acquire(blocking=False):  # pragma: no cover - Python < 3.14 only
        lock.release()
        return False
    return True  # pragma: no cover - Python < 3.14 only


@contextmanager
def attempt_lock(attempt_id: str) -> Iterator[None]:
    """Hold the attempt's finalize lock; logs when it had to wait for another run."""
    lock = lock_for(attempt_id)
    if not lock.acquire(blocking=False):
        log.info("finalize attempt=%s waiting for a concurrent finalize to end", attempt_id)
        lock.acquire()
    try:
        yield
    finally:
        lock.release()
