# CLAUDE.md — results engine

## Responsibility
**Finalize an attempt exactly once.** When an attempt is submitted, this engine
grades it (deterministic objective items + the Gemini writing grader), builds its
per-skill pedagogical feedback, then computes the integrity profile + advisory
cheating verdict ONCE (Gemini analyst, or the deterministic-only fallback) and
persists a single frozen `AttemptReport` holding both axes side by side. The
teacher dashboard reads that report instead of re-running the LLM on every load —
this is the fix for the "dashboard is slow" problem (no per-load LLM/grading).

## Inputs / Outputs
- **In:** an `attempt_id`. Reads the `Attempt`, its answers, and the authoring `Test`
  via the repositories; composes `GradingService` + `FeedbackService`.
- **Out:** a persisted `AttemptReport` (`attempt_reports` table) — frozen
  `GradingResult` + `list[SkillReport]` + summary + `model_id` + `status` +
  `integrity` (`IntegrityProfile` the verdict was built from) + `verdict`
  (`AnalysisVerdict`, advisory).

## Read API for the dashboard (integrator)
Stored once at finalize / regrade — reads never call an LLM or recompute:
- `ResultsService.get_verdict(attempt_id) -> AnalysisVerdict | None`
- `ResultsService.get_integrity(attempt_id) -> IntegrityProfile | None`
- `AttemptRepository.get_verdict(attempt_id) -> AnalysisVerdict | None` (column read)
- `AttemptRepository.get_report(attempt_id).verdict / .integrity`, and
  `get_reports_for_test(test_id) -> {attempt_id: AttemptReport}` for the list view.
