"""Content engine: item bank, pooling, roster + assignment (Phase 3).

Public surface:

* `ContentService` — item-bank CRUD + asset storage.
* `StorageBackend` / `FilesystemStorage` — asset blob store.
* pooling: `TestBank`, `SectionPool`, `AttemptLayout`, `OptionShuffle`,
  `build_attempt_layout`, `draw_sections`, `shuffle_options`, `bank_from_sections`.
* `RosterService`, `derive_seed` — roster management + per-student seed.
"""

from __future__ import annotations

from app.content.pooling import (
    AttemptLayout,
    OptionShuffle,
    SectionPool,
    TestBank,
    bank_from_sections,
    build_attempt_layout,
    draw_sections,
    shuffle_options,
)
from app.content.roster import RosterService, derive_seed
from app.content.service import ContentService
from app.content.storage import FilesystemStorage, StorageBackend

__all__ = [
    "ContentService",
    "StorageBackend",
    "FilesystemStorage",
    "TestBank",
    "SectionPool",
    "AttemptLayout",
    "OptionShuffle",
    "build_attempt_layout",
    "draw_sections",
    "shuffle_options",
    "bank_from_sections",
    "RosterService",
    "derive_seed",
]
