# CLAUDE.md — content engine

## Responsibility
Own the **item bank** (tests/sections/items/assets), **pooling** (per-attempt
randomization), and the **roster + assignment** (one link per test). This is the
authoring/selection side of content — it decides *which* content a student sees,
never whether their answer is right.

## Inputs / Outputs
- **In:** `contracts/` authoring models (`Test`/`Section`/`Item`), asset bytes,
  roster names, an attempt seed.
- **Out:** persisted tests (via `ContentRepository`), stored assets (via
  `StorageBackend`), a reproducible `AttemptLayout` (drawn `section_ids` +
  per-item `OptionShuffle`), and roster entries with derived seeds.

## Must NOT
- Decide if an answer is correct (that's `grading`).
- Send the answer key to the client. CRUD persists authoring content *with* the
  key; the student-facing projection/stripping is `delivery`'s job (rule #1).
- Shuffle **item order within** a section/passage/recording — randomization is at
  the section-pool level + option-shuffle only (golden rule #7).
- Auto-publish. `publish()` only flips an already-authored draft (rule #5).

## Key invariants
- **Reproducible from the seed.** `build_attempt_layout(bank, seed)` is a pure
  function: same seed -> same sections + same option permutation. The permutation
  is recomputed from `Attempt.seed`, not stored as its own column. Per-student
  seed = `derive_seed(test_id, roster_entry_id)` (stable across resumes).
- **Log every pooling/assignment decision** at INFO via `app/core/logging.py`.

## Layout
- `storage.py` — `StorageBackend` interface + `FilesystemStorage` (MinIO later).
- `pooling.py` — `TestBank`/`SectionPool` bank shape; `AttemptLayout`/
  `OptionShuffle`; `draw_sections`, `shuffle_options`, `build_attempt_layout`,
  `bank_from_sections`. All pure, seed-driven.
- `roster.py` — `RosterService` (manage names) + `derive_seed`.
- `service.py` — `ContentService` facade (bank CRUD + assets). Builds on the
  existing `ContentRepository`/`AttemptRepository` — do not re-model rows here.

## Tests
`tests/test_content_*.py` — pooling determinism + two-student divergence,
permutation round-trip, storage round-trip + traversal guard, roster/seed.
