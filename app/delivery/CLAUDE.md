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

## Tests
`tests/test_delivery_*.py` — served payload has no `correct`; window/grace
enforced; refresh resumes the same attempt; displayed->canonical round-trip; late
submit rejected; timer expiry.
