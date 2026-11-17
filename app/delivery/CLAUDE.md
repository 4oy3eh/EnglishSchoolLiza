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

## Student access: classroom PIN + session token (Phase 14)
- **Who may start.** `POST /exam/roster/{id}/start` takes `{"pin": "…"}` and runs
  `DeliveryService.authenticate` BEFORE `start`: unknown entry 404; empty / not
  PIN-shaped (`normalize_pin` drops whitespace + upper-cases, then 4 chars of
  `contracts.runtime.PIN_ALPHABET`) → `PinRequiredError` 400, never counted; locked →
  `PinLockedError` 429 + `Retry-After` (the PIN is not even compared); wrong →
  `WrongPinError` 403 + `X-Pin-Tries-Left`; the failure that reaches the limit answers
  429. Compare is `hmac.compare_digest` (`app/content/roster.pin_matches`). A RESUME
  needs the PIN too — or the runner skips `/start` and uses its stored token.
- **Session token.** A successful start (new or resume) calls `open_session`: 256-bit
  `secrets.token_urlsafe`, only its SHA-256 stored (`attempt_sessions`, via
  `AttemptRepository.add_attempt_session`). Several sessions per attempt may coexist
  (the PIN typed on a second device does not kick the first one out). Every
  `/exam/attempts/{id}/...` route depends on `require_attempt_token`
  (`check_session`): unknown attempt 404 (unchanged), missing / wrong / another
  attempt's token 401 (`SessionTokenError`, WARNING "session REJECTED"). The telemetry
  ingest checks the same table itself (`AttemptRepository.attempt_session_valid`, no
  delivery import) token-FIRST, so an unknown attempt is the same 401 as a bad token
  (the ingest never confirms an attempt id): header `X-Attempt-Token`, or `?token=`
  because `sendBeacon` cannot set headers — `apps/api/telemetry.py` masks `token=` in uvicorn's access log; 401
  drops the batch (the recorder treats it like any non-retryable 4xx).
- **Lifetime.** Tokens do not expire on their own (an attempt lives ≤ its deadline +
  reopen). Teacher **reset** deletes the attempt and its sessions. Teacher **new PIN**
  (`RosterService.regenerate_pin`) only changes FUTURE starts: open sessions keep
  working, so a student mid-test is never thrown out by a reprint; to stop a session,
  reset the attempt. The new PIN also lifts that entry's wrong-PIN lock — AFTER the
  new PIN is committed (`apps/api/admin.regenerate_pin` commits, then
  `reset_entry`), so the old PIN is never unlocked while still valid.
- **Why the PIN is stored in plaintext.** It is a classroom PIN, not a password: the
  teacher must read it back to print the cards / tell a student who lost theirs, and a
  hash of a 4-char code from a 31-letter alphabet (923 521 values) would be reversed
  instantly anyway. Its protection is the online rate limit plus "the share link only
  lists names". It is shown only on teacher routes (`/admin/...` roster, add, PIN
  cards), never in `/exam/tests/{id}/roster`, never in the results CSV, never logged.
- **Wrong-PIN guard (`access.PinRateLimiter`, process-wide `access.get_pin_limiter()`;
  admin and delivery both use the getter, admin never imports `apps/api/delivery`).**
  Golden rule #3 drives the design: a refused student's timer keeps running, and a
  whole class usually shares ONE address (school NAT; behind a tunnel without
  `--proxy-headers` everyone is 127.0.0.1), so typos must never lock an IP.
  * **Per student** (the real per-target guard): 5 wrong within 5 min → that entry
    locked 5 min (even the right PIN). A right PIN clears it.
  * **Per IP = spray throttle**, keyed on `request.client.host` (never a raw
    `X-Forwarded-For`; see admin CLAUDE.md "Client IP"): counts DISTINCT students the
    IP failed on that stay *unresolved* for > 120 s (within 10 min). A right PIN for X
    from that IP resolves X and forgives the oldest other unresolved student once per
    student per window (wrong-name tries). ≥ 5 such students → every PIN from the IP
    refused for 30 s, doubling per strike to 300 s; tally restarts after a strike.
    Thresholds sized by simulation (30 children, 1-3 typos each, all starting within
    20-180 s, wrong-name tries, 2 children stuck 3 min: never throttled); rationale in
    the `access.py` module doc.
  * **Per test = monitor, never a lock** (a per-test lock would let anyone with the
    link DoS the class): wrong PINs per test in a 10-min sliding window; ≥ 40 → one
    WARNING `PIN ALERT` (re-armed when it drops) and `test_activity` feeds
    `GET /admin/tests/{id}/pin-activity` (counts per student, locked students,
    throttled IPs — never PINs) → the dashboard banner «Много неверных PIN…».
  * **Teacher remedies:** «Снять блокировки» (`POST /admin/tests/{id}/pin-locks/clear`
    → `clear_test`: every student lock of the test + every IP that failed on it; INFO
    log) and «Сгенерировать новый PIN» (`reset_entry`, after the commit).
  * **Logs:** each lock event its own WARNING from `access.py` (`PIN LOCK student`,
    `PIN THROTTLE ip`, `PIN ALERT test`), emitted after the limiter lock is released;
    the service adds one WARNING per refused / wrong request. PINs are never logged.
  * **Known residual risk:** anyone holding the share link can type 5 wrong PINs on a
    classmate's name and lock that ONE student for 5 min (and a sprayer walking the
    whole roster can lock several before the IP throttle). Remedy = the banner +
    «Снять блокировки» (documented for the teacher in docs/OPERATIONS.md §3).
  Check + compare + count are one step under a lock. **In-memory, single uvicorn
  worker** (like the login limiter): a restart forgets locks; a multi-worker deploy
  would need Redis. Tests reset it in `conftest.py` (`app.delivery.access.PIN_LIMITER`).
