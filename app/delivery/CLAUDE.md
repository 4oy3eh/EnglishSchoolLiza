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
WARNING. The exam window is permissive for now (`_ALWAYS_OPEN`), so the deadline is
driven by `duration_minutes`; a scheduled window on the authoring `Test` is a later
concern.

**Serving model:** the runner prefetches the whole `ClientTest` once and renders
**one item at a time client-side**. The per-item `GET /exam/attempts/{id}/items/{item_id}`
endpoint supports *true* lazy server-side serving (a stricter anti-dump runner that
never ships the full test to the network tab) — kept on the surface for that future
runner even though the current `exam.js` doesn't call it.

**Known gap (deferred to wire-up):** stimulus assets (audio mp3 / image options) are
referenced by the runner as `/assets/{asset_id}`, but **no asset-serving route exists
yet** — text items work end-to-end; listening/image sections need that route. It is
intentionally out of the Phase 11 gate and can't resolve real assets until ingestion
stores them with `asset_id`s (MinIO/`FilesystemStorage`), so it lands with the
seed/wire-up phase, not here.

## Tests
`tests/test_delivery_*.py` — served payload has no `correct`; window/grace
enforced; refresh resumes the same attempt; displayed->canonical round-trip; late
submit rejected; timer expiry. `tests/test_delivery_api.py` covers the HTTP surface
(keyless serve, full flow, resume, server-side expiry + 409, 404s).