None = no report yet, or a report written before migration `e5f6a7b8c9d0` (a
regrade fills it). `AnalysisVerdict`: `suspicion_score`, `confidence`, `flags[]`,
`summary` (Russian for the deterministic/Gemini paths), `model_id` (None =
deterministic-only), `writing[]` = `{item_id, ai_assist_likelihood, signals[],
rationale}`, `error` (LLM failure note). `IntegrityProfile.writing[]` =
`WritingFeatures` (typed/non-keyboard/unexplained chars, paste attempts, away time,
text stats, `flags[]` — see app/integrity/CLAUDE.md). Show the raw replay next to
it (rule #6); the verdict never feeds a score (rule #2). The teacher dashboard
reads exactly this: `app/admin` takes `report.verdict` / `report.integrity` from
`get_reports_for_test` (list, CSV) and `get_report` (detail) and never calls an
analyst on a read; only an attempt with NO stored verdict gets the deterministic
formula computed for display in the detail (not persisted — a regrade stores it).

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
- **Gemini optional.** Grader/feedback analyst/integrity analyst are built only when
  `GEMINI_API_KEY` is set; otherwise writing -> manual review, the report is
  scores-only and the verdict is deterministic-only (`model_id=None`).
- **Integrity axis never fails or moves the grade.** `_assess` runs AFTER grading
  + feedback; `AnalysisService` already degrades an analyst error to the
  deterministic verdict + `error`, and any other analysis crash is caught here and
  logged at ERROR with **no verdict stored** (`verdict=None`, `integrity=None`) —
  never a made-up "suspicion 0.0 / confidence 0.0" row, which would rank as a
  scored clean attempt. The dashboard shows "—" for a missing verdict (list / CSV)
  and computes the live deterministic fallback in the detail; a regrade fills it.
  The report stays `complete` with its grade. `record_failure` stores a
  deterministic (no-LLM) verdict best-effort (None when even that fails).
  The analyst gets `analysis_context(test, grading, answers)`:
  level, open_writing tasks + submitted texts (blank optional tasks skipped) and the
  objective % (single_choice/gap_fill/matching awarded / max) as opaque
  level-mismatch input — nothing flows back into a score.
- **One analyst source.** `app/results/analyst.py: default_analysis_llm()` (Gemini
  from settings, lru-cached) is used by both `finalize._service` and a
  `ResultsService` constructed without `analysis_llm` (the admin layer's regrade
  service: the `FROM_SETTINGS` sentinel). `tests/conftest.py` stubs it to None so no
  test calls the real API. Late telemetry (after submit) is only picked up by a
  regrade; the runner flushes + drains telemetry before submitting to avoid that.

- **The LLM verdict is paid for once per input (`input_fingerprint`).** Every
  stored verdict carries `input_fingerprint = "v<ver>:<event count>:<last event
  id>:<sha256 of level + objective % + writing tasks/texts, 16 hex>"`, read BEFORE
  the analysis runs (an event landing mid-analysis makes it stale, never falsely
  fresh). A `ResultsService` built with `reuse_verdict=True` — `finalize._service`,
  i.e. the submit/expiry background task, the sweep and "grade all missing" —
  reuses the stored (profile, verdict) when the verdict is an LLM one (`model_id`
  set, no `error`) and the fingerprint is unchanged: a retry caused by a failed
  FEEDBACK call no longer pays for the integrity analysis again. The default
  (`reuse_verdict=False`: the admin layer's regrade service) always recomputes.
  Lives inside the verdict JSON — no migration. Bump `FINGERPRINT_VERSION` when the
  prompt / inputs change incompatibly.
- **Automatic retries of a failed report are capped (`MAX_AUTO_RETRIES` = 3).** A
  `failed` report written by the n-th automatic (sweep) retry ends its `error`
  with ` [auto-retry n]` (`with_auto_retry` / `auto_retry_count`; in the error
  text, so no migration, and the teacher sees it). `finalize_attempt(...,
  max_auto_retries=3)` — only the sweep passes it — skips an attempt whose current
  failed report already records 3 (INFO; a WARNING when the 3rd fails). The
  teacher's "grade all missing" and single regrade have no cap, and a failure there
  writes no marker (the budget starts again). A failed report older than the
  attempt's latest finish (re-open + re-submit) does not count.
- **No credential in a log or a report.** `describe_error` scrubs every exception
  string (`redact.py`: `key=<...>`, a dumped `x-goog-api-key`, Google-key shapes and
  the configured secrets) before it is logged or stored in `AttemptReport.error`;
  crash logs use `_log_failure` (scrubbed traceback) instead of `log.exception`.
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

- **Partial assignments.** `_finalize_locked` works on `served_test(test,
  attempt.papers)`: the feedback skills and the analysis context cover the served
  papers only (grading restricts itself the same way). Finalize / sweep / expiry are
  otherwise unchanged.

## Layout
- `service.py` — `ResultsService.finalize(attempt_id)` / `record_failure` /
  `list_missing` (read-only candidate selection) / `get_verdict` / `get_integrity`
  / `analysis_context`, `describe_error`, `auto_retry_count` / `with_auto_retry`,
  `MAX_AUTO_RETRIES`, `ResultsError`.
- `redact.py` — `redact_secrets(text)` / `redacted_traceback(exc)`.
- `analyst.py` — `default_analysis_llm()` (the settings-built Gemini analyst).
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
`tests/test_results_verdict.py` — finalize persists integrity + verdict
(deterministic by default) and the read API returns them; the analyst receives level
/ writing texts / objective % and the grade is identical with or without it; a
failing analyst still yields a complete grade + deterministic verdict with `error`;
a crashing analysis layer still saves the grade (no verdict); regrade recomputes;
record_failure stores a deterministic verdict; blank optional writing is not sent.
`tests/test_results_hardening.py` — the stored `error` and every log line (incl. a
crashed background finalize's traceback) carry no API key; an analysis crash stores
`verdict=None`; the automatic path reuses the stored LLM verdict until the events
or the context change, the explicit regrade always recomputes, a failed /
deterministic verdict is never reused, a sweep retry of a failed feedback call
makes no analyst call; the sweep stops after 3 automatic retries (marker in
`error`), the button / regrade still run and reset the budget, a new finish too.