- **Share link = published tests only.** `/exam/tests/{id}/info` and `/roster` answer
  404 for a draft exactly as for an unknown id (`_published_test_or_404`).
- **Runner (`apps/web/exam.*`).** Name → `#pinScreen` (one 4-char field, auto
  upper-case, English errors incl. tries left / lock, «This is not me») → `/start` →
  `exam:active` = `{testId, attemptId, rosterEntryId, name, token}`. `api()` adds the
  header to every `/attempts/` call; a 401 there drops the token and reloads into the
  PIN screen ("continue"), and the persisted save queue is resent after it. The old
  "are you really …?" `confirm()` is gone — the PIN is the identity check.

## UI language for YLE tests (Phase 14)
The runner is English (it mirrors the Cambridge exam). `GET /exam/tests/{id}/info`
(title + level, no content) lets the start screen decide: for `YLE_LEVELS`
(`A1_MOVERS`; Starters / Flyers codes pre-listed) it shows EN / УКР / РУС, remembered
in `localStorage["exam:lang"]`, default EN; every other level is English with no
switcher, whatever is stored. `I18N` in `exam.js` holds every UI string (start / PIN
screens, buttons, navigator, review, save indicator, timer warnings, audio
instructions, alt-text templates, confirms, done screen, the monitoring notice) in
`en` / `uk` / `ru` (a test checks the three key sets match); `t()` looks them up and
`applyStaticI18n()` fills `[data-i18n]` / `[data-i18n-aria]` + `<html lang>`. **Never
translate test content** — stimuli, prompts, options, part titles, test title are
rendered verbatim. New UI text must go through `t()` with all three languages.

## Layout
- `access.py` — `PinRateLimiter` / `PinDecision` / `PinActivity`, the process-wide
  `PIN_LIMITER` + `get_pin_limiter()`, `new_session_token`, `TOKEN_HEADER`.
- `projection.py` — pure authoring -> `Client*` projection (strips the answer key,
  applies option shuffle). The enforcement point for golden rule #1.
- `service.py` — `DeliveryService` (lifecycle), `ExamWindow`, `AttemptState`, and
  the `DeliveryError` hierarchy. `bank_builder` is injectable; the default emits
  one singleton pool per section (deliver the whole authored test + option
  shuffle), so callers with alternative passages can opt into section divergence.

## HTTP surface (Phase 11)
`apps/api/delivery.py` exposes this service to the student runner (`apps/web/exam.*`)
under `/exam`, behind the per-test share link (students are identified by
`roster_entry_id` + their classroom PIN, then a per-attempt session token — see
"Student access" above): roster pick, start (create-or-resume), keyless
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

**Runner submit flow + warnings (apps/web/exam.*).** The final submit goes through
a review panel (`#review`, opened by "Review & submit" on the last part or in the
navigator): per part answered / unanswered (navigator data), unsaved / rejected
answers from the save queue (live), time left, and "Go to question" jumps back into
the runner. Its `#submitBtn` keeps the old confirms (unanswered; then, after
draining the save queue, unsaved / rejected) before `POST /submit`. The deadline
auto-submit runs from the timer tick and never waits for the panel (rule #3).
Display-only warnings at 10 and 2 minutes left (`#timeWarn` + an assertive
`#timeWarnLive` region); the save indicator is a polite live region; controls are
labelled by "Question N" + prompt, pictures without alt get one from the prompt /
part title. Tests: `tests/test_exam_review_e2e.py` (Playwright, fake page clock).

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
`app.results.finalize_attempt`, autouse in that file). Phase 14 there: the roster list
never carries PINs; `/info`; start without / malformed (not counted) / wrong PIN;
case + spaces; lockout per entry (fake clock, right PIN refused while locked, 5 min
later fine); a class of 30 on one IP, every child mistyping in waves, all get in; an
IP spraying many names is throttled (429 "from this network", then fine); drafts 404
on `/info` + `/roster`; resume needs the PIN and opens a second valid session; every
attempt endpoint × {no, empty, wrong, another attempt's} token → 401 with nothing
changed; WARNING logs; token stored hashed. `tests/test_roster_pin.py`: PIN alphabet /
generation / uniqueness / unique index, normalize + compare, limiter (seeded class
simulation never throttled, spray throttle + doubling, resolve / forgive-once,
per-test monitor alert once and never locks, `clear_test`, atomic under threads,
stale keys dropped), teacher `pin-activity` / `pin-locks/clear` (auth, counts, no
PINs, INFO log), regenerate lifts the lock only after the commit, the migration on a scratch DB (backfill, downgrade -1,
upgrade), teacher PIN view / regenerate (old PIN 403, open session kept, lock lifted),
no PIN in the CSV, reset drops sessions. `tests/test_telemetry.py`: ingest 401 without /
with a wrong / another attempt's token, unknown attempt = the same 401, `?token=` accepted, access-log masking.
`tests/test_pin_runner_e2e.py` (Playwright): PIN screen messages, start, reload resume
without PIN, lost / forged token → PIN screen → same attempt, every attempt + events
request carries the token, lockout message, Movers language switch (chrome changes,
content identical, remembered) + the i18n key-set check. All other E2Es start through
the PIN (`tests/pin_helpers.py`).
