# CLAUDE.md — results engine

## Responsibility
**Finalize an attempt exactly once.** When an attempt is submitted, this engine
grades it (deterministic objective items + the Gemini writing grader) and builds its
per-skill pedagogical feedback, then persists a single frozen `AttemptReport`. The
teacher dashboard reads that report instead of re-running the LLM on every load —
this is the fix for the "dashboard is slow" problem (no per-load LLM/grading).

## Inputs / Outputs
- **In:** an `attempt_id`. Reads the `Attempt`, its answers, and the authoring `Test`
  via the repositories; composes `GradingService` + `FeedbackService`.
- **Out:** a persisted `AttemptReport` (`attempt_reports` table) — frozen
  `GradingResult` + `list[SkillReport]` + summary + `model_id` + `status`.

## Must NOT
- **Be called from inside `DeliveryService`** (its boundary forbids grading). The
  trigger is the delivery *API* submit endpoint, as a background task.
- **Cross the grading/integrity boundary** (golden rule #2): grading never reads
  integrity; feedback never reads a cheating signal; a feedback verdict never moves a
  score (the report's scores come straight from the `GradingResult`).
- **Crash the worker.** `finalize_attempt` (the background entrypoint) logs and
  swallows; a failed feedback LLM is caught in the service (status="failed", score
  still saved) so the teacher can re-grade.
- **Fail silently.** A finalize that blows up is *persisted* as a `failed` report
  carrying `error` (`record_failure`: deterministic grade if possible, else a zero
  grade) — an attempt is never left `submitted` with no row.

## Key invariants
- **Compute once.** Grading + feedback run at submit (and on an explicit teacher
  re-grade), not on dashboard reads. `upsert_report` makes a re-grade idempotent.
- **Own transaction.** `finalize_attempt` opens its own `session_scope` (the request
  session is gone by the time the background task runs) and commits.
- **Gemini optional.** Grader/analyst are built only when `GEMINI_API_KEY` is set;
  otherwise writing -> manual review and the report is scores-only.

- **One finalize per attempt at a time.** `finalize` / `record_failure` hold the
  per-attempt lock from `locks.py`, so a teacher regrade racing the submit-time
  background finalize waits and then overwrites. In-process only (single uvicorn
  worker); a multi-worker deployment needs a DB/Redis lock instead.
- **In flight is visible.** `locks.is_locked(attempt_id)` is true while a finalize
  holds the lock; the dashboard shows "pending" (never "missing") and the recovery
  loop skips the attempt (fast path only — see the next bullet).
- **Never pay twice: "already done?" is decided INSIDE the lock.** `finalize_attempt`
  holds the per-attempt lock for the whole run (finalize AND `record_failure`), and
  only after acquiring it opens its session and re-reads the report through
  `AttemptRepository.get_report_marker` (plain column SELECT, never the identity
  map). It returns False and logs `finalize attempt=... skipped: ...` at INFO when:
  `skip_if_complete` (default: the submit/expiry background task, the sweep, the
  button) and a `complete` report at least as new as the attempt's finish exists
  (a report older than the finish — re-open + re-submit — is stale and re-run); or
  `skip_failed_since` (the sweep: now - `FAILED_RETRY_AFTER`) and a `failed` report
  written at/after it. This closes the stale-candidate-list windows and the
  non-atomic `is_locked`-then-acquire. The teacher's single-attempt regrade
  (`AdminService.regrade` -> `ResultsService.finalize`) bypasses it and always re-runs.
- **The sweep leaves fresh finishes to their background task.** `sweep_missing_reports`
  passes `min_age=SWEEP_MIN_AGE` (60 s): an attempt with no report whose
  `submitted_at` (or `deadline`, when expired) is younger is skipped (INFO) — the
  submit/lazy-expiry request scheduled its finalize. Attempts the sweep expired itself
  have no such task and are exempt. The teacher's button has no age filter.
- **Recoverable, one transaction per attempt.** `finalize.finalize_missing(test_id |
  None, failed_before=None)` reads the candidate ids (`ResultsService.list_missing`)
  in a short read session, then runs `finalize_attempt(id)` — its OWN session — per
  id. Never several attempts' LLM calls inside one session: an open SQLite write
  transaction would block every student's answer save meanwhile. Used by the
  teacher's "Дооценить все" (`POST /admin/tests/{id}/finalize-missing`, retries
  every failed/missing attempt) and by `sweep_missing_reports()` (startup hook):
  the sweep takes attempts with NO report plus `failed` reports older than
  `FAILED_RETRY_AFTER` (10 min); younger failures are skipped and logged at INFO.
- **Abandoned attempts are expired first.** `finalize_missing(..., expire_overdue=True)`
  (used by the sweep and by the admin default runner, scoped to the test there)
  first calls `expire_overdue_attempts(test_id)`: its OWN short `session_scope`
  constructs `DeliveryService` exactly as `apps/api/delivery.py` does and runs
  `expire_overdue(now)`, committing before any finalize, so an attempt left
  `in_progress` past its deadline (tab closed for good) becomes a normal candidate
  in the same run. Never raises (a failed expiry must not block the recovery of
  already-finished attempts). Dependency direction: results -> delivery is allowed
  (results already composes grading + feedback the same way); delivery never imports
  results — the delivery *API* schedules `finalize_attempt`, also on a lazy expiry.
- **Periodic recovery.** `apps/api/main.py` runs `sweep_missing_reports` 5 s after
  startup (`RECOVERY_INITIAL_DELAY_S`, interruptible) and every
  `RECOVERY_SWEEP_INTERVAL_S` (default 300) on a daemon thread stopped by a
  `threading.Event` at shutdown; `SKIP_STARTUP_SWEEP=1` disables it. The lock and the
  cooldown above are what keep the repeated runs from duplicating paid LLM work.

## Layout
- `service.py` — `ResultsService.finalize(attempt_id)` / `record_failure` /
  `list_missing` (read-only candidate selection), `describe_error`, `ResultsError`.
- `finalize.py` — `build_results_service` factory, the `finalize_attempt` background
  entrypoint (own session; persists a `failed` report on a crash),
  `expire_overdue_attempts` (delivery expiry, own short txn), `finalize_missing`
  (per-attempt transactions) and `sweep_missing_reports`.
- `locks.py` — per-attempt `threading.RLock` registry (`attempt_lock`, `lock_for`,
  `is_locked`).
- `__init__.py` — public surface.

## Tests
`tests/test_results.py` — finalize persists a report (grade + skills); re-finalize
overwrites (idempotent); a failed feedback LLM still saves the score with
status="failed" + `error`; `record_failure` writes a failed row (deterministic or
zero grade); `finalize_missing` picks only missing/failed reports, runs one
sequential transaction per attempt (a failure on the first doesn't stop the second)
and skips in-flight attempts; the in-lock "already done?" re-check (background task
skips a complete report but re-runs a stale one; a report completed / failed after
the candidate read is skipped by the sweep, the button retries the failure; the sweep
leaves < 60 s finishes alone; a background finalize holding the lock mid-LLM while
the sweep waits on it -> exactly one LLM call, real per-thread sessions on a file DB); the background entrypoint persists its failure; the
sweep recovers and retries `failed` only after the 10-min cooldown; a concurrent
finalize is serialized by the attempt lock; the sweep expires an abandoned
past-deadline attempt (own txn, before the finalizes) and leaves it `expired` with a
complete report; a failing expiry step never blocks the rest. Admin-side coverage
(status chip, endpoint, per-attempt txns, abandoned attempts recovered by the
button, explicit regrade always re-runs) lives in `tests/test_admin.py`; the periodic
loop (5 s initial delay interruptible by stop, start/stop, survives a failing sweep,
`SKIP_STARTUP_SWEEP`, lock + cooldown respected) in `tests/test_recovery_loop.py`.
`tests/conftest.py` sets `SKIP_STARTUP_SWEEP=1` for every test (autouse), so no test
can start the real loop against the developer's DB; loop tests override it.
