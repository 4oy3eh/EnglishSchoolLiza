# CLAUDE.md — admin (teacher) engine

## Responsibility
The **teacher-facing composition root** and dashboard backend. It is the one engine
allowed to hold grading *and* integrity/analysis at once, because the dashboard
shows them side by side. It owns:

- **bank management** — list / get / delete authored tests.
- **review queue** — list ingested `draft` tests and **approve** them. This is the
  single `draft -> published` human-approval gate (golden rule #5); approval is the
  ONLY way content goes live. Phase 9 ingestion only ever produces drafts.
- **roster** — add students; live roster status (assigned / in-progress / submitted).
- **results** — per-attempt score + advisory verdict + raw replay; per-test list
  ranked most-suspicious-first.
- **auth** — a single shared teacher password mints a short-lived HMAC-signed bearer
  token; every admin route requires it.

## Inputs / Outputs
- **In:** an authenticated teacher (bearer token); `test_id` / `attempt_id` /
  roster names. Reads the item bank, roster, attempts, and the append-only event
  stream through the existing repositories/services.
- **Out:** `Test` / `RosterEntry` contracts, and the admin view DTOs in `models.py`
  (`ReviewDraft`, `RosterStatus`, `AttemptOverview`, `AttemptResult`). The verdict is
  always presented next to the deterministic profile and the raw events (rule #6).

## Must NOT
- **Let a cheating signal change a score (golden rule #2).** Grading and
  integrity/analysis are computed on independent paths and only *bundled*;
  "rank suspicious-first" reorders rows, it never edits a score.
- **Auto-publish (golden rule #5).** `approve` only flips an existing `draft`; it
  never invents content and refuses anything not currently a draft.
- **Send an answer key to a student.** This is a teacher surface; it serves
  authoring `Test`s for review, never to the runner.
- **Add a persisted table / migration.** The view models are read-side DTOs (like
  `app/ingestion/models.py`), not contracts — no schema change (rule #4).

## Key invariants
- **Approval is the publish gate.** `approve(test_id)` → `ContentService.publish` →
  `draft -> published`, logged loudly. The only state transition to `published`.
- **Auth is enforced.** No admin route resolves without a valid, unexpired token;
  signature check is constant-time and the secret never leaves the server.
- **Log every admin action** at INFO (auditability): approve, delete, add_student,
  results reads.

## Layout
- `auth.py` — `TokenSigner` (mint/verify HMAC bearer token) + `verify_password` +
  `AuthError`. `fastapi-users` is the documented upgrade path for multi-teacher.
- `models.py` — read-side DTOs (`ReviewDraft`, `RosterStatus`, `AttemptOverview`,
  `AttemptResult`). Compose contracts; persist nothing.
- `service.py` — `AdminService` (bank / review / roster / results) + `AdminError` +
  `build_admin_service` factory (wires every composed engine from a session).
- The HTTP surface lives in `apps/api/admin.py`; the dashboard in `apps/web`.

## Tests
`tests/test_admin.py` — auth enforced (401 without/with bad/expired token; login →
token works); approve flips draft→published and is the only publish path; results
endpoint returns score + verdict + event count; ranked suspicious-first; admin
actions logged.
