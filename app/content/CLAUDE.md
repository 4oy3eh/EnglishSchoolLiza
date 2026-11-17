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
  Imports (`import_test`) and copies (`duplicate_test`) are always stored as drafts
  (an imported `status` is ignored) — they land in the admin review queue.
- Silently change a published test's answer key or item set (see the editor guard).

## Key invariants
- **Reproducible from the seed.** `build_attempt_layout(bank, seed)` is a pure
  function: same seed -> same sections + same option permutation. The permutation
  is recomputed from `Attempt.seed`, not stored as its own column. Per-student
  seed = `derive_seed(test_id, roster_entry_id)` (stable across resumes).
- **Log every pooling/assignment decision** at INFO via `app/core/logging.py`.
- **Editing = one gate (`ContentService._commit`).** Every update builds the WHOLE new
  `Test` and goes through: `editor.validate_test_data` (the Pydantic contracts + the
  semantic rules Pydantic can't express: `single_choice.correct` ∈ option keys, unique
  non-blank option keys, `matching` needs a `matching_pool` stimulus and `correct` ∈
  its keys, no blank accepted / variants / `rejected` / bullets / option text, unique
  URL-safe ids
  `[A-Za-z0-9][A-Za-z0-9._-]{0,119}`, safe asset ids) → referenced assets exist in
  storage → section/item ids not owned by ANOTHER test (they are global PKs) → the
  publish guard → `ContentRepository.replace_test` (sections/items rewritten; roster
  + attempts untouched) → INFO log `content edit action=… test=… target=…`.
  Refusals are `editor.ContentEditError` subclasses with a **Russian** message:
  `ContentValidationError` (400), `ContentNotFoundError` (404), `ContentConflictError`
  (409). Pydantic errors are translated (`ru_validation_message`: "Раздел 2 → вопрос 1
  → варианты ответа: нужно не меньше 2 элемент(ов)").
- **The lock guard (`editor.check_edit_allowed`).** A draft WITHOUT attempts → anything
  goes. A *locked* test (`editor.is_locked`: published OR `count_attempts > 0` — a draft
  after unpublish included) → only a *cosmetic* change (`is_cosmetic_change`: the test is
  identical once `title` / `text` / `prompt` / `alt` / `bullet_points` are stripped —
  EXCEPT inside `options` (`KEY_BEARING_CONTAINERS`): single_choice options and the
  matching-pool letters + texts are compared verbatim, text and alt included, because
  swapping the texts of A and B moves the right answer without touching `correct`) and
  only with `force=True` (WARNING "FORCED on PUBLISHED" / "FORCED on a draft WITH N
  attempt(s)"). Anything else (key, accepted, variants, `rejected`, rubric, word_min,
  grade_mode, option keys / texts / kinds / images, pool texts, audio settings, ids,
  order, level, duration, add/remove) → 409: published without attempts → "unpublish or
  make a draft copy"; with attempts → ONLY «Создать черновик-копию» (a regrade would
  re-score finished attempts against the new key and the attempt layout is rebuilt from
  the edited sections — unpublishing does not unlock it). `duplicate_test` → `<id>-v2` (next free `-vN`; a
  `-vN` suffix is bumped), ids rebased by `reid_test` (`<old>-…` → `<new>-…`, others get
  `<new>-` prepended so `…-qN` numbering survives), status draft; assets shared.
- **Rule #7 in the editor.** `reorder_items` is refused (409) unless
  `editor.can_reorder_items(section)` — a `passage_text` / `gapped_text` stimulus with
  blank text and no images (a section of standalone questions). Sections may be
  reordered on a draft.
- **Import / export.** `export_test` returns the authoring `Test` WITH the key
  (teacher-only). `import_test(json, assets, replace=, new_id=)`: validates, optional
  re-id, every referenced asset must be uploaded (file name == asset id) or already
  stored (missing ids listed, 400); id collision → 409 unless `replace` AND no attempts
  (roster kept; replacing a PUBLISHED test drops it to draft → WARNING log +
  `ImportOutcome.unpublished`); an uploaded blob that differs from a stored one (sha256)
  is refused unless `replace` and no other test references it; unreferenced uploads are
  ignored and reported. Too-deep JSON (`RecursionError`) → Russian 400. Asset values may
  be bytes or seekable streams: validated by header + size (`editor.validate_asset`),
  compared by digest and copied with `StorageBackend.put_stream` — never all in memory. `referenced_asset_ids` now lives in `editor.py` (re-exported by
  `load_test`).
