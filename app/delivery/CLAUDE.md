# CLAUDE.md — delivery engine

## Responsibility
The **exam runtime**: drive one attempt from start to submit. Validate the exam
window, create-or-resume the single attempt for a roster entry (refresh-safe),
serve items one-at-a-time, save answers, run the server-authoritative timer, and
finalize on submit.

## Inputs / Outputs
- **In:** a `roster_entry_id` + an `ExamWindow`, the persisted authoring `Test`
  (via `ContentRepository`), the per-attempt seed (`derive_seed`), and the
  per-attempt `AttemptLayout` (from `app/content` pooling).
- **Out:** `Attempt` rows (lifecycle), `Answer` rows (canonical responses), and
  **`Client*`** projections served to the browser (never carrying an answer key).

## Must NOT
- **Send `correct` to the client** (golden rule #1). Everything served goes
  through `projection.py` -> the `Client*` family, which is structurally keyless.
- **Pause the timer** (golden rule #3). The deadline is fixed at start
  (`min(started_at + duration, window close)`) and measured against wall-clock.
- **Touch grading or integrity.** Delivery records answers and lifecycle only.
- Re-model rows or invent a permutation table — the layout is recomputed from
  `Attempt.seed` every call (rule #7), never persisted.
- Start a **second** attempt for a roster entry — reopening the link resumes.

## Key invariants
- **Refresh-safe resume.** `start` returns the existing attempt if the roster
  entry already points at one; window/grace is validated only on first create.
- **Reproducible draw.** Seed = `derive_seed(test_id, roster_entry_id)`; the
  `AttemptLayout` (drawn sections + option shuffles) is rebuilt from it, so a
  resume serves the identical test.
- **Displayed -> canonical.** `save_answer` takes the *displayed* option index for
  `single_choice` and maps it to the canonical key via `OptionShuffle` before
  persisting; text items store their text/pool key directly.
- **Late actions rejected.** Saving/submitting past the deadline raises
  `AttemptExpiredError`; `get_state` flips a crossed-deadline attempt to
  `expired`.
- **One expiry transition.** `_expire_if_overdue` is the only code that flips
  `in_progress -> expired` (status only: saved answers stay, the deadline never
  moves). It goes through `AttemptRepository.mark_expired`, a conditional UPDATE, so
  exactly one caller "owns" each transition. Three entry points share it:
  `get_state` (lazy; `AttemptState.just_expired` is true only on the flipping call),
  `expire_if_overdue(attempt_id)` (the HTTP layer, after a late save/submit), and
  `expire_overdue(now, test_id=None)` (eager: every `in_progress` attempt with
  `deadline <= now` via `list_overdue_attempt_ids` — the student who closed the tab
  and never came back). Idempotent; logs INFO per expired attempt.
- **Roster follows the expiry.** `mark_expired` moves the roster entry
  `in_progress -> expired` (`RosterStatus` literal; the column is a free string, so
  no migration) in the same UPDATE transaction — `expired` is a finished state like
  `submitted` (picker "(done)", disabled). `expire_overdue` also runs
  `sync_expired_roster_status(test_id)`, an idempotent repair for rows expired before
  this rule existed. Teacher `reset` -> `not_started`, `reopen` -> `in_progress`
  (original deadline kept, so a past one re-expires both at once).
- **Lost expiry race re-reads fresh.** When `mark_expired` returns False (another
  writer moved the attempt on), `_expire_if_overdue` re-reads with
  `get_attempt(id, fresh=True)` (`populate_existing`), never the identity-mapped row
  (stale `in_progress`, or even rewritten to `expired` in memory by the no-op
  ORM UPDATE's session sync).
- **Expiry triggers no grading here.** Delivery only reports the transition; the
  API (`apps/api/delivery.py`) schedules `finalize_attempt`, and the results
  recovery sweep calls `expire_overdue` itself (results -> delivery, never the
  reverse).
- **Log the full lifecycle** at INFO (start / resume / serve / answer / expire /
  submit) via `app/core/logging.py`.

## Layout
- `projection.py` — pure authoring -> `Client*` projection (strips the answer key,
  applies option shuffle). The enforcement point for golden rule #1.
- `service.py` — `DeliveryService` (lifecycle), `ExamWindow`, `AttemptState`, and
  the `DeliveryError` hierarchy. `bank_builder` is injectable; the default emits
  one singleton pool per section (deliver the whole authored test + option
  shuffle), so callers with alternative passages can opt into section divergence.

## HTTP surface (Phase 11)
`apps/api/delivery.py` exposes this service to the student runner (`apps/web/exam.*`)
under `/exam`, unauthenticated behind the per-test share link (students are
identified by `roster_entry_id`): roster pick, start (create-or-resume), keyless
`ClientTest`/`ClientItem`, the server-authoritative `/state` timer, save answer
(displayed->canonical), and submit. `DeliveryError` subclasses map to HTTP codes
(404 / 403 window / 409 expired-or-finalized); `_http()` logs every rejection at
WARNING.

**Finalize on lazy expiry (Phase 13).** When a request performs the
`in_progress -> expired` transition — `/state` or `/start` (resume) via
`AttemptState.just_expired`, or a late save/submit via `expire_if_overdue` — the
router commits first (SQLite lock, like submit) and schedules the same background
`finalize_attempt` the submit endpoint does, once per transition (later polls see
`expired` and schedule nothing). A late save/submit still answers 409 with the
usual `{"detail": ...}` body, returned as a `JSONResponse` (not raised) so the
committed expiry and the background task survive. The exam window is permissive for now (`_ALWAYS_OPEN`), so the deadline is
driven by `duration_minutes`; a scheduled window on the authoring `Test` is a later
concern.

**Serving model:** the runner prefetches the whole `ClientTest` once and renders
**one item at a time client-side**. The per-item `GET /exam/attempts/{id}/items/{item_id}`
endpoint supports *true* lazy server-side serving (a stricter anti-dump runner that
never ships the full test to the network tab) — kept on the surface for that future
runner even though the current `exam.js` doesn't call it.

**Asset serving (resolved in Phase 12):** stimulus assets (audio mp3 / image options)
are referenced by the runner as `/assets/{asset_id}` and now resolved by
`apps/api/assets.py` (`GET /assets/{asset_id}`), which streams the bytes through the
shared `StorageBackend` (`FilesystemStorage` now, MinIO later). It is unauthenticated
like the rest of delivery — assets are stimulus content, never the answer key — and
maps a missing/traversal-unsafe id to 404. Tests: `tests/test_assets_api.py`.

## Tests
`tests/test_delivery_*.py` — served payload has no `correct`; window/grace
enforced; refresh resumes the same attempt; displayed->canonical round-trip; late
submit rejected; timer expiry; `expire_overdue` flips only past-deadline
`in_progress` attempts and is idempotent; `mark_expired` is conditional; expiry
(lazy / eager) marks the roster entry `expired` in the same transaction, never
overwrites a `submitted` one, and `expire_overdue` heals stale `in_progress` roster
rows; a lost expiry race returns the fresh DB row. `test_delivery_api.py` also checks
the pick-your-name roster endpoint shows `expired` after a lazy / late-submit
expiry; `test_e2e_full.py::test_picker_shows_expired_student_as_done` checks the
real picker renders it "(done)" + disabled.
`tests/test_delivery_api.py` covers the HTTP surface (keyless serve, full flow,
resume, server-side expiry + 409, 404s) and that a lazy expiry via state / resume /
late save / late submit schedules the finalize exactly once (spy on
`app.results.finalize_attempt`, autouse in that file).
