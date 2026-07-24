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

## Key invariants
- **Compute once.** Grading + feedback run at submit (and on an explicit teacher
  re-grade), not on dashboard reads. `upsert_report` makes a re-grade idempotent.
- **Own transaction.** `finalize_attempt` opens its own `session_scope` (the request
  session is gone by the time the background task runs) and commits.
- **Gemini optional.** Grader/analyst are built only when `GEMINI_API_KEY` is set;
  otherwise writing -> manual review and the report is scores-only.

## Layout
- `service.py` — `ResultsService.finalize(attempt_id)` + `ResultsError`.
- `finalize.py` — `build_results_service` factory + the `finalize_attempt` background
  entrypoint (wires the Gemini grader/analyst from settings, own session).
- `__init__.py` — public surface.

## Tests
`tests/test_results.py` — finalize persists a report (grade + skills); re-finalize
overwrites (idempotent); a failed feedback LLM still saves the score with
status="failed"; the report round-trips through the repository.
