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
(`PRE_A1_STARTERS`, `A1_MOVERS`, `A2_FLYERS`) it shows EN / УКР / РУС, remembered
in `localStorage["exam:lang"]`, default EN; every other level is English with no
switcher, whatever is stored. `I18N` in `exam.js` holds every UI string (start / PIN
screens, buttons, navigator, review, save indicator, timer warnings, audio
instructions, alt-text templates, confirms, done screen, the monitoring notice) in
`en` / `uk` / `ru` (a test checks the three key sets match); `t()` looks them up and
`applyStaticI18n()` fills `[data-i18n]` / `[data-i18n-aria]` + `<html lang>`. **Never
translate test content** — stimuli, prompts, options, part titles, test title are
rendered verbatim. New UI text must go through `t()` with all three languages.

## B2 / C1 / C2 payload for the runner (contract: `contracts/content.py`)
The runner work for these is mechanical; the server already serves (all keyless):
- **`key_word_transformation`** (`ClientKeyWordTransformationItem`):
  `{"item_type": "key_word_transformation", "id", "prompt" (the lead-in sentence),
  "key_word" (e.g. "IDEA" — show in bold capitals), "gapped_sentence" (exactly ONE gap:
  split at the first match of `contracts.content.KWT_GAP_PATTERN` =
  `_{3,}|\.{3,}|…+` and put one text input there), "min_words", "max_words" (resolved
  from the level: B2 2–5, C1 3–6, C2 3–8, or the item's override)}`. Rubric line:
  "Use between {min} and {max} words, including the word given." Save the typed words
  as a plain string (`POST /answers` with a str response, like gap_fill). No answer
  data on the wire (`answers` / `part_a` / `part_b` never served — golden rule #1,
  `tests/test_no_correct_invariant.py`).
- **gap_fill** gains optional `stem_word` (word formation: show "(COMPETE)" next to the
  gap, capitals) and `max_words` (show "one word only" / "no more than N words").
- **matching_pool** stimulus gains `each_option_once` (true = gapped text: grey out /
  mark an option once any question of the part uses it; false = reusable multiple
  matching) and optional `audio` (an `AudioAssetStimulus`: this matching part plays that
  recording). `audio.listening_tracks` / `section_audio` treat it exactly like an
  `audio_asset` stimulus: consecutive listening sections with the same `audio.asset_id`
  are ONE track (C1/C2 Listening Part 4: Task 1 and Task 2 over one recording, played
  once for both), a different id starts the next track. The runner's `buildModel`
  must mirror this (today it only looks at `st.kind === "audio_asset"`; until then such
  a part simply continues the previous track) — `/audio-progress` already accepts
  the pool's asset id. Section `reported_skill` is teacher-side only (not served).
- Levels `B2_FIRST` / `C1_ADVANCED` / `C2_PROFICIENCY`: English UI (no switcher —
  `YLE_LEVELS` only). Reading & Use of English parts are all `skill="reading"`, so the
  runner keeps one block with continuous question numbers.

