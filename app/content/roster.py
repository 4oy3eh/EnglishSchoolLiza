"""Roster + assignment (Phase 3.3).

Access model (docs/ARCHITECTURE.md): one shareable link **per test**; the teacher
uploads a roster of names; the student picks their name (no free typing); one
attempt per roster entry, resumed on refresh.

This module manages roster entries on top of the existing `AttemptRepository`
and derives each entry's **assignment seed** — the integer that drives pooling
and option-shuffle for that student. The seed is a deterministic function of
`(test_id, roster_entry_id)`, so a resumed attempt reproduces the exact same
layout (rule #7) without persisting the permutation. The attempt row itself is
created by the delivery engine (Phase 4); here we only own the roster + the seed.
"""

from __future__ import annotations

import hashlib
import uuid

from app.core.logging import get_logger
from app.persistence.repository import AttemptRepository
from contracts import RosterEntry

log = get_logger(__name__)

# Seeds are stored in an INTEGER column; keep them within signed 32-bit so the
# value is portable to Postgres (sqlite is dynamic, Postgres INTEGER is not).
_SEED_MODULUS = 2**31 - 1


def derive_seed(test_id: str, roster_entry_id: str) -> int:
    """Deterministic per-student assignment seed (stable across resumes)."""
    digest = hashlib.sha256(f"{test_id}::{roster_entry_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % _SEED_MODULUS


class RosterService:
    """Manage a test's roster and assign per-student seeds."""

    def __init__(self, repo: AttemptRepository) -> None:
        self.repo = repo

    def add_student(self, test_id: str, display_name: str) -> RosterEntry:
        """Add one named roster entry to a test's roster."""
        entry = RosterEntry(
            id=str(uuid.uuid4()), test_id=test_id, display_name=display_name
        )
        self.repo.add_roster_entry(entry)
        log.info("roster add_student test=%s name=%s entry=%s", test_id, display_name, entry.id)
        return entry

    def list_students(self, test_id: str) -> list[RosterEntry]:
        """The roster the student picks their name from."""
        return self.repo.list_roster_entries(test_id)

    def assign_seed(self, entry: RosterEntry) -> int:
        """The reproducible pooling/shuffle seed for a roster entry."""
        seed = derive_seed(entry.test_id, entry.id)
        log.info(
            "assign seed test=%s entry=%s name=%s seed=%d",
            entry.test_id,
            entry.id,
            entry.display_name,
            seed,
        )
        return seed