- **Asset upload (`upload_asset`).** Only `.mp3 .png .jpg/.jpeg .webp`, the magic bytes
  must match the extension (`editor.sniff_media_type`; an mp3 without ID3 must be an
  MPEG **Layer III** frame header — `is_mp3_frame` — so AAC/ADTS, MP1/MP2 are refused),
  non-empty, ≤ `MAX_ASSET_BYTES` (50 MB). The id is the base name (directories / `..`
  dropped, unsafe chars → `-`, `.jpeg` → `.jpg`); identical bytes reuse the id,
  different bytes get `-2`, `-3`, … — an upload never overwrites a stored asset. The
  name is reserved atomically: `put_stream(exclusive=True)` (filesystem: temp file in
  `<root>/.incoming/` + `os.link`, which fails if the target exists → `AssetExistsError`
  → re-check / next suffix), so concurrent same-name uploads cannot clobber each other.
- **Storage streaming.** `StorageBackend.put_stream` / `digest` / `version` have
  buffering defaults (any backend works; the default `exclusive` is NOT atomic);
  `FilesystemStorage` streams in 1 MB chunks and swaps files in atomically
  (`os.replace`). `version` (mtime + size) is the cheap ETag token for `/assets`.

## Layout
- `storage.py` — `StorageBackend` interface (+ `put_stream` / `digest` / `version`,
  `AssetExistsError`, `stream_digest`) + `FilesystemStorage` (MinIO later).
- `pooling.py` — `TestBank`/`SectionPool` bank shape; `AttemptLayout`/
  `OptionShuffle`; `draw_sections`, `shuffle_options`, `build_attempt_layout`,
  `bank_from_sections`. All pure, seed-driven.
- `roster.py` — `RosterService` (manage names, `regenerate_pin`) + `derive_seed` +
  the PIN helpers `normalize_pin` / `is_well_formed_pin` / `pin_matches` (Phase 14).
  The PIN itself is generated by `AttemptRepository.add_roster_entry` (`new_pin`,
  `secrets`, unique per test) so EVERY insert path — teacher add / bulk, demo seed,
  `load_test(roster=...)` / `load_fixtures` — gets one; see app/delivery/CLAUDE.md.
- `service.py` — `ContentService` facade (bank CRUD + assets + editing:
  `update_test_meta` / `update_section` / `reorder_sections` / `reorder_items` /
  `update_item` / `add_item` / `remove_item` / `add_section` / `remove_section` /
  `duplicate_test` / `export_test` / `import_test` / `upload_asset`). Builds on the
  existing `ContentRepository` (`replace_test`, `count_attempts`,
  `ids_in_use_elsewhere`, `test_exists`) — do not re-model rows here.
- `editor.py` — pure editing helpers: errors, Russian validation messages,
  `validate_test_data`, `is_cosmetic_change` / `check_edit_allowed`,
  `can_reorder_items`, `reid_test` / `next_copy_id`, asset type / size / name checks,
  `referenced_asset_ids`.
- `transfer.py` — CLI `python -m app.content.transfer export|import` (`make export-test
  id=…` / `make import-test file=… assets=dir`), thin wrappers over the service.
- `load_test.py` / `ingest_*_2022.py` — the legacy per-test Python-builder path (new
  tests are authored as JSON and imported, see `docs/INGEST_VIA_CLAUDE_CODE.md` §6a).

## Tests
`tests/test_content_*.py` — pooling determinism + two-student divergence,
permutation round-trip, storage round-trip + traversal guard, roster/seed.
`tests/test_content_editor.py` — every bundled test passes the editor validation;
Russian Pydantic messages; cosmetic-vs-structural; each update valid + invalid; rule #7
reorder guard; published guard (force for text, 409 for key / set even with force,
with and without attempts); a draft WITH attempts is locked too (409 → «Создать
черновик-копию», forced cosmetic fix + WARNING, the copy is free); option / pool texts
are key-bearing (A/B swap, pool swap, option "typo" refused; stimulus / title still
cosmetic); duplicate (`-v2`, `-v3`,
rebased ids, original untouched); export → import (new id) round trip; import never
publishes, collision / replace / attempts, missing-asset report, bad JSON / assets,
shared-asset clobber; replace-of-published → `unpublished` + WARNING; deep JSON → 400;
streamed import assets (digest compare, size cap); upload types / size / traversal /
collisions; streamed upload + atomic name reservation (stale `exists` race, 8 threads
same name → 8 distinct intact blobs); storage digest / version; mp3 sniff refuses
AAC/ADTS / Layer II / bad headers; CLI export + import (+ assets dir, missing asset
exit 1). `tests/test_content_editor_e2e.py` (Playwright,
see admin).