## IQ family payload (Phase 17.1 — contracts in `contracts/content.py`)
What `projection.py` already serves (all keyless; `tests/test_iq_family.py`,
`tests/test_no_correct_invariant.py`):
- **`gap_fill`** may carry `image` (an asset id, None for every older item): show the
  picture above the gap (IQ Test 1 recall: one symbol per item, prompt "Number for
  this symbol").
- **`mark_toggle`** (`ClientMarkToggleItem`): `{id, prompt, image}` — no `correct`.
  Save `"1"` (marked) / `"0"` (unmarked) as a plain string.
- **`drawing`** (`ClientDrawingItem`): `{id, prompt, asset_id, mode: "region" |
  "lines", square}` — no `target`. `square` (lines only) is where the square sits on
  the picture in normalized coords, for framing the canvas. Save the strokes as a
  JSON string `{"w": canvasWidthPx, "h": canvasHeightPx, "strokes": [[[x, y], ...],
  ...]}` with the canvas covering the picture (grader caps: ≤ 300 strokes, ≤ 5 000
  points, ≤ 300 000 chars — keep the runner well under them, e.g. drop points closer
  than 2 px). The char cap (`contracts.content.DRAWING_MAX_RESPONSE_CHARS`) is also
  enforced on save — see "Answer size caps".
- **Section extras** on `ClientSection`: `time_limit_seconds` (None = shares the
  test time), `item_count` (also while `items_withheld` — the intro's Ready screen),
  `intro` (`SectionIntro`: text, images, samples, `practice` — untimed, served as
  authored) and `study` = `ClientStudyInfo {study_seconds}` only.
- **Intro practice (17b)** = `IntroPractice {prompt, images, kind: text | choice |
  mark | drawing, options [{key, text}], solution, solution_images, explanation}`:
  the booklet's practice problems, answered + checked on the intro's Example screen.
  NOT scored items — never graded, never saved (no `save()`, no PUT). Their
  `solution` is the booklet's PUBLIC printed answer, so it is served; golden rule #1
  still holds for scored items (`tests/test_no_correct_invariant.py` pins
  `solution` / `solution_images` to `IntroPractice` only). Check = case-insensitive,
  spaces collapsed (text), key (choice), "1"/"0" (mark); drawing = a not-checked
  "try it" overlay + "Show answer".
- **`gap_fill.numeric`** (authoring + client, default False): the runner adds
  `inputmode="numeric"` (exam.js `itemHTML`, every runner). Display hint only.
- Level `IQ`: English UI (not in `YLE_LEVELS`); one paper `"iq"` = the whole test.

**Study stimulus contract (implemented in 17.2).** `Section.study` (authoring:
`asset_id` and/or `text`, `study_seconds`; the editor requires the section to have
`time_limit_seconds`) is NEVER part of the bulk `ClientTest` — `project_section`
emits only `ClientStudyInfo`. The content (`projection.project_study` →
`ClientStudyStimulus`) is served only by the begin response and
`GET .../sections/{id}/study`, and only while that section's study window is open
(see "Timed sections" below).
**The study picture is never named by its asset id** (review hardening):
`ClientStudyStimulus.image_url` = `projection.study_asset_url(attempt, section)` =
`GET /exam/attempts/{id}/sections/{sid}/study/asset` — attempt token, only inside the
window (`DeliveryService.section_study_asset`; 409 `study_not_open` before / after,
404 for a text-only study), bytes via `apps.api.assets.private_asset_response` with
`Cache-Control: no-store`. The public `GET /assets/{id}` answers 404 (WARNING) for
every asset referenced by any test's `Section.study`
(`ContentRepository.study_asset_ids`), so the picture cannot be re-fetched during
the recall; do not reuse a study asset as any other stimulus (it would 404 there).
The runner fetches it as a blob with `X-Attempt-Token` (an `<img src>` cannot send
the header), shows an object URL and, the moment the window ends by the server clock,
removes the `<img>` and revokes the URL (`iqDropStudy`).
**Study cells (17b).** `Section.study.images` (asset ids, reading order) = a memory
sheet cut per symbol so it is legible on a phone (the book's T1: 20 symbol + number
boxes). `asset_id` stays supported (old tests); a study needs a picture, cells or
text. Served exactly like the picture, per index: `ClientStudyStimulus.image_urls[i]`
= `projection.study_cell_url` = `GET .../sections/{sid}/study/asset/{i}` (attempt
token, only inside the window, `no-store`, 409 `study_not_open` outside it, 404 for an
index the study does not have — `DeliveryService.section_study_asset(…, index)`);
every cell is in `ContentRepository.study_asset_ids` (public `/assets/` 404) and in
`referenced_asset_ids`; teacher preview `GET /admin/tests/{id}/sections/{sid}/study/asset/{i}`.
The runner fetches all cells in parallel and lays them out as `#iqStudyGrid`: 4
columns below 420 px, 5 below 800 px (phones, tablet portrait), 10 from 800 px (desktop,
phone landscape) — each cell >= 64 px,
all revoked + removed at the window end. Tests: `tests/test_iq_study_cells.py`, the
UX E2E (grid columns / cell size per viewport, blobs, gone after the window). Teacher preview:
`GET /admin/tests/{id}/sections/{sid}/study/asset` (bearer, no-store), loaded as a blob
by `editor.js` "Предпросмотр как ученик".

**Answer size caps.** `PUT …/answers/{item}`: a str over `settings.answer_max_chars`
(`ANSWER_MAX_CHARS`, default 4 000 000 — must stay above any `colour_task` PNG data
URL: 0.5-1.2 M chars for the YLE pictures, more with a browser's weaker PNG) → 422
at the request model (WARNING); then `save_answer` applies the per-type cap
(`DeliveryService.answer_size_limit`: a `drawing` → `DRAWING_MAX_RESPONSE_CHARS`
300 000) → `AnswerTooLargeError` → 413 + `X-Answer-Error: answer_too_large` (WARNING
"answer REJECTED … cap"), nothing saved. Checked after the attempt expiry / item
lookup, before the section phase. The runner treats 413 like 400/404/422: parked as
rejected, never retried.

## Timed sections (IQ family, Phase 17.2 — rules in `sections.py`)
**When.** An attempt runs in "timed sections" mode iff EVERY served section has
`time_limit_seconds` (`plan_sections`). A test where only some sections have one keeps
the plain behaviour (one deadline from `duration_minutes`, no section clocks) and the
start logs a WARNING "section clocks ignored" (the editor refuses such a test since
`editor.section_clock_errors`; this path only covers rows stored before). Every other test is untouched
(regression tests).
**Clock rules (server time, never paused — rule #3).** Sections run strictly in the
served order. Section k becomes current (its untimed intro / samples screen,
`intro_started_at`) when section k-1 closed — the first one at attempt start.
"Begin" starts its clock; an intro left open `settings.section_intro_max_seconds`
(default 420 s since 17b — it covers the runner's three intro screens incl. the
practice problems; was 300 s; `SECTION_INTRO_MAX_SECONDS`,
`DeliveryService(intro_cap_seconds=…)` in tests) begins by itself at
`intro_started_at + cap` (`started_by="intro_cap"`), so nobody can park on an intro
(or read ahead) forever. A section with `study`: begin opens the study window
[`started_at`, `started_at + study_seconds`) — the stimulus is shown, nothing can be
answered — then the answering (recall) phase runs for `time_limit_seconds`
(`time_limit_seconds` = the answering phase; `study_seconds` comes on top — book
Test 1: 60 s study + 60 s recall). Section deadline = begin + study + limit, capped by
the attempt deadline, fixed at begin. It closes at its deadline (`expired`) or when
the student finishes it (`finished`, allowed in study or answering); the next
section's intro starts at that moment. Phase at `now`: no row → `pending`; not begun
→ `intro`; before `study_ends_at` → `study`; not closed → `answering`; else `closed`
(every section counts as closed once the attempt is submitted / expired).
**Overall deadline** (fixed at start): `min(start + Σ(cap + study_seconds +
time_limit_seconds), window hard close)` — `duration_minutes` is not used for such a
test. An abandoned attempt's last section therefore closes exactly at the attempt
deadline; the existing expiry / finalize path then runs unchanged. The server does
not auto-submit when the last section closes — the runner submits (or the attempt
expires at its deadline).
**Storage.** `attempt_sections` (contract `AttemptSection`, migration `f2a3b4c5d6e7`),
one row per (attempt, section) once reached: `intro_started_at`, `started_at`,
`started_by`, `study_started_at`, `study_ends_at`, `answering_started_at` (= begin, or
the study end once observed), `deadline`, `closed_at`, `close_reason`. Lazy and
deterministic: every section call replays the rules (`resolve`, pure) over the
stored rows up to `now` and writes the transitions the clock has already reached
(an abandoned tab's sections close on time) with conditional statements — insert on
conflict do nothing; begin / study end / close only `WHERE … IS NULL` — so two
requests never both own a transition; a lost write re-reads and replays. Writes stop
once the attempt is over. `delete_attempt` (teacher reset / remove) deletes the rows.
**Serving.** The bulk `ClientTest` withholds the items of every section not yet
answering or closed (`items=[]`, `items_withheld=true`; `ClientSection.items` may be
empty only then): no subtest can be read before its clock runs, no recall question
before the study stimulus is gone. The runner fetches a section's items from
`GET .../sections/{id}/items` (answering / closed only) or re-reads `/test`.
`serve_item` follows the same rule. **Answers:** `save_answer` accepts an item only
while its section is `answering`; otherwise `SectionStateError` → 409 with a code
(WARNING "answer REJECTED … code=…"): `section_not_current` (a later section),
`section_not_begun` (intro), `section_study` (recall during study), `section_closed`
(finished), `section_expired` (time ran out). Nothing is saved. The attempt-level
expiry (`AttemptExpiredError`) is checked first, as before.
**Logging.** INFO `section intro` / `section begin … by=student|intro_cap` /
`section study end … -> recall` / `section EXPIRE` / `section close (finished)` —
each transition once (the write that owns it); WARNING for every refused section
action and rejected answer.
**Known limits.** A teacher reopen (with or without extension) does not reopen
closed sections — for a retake use reset. The intro cap is read from settings at
call time while the overall deadline was fixed at start, so change it only between
sessions.

## Papers: a partial assignment (teacher's choice, never the student's)
`RosterEntry.papers` (None = whole test) is copied to `Attempt.papers` at a NEW start
(`_papers_for`: a paper no longer in the edited test is dropped with a WARNING; nothing
left = whole test). Every later call works on `served_test(test, attempt.papers)`
(`contracts.papers`): the projection, `save_answer` / `serve_item` (an unserved item →
404), `/audio-progress` (only served tracks, else 400), saved answers. The deadline is
`min(start + served minutes, hard_close)` — whole test = `duration_minutes`, partial =
the sum of the chosen papers' minutes — fixed at start (rule #3). The layout is built
from the served sections with the same seed: pool draws and option shuffles are keyed
per pool / item, so they equal the whole-test draw. The runner needs no change: it only
ever receives the served sections ("Part 1 of N", navigator, review, tracks).
Start log: `papers=… minutes=… sections=…`. Tests: `tests/test_papers.py`,
`tests/test_papers_e2e.py`, `tests/test_papers_hardening.py`.
**Start vs the teacher's change (no lost update).** `start` first CLAIMS the roster
entry (`AttemptRepository.claim_roster_entry`: conditional UPDATE `attempt_id IS NULL`
→ `in_progress` + the new attempt id), then re-reads the papers (`get_roster_entry(…,
fresh=True)`) and only then builds the attempt. The teacher's `set_roster_papers` has
the same `attempt_id IS NULL` condition, so a change either landed before the claim
(and is served) or answers 409. A lost claim resumes the winner's attempt.

**Known limitation — assets are not attempt-scoped.** `GET /assets/{asset_id}` is
unauthenticated and serves any stored asset by id (except study stimuli, see above) (pre-existing, Phase 12), so the
recording / pictures of a part NOT assigned to a student can still be fetched by
someone who knows its id. Assets never carry answer data (the key lives only in the
authoring test), and ids are not listed anywhere a student sees for an unserved part;
attempt-scoped asset gating is deliberately not built.

**Matching-pool pictures (runner).** `PoolOption.image` → `poolPicHTML`: word-bank chips
(`.wb-chip.has-pic`, 64 px picture above the word, alt = the word), the filled slot
(small picture), letter buttons (`.ol-pic`) and the «Choose from» list; the pool's
`images` render first, above the passage (`contextImagesHTML`). A single_choice with
`shuffle_options=False` is served in authored order (identity `OptionShuffle`).

## Layout
- `access.py` — `PinRateLimiter` / `PinDecision` / `PinActivity`, the process-wide
  `PIN_LIMITER` + `get_pin_limiter()`, `new_session_token`, `TOKEN_HEADER`.
- `projection.py` — pure authoring -> `Client*` projection (strips the answer key,
  applies option shuffle). The enforcement point for golden rule #1.
- `sections.py` — timed sections, pure: `plan_sections`, `overall_seconds`,
  `begun`, `resolve` (replay the clock rules up to `now`), `phase_of`,
  `remaining_seconds`.
- `service.py` — `DeliveryService` (lifecycle), `ExamWindow`, `AttemptState`, the
  timed-section DTOs (`SectionStatus`, `SectionsState`, `SectionBegin`) and the
  `DeliveryError` hierarchy (incl. `SectionStateError` with its `code`). `bank_builder` is injectable; the default emits
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
committed expiry and the background task survive.

**Scheduled exam window.** The window is the test's own schedule (`Test.opens_at` /
`closes_at` / `late_start_grace_minutes`, columns on `tests`, teacher-set on the
dashboard): `ExamWindow.for_test(test)` (unset bound = unbounded, so a test without a
schedule is `ALWAYS_OPEN` and its deadline is `duration_minutes` alone);
`ExamWindow.state(now)` → `open` / `not_yet_open` / `closed` is the same rule `start`
applies. `start(entry, window=None)` derives it from the test; only a NEW attempt is
checked (resume never is); the deadline `min(start + duration, hard_close)` is fixed at
start, so a later window change never moves a running deadline and the recovery sweep is
untouched. `WindowNotOpenError.opens_at` / `WindowClosedError.closed_at`; both log WARNING
"attempt start REFUSED". HTTP: `/start` checks the window BEFORE the PIN for a new attempt
(typos on a closed test are never counted) → 403 + `X-Exam-Window: not_yet_open|closed`
(+ `X-Exam-Opens-At`), which the runner tells apart from a wrong PIN (that 403 carries
`X-Pin-Tries-Left`); `/info` returns `window_state`, `opens_at`, `closes_at`,
`starts_until` (close + grace) and `server_now`. Runner: `#windowNote` above the name list
("Opens: …" / "Closed …" / "Start until …", `fmtWhen` in the UI language), the
`pinWindowNotYet` / `pinWindowClosed` PIN messages, and one timer that re-reads `/info`
at the opening / closing moment (server clock offset from `server_now`). Tests:
`tests/test_exam_window.py`, `tests/test_window_runner_e2e.py`.

**Per-part listening audio.** Each listening section may carry its own
`AudioAssetStimulus` (Cambridge YLE ships one mp3 per part). `audio.listening_tracks`
(mirrored by `exam.js` `buildModel`): inside a listening block a different `asset_id`
starts a new TRACK, the same `asset_id` or a non-audio stimulus continues it, a new
block never continues one — so every older test (one long mp3 repeated on each part)
is still exactly one track. A track's `plays` / `locked` / `preview_asset_id` come from
its first section. Runner model: tracks play strictly in order (a later track cannot
start before the current one ended; the bar of a later part says so + «Go to …»);
navigation never stops a locked track (back, forward, into Reading); the next track
starts by itself when the student is on (or moves to) one of its parts right after the
previous one ended in this page — a track with a sound-check, the first track of a
block, or a resume after a reload needs the Start button (browsers need a gesture);
it never auto-starts while the previous track still has replays (`prev.used < plays`:
the student picks «↻ Play it again» or «Go to the next recording»; the next part's bar
offers the replay too); starting a track forfeits replays left on earlier ones.
**Interruptions never deadlock the order:** a locked track whose resume is refused
after a pause (iOS screen lock / audio interruption: `play()` rejects) or that fires
`error` (404, network drop, decode) is set `playing=false, interrupted, blocked` → the
bar shows «Resume listening» (same play, continues from where it stopped, `used`
unchanged); 2 consecutive load errors (`TRACK_MAX_ERRORS`, reset by `playing`) mark it
`failed` + done («could not be played — tell your teacher», EN/УКР/РУС) so later tracks
stay reachable. Each case → console warning + telemetry `audio_play` with
`payload.note` (`play_refused` / `load_error` / `skipped`; capture only). The `<audio>` elements live in
a hidden `#audioPool` (never in the re-rendered `#audioBar`, whose removal would pause
them). Progress: one row per (attempt, track asset id) in `attempt_audio_progress`
(contract `AudioTrackProgress`, migration `b8c9d0e1f2a3`, atomic upsert with max);
`POST /audio-progress {asset_id, seconds}` (`seconds` strict int 0..86400 — NaN /
huge / negative → 422, never an SQLite overflow) → `{"audio_progress": {…}}` (an asset that
is not a track of the test → `UnknownTrackError` 400, WARNING); `/state` carries
`audio_progress`. Transition: a runner page loaded before the deploy still posts
`{seconds}` → the legacy `attempts.audio_progress_seconds` (old response shape); the
new runner applies that value to the first track — so an open tab survives the deploy.
**Deprecated:** the legacy `{seconds}` path and its column go in the next phase.
Progress logs: one INFO per track when its first point lands, DEBUG afterwards.
After a reload a resumed track that was already fully heard is detected from its
metadata (`probeFinished`). Tests: `tests/test_audio_tracks.py`,
`tests/test_listening_tracks_e2e.py`, `tests/test_listening_interrupt_e2e.py` (refused
resume → Resume button; 404 → Resume → skipped → next track plays; plays=2 never
auto-starts over a replay).

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

**Timed-section endpoints (Phase 17.2, all behind the attempt token).**
`GET /exam/attempts/{id}/sections` → `SectionsState {timed, attempt_status,
server_now, intro_max_seconds, current_section_id, sections: [SectionStatus {phase,
time_limit_seconds, study_seconds, intro_started_at, intro_ends_at, started_at,
started_by, study_ends_at, deadline, closed_at, close_reason, remaining_seconds (of
the current phase)}]}` (`timed=false`, no sections, for any other test; expires a
crossed-deadline attempt + schedules the finalize like `/state`).
`POST …/sections/{sid}/begin` → `SectionBegin {section, study}` (idempotent once
begun; `study` = the stimulus while the window is open). `GET …/sections/{sid}/study`
→ `ClientStudyStimulus` (`image_url`, never an asset id) only inside the window;
`GET …/sections/{sid}/study/asset` → the study picture bytes (`no-store`) only inside
the window. `GET …/sections/{sid}/items` →
the keyless `ClientSection` once answering / closed. `POST …/sections/{sid}/finish`
→ `SectionsState`. Refusals: 409 + header `X-Section-Error: <code>` + body
`{"detail", "code", "section_id"}` (returned after a commit: the transitions the
refusal materialized are true by the clock); codes as above plus `study_not_open`
and `not_timed`. A section action past the attempt deadline answers 409 like a late
save (expiry committed, finalize scheduled). Unknown section → 404.
**Runner (17.5):** `apps/web/exam-iq.js` drives these endpoints (intro → begin → study →
items on one page → finish / expiry → submit after the last section); it asks for
`/sections` only when every served section has `time_limit_seconds`. E2E:
`tests/test_iq_runner_e2e.py`.
**Runner UX (17b).**
- *Intro, step by step* — all client-side inside the server's `intro` phase:
  1 Instructions (title, text, images) → 2 Example (samples + interactive practice;
  skipped when there are neither) → 3 Ready ("N questions · M min S s · about X per
  question", X = limit / `item_count`: one decimal < 3 s, whole seconds < 90 s,
  else minutes; for a study section "First you study for S s, then you answer";
  "The clock does not stop if you leave this page"; Start = `begin`). Back between
  steps; the step is kept per section in `sessionStorage` (a reload returns to it).
  The intro cap's countdown is shown small ("starts by itself in m:ss"); when it
  runs out the server begins the section and the runner follows (`started_by =
  intro_cap`). The header clock shows the part's full length, grey, during the intro.
- *Timer prominence* — `body.iq-mode`: the sticky header carries the part pill +
  title + a big clock with a phase label (Time / Study / Time left); amber at
  <= 30 s, red (pulsing unless reduced motion) at <= 10 s; one non-blocking toast
  "30 seconds left" per subtest (only if it had more than 30 s; `pointer-events:
  none`, also read out); when a subtest closes with `close_reason = "expired"` the
  next intro opens under a "Time is up" interstitial (Continue). Display only — the
  server clock stays authoritative (rule #3).
- *Drawing (owner's decision)* — on the page each drawing is a NON-capturing
  thumbnail (`#dthumb-{id}`: picture + the confirmed strokes; the page scrolls
  freely over it on a phone); a tap opens ONE full-screen overlay (`#drawOverlay`:
  fits the screen keeping the picture's aspect, re-fits on resize / rotation —
  strokes live in picture pixels, nothing is lost; the countdown stays visible;
  Undo, Clear, Confirm = save + close + thumbnail repaint, Close = discard, with a
  confirm() first if something changed). Same overlay on desktop (one code path; a
  bigger canvas is better with a mouse too). 1.5 s before the section deadline
  (and on any phase change) an open overlay is auto-confirmed — the server decides
  whether the save still counts. Phone landscape: picture left, controls in a
  column on the right.
- *Responsive* — no horizontal page scroll at 390x844 / 844x390 / 768x1024 /
  1366x768; touch targets >= 44 px; compact header when the screen is short
  (landscape phone); picture options stay in one row and shrink; study picture
  capped to the screen height (pinch-zoom allowed).
E2E: `tests/test_iq_runner_ux_e2e.py` (steps, practice, Ready numbers, auto-start on
the cap, notice / colours / Time is up, 4 viewports, touch drawing + rotation +
swipe-scroll over a thumbnail).

## Tests
`tests/test_iq_hardening.py` — answer caps (drawing 413 + header + WARNING, general
422, colour-sized data URL accepted), study picture (public 404, `image_url` only,
token, no-store, 409 after the window, text-only 404, teacher route), `IQReport.notes`,
empty norm rows, `resolve` ValueError.
`tests/test_iq_delivery.py` — timed sections: deadline math (+ window cap), non-timed
and partly-timed regressions, strict order + items withheld before begin, begin /
answer / expiry / late answer rejected (WARNING), finish early → next intro, intro
cap, study window (stimulus only inside it, never in the bulk payload, recall
refused during it), abandoned attempt (every clock runs, then the attempt expires),
pure `resolve`, resume after a refresh mid-section, reset drops the rows, the whole
HTTP flow (codes, header, token, no teacher-only data), the migration.
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
