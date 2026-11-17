# CLAUDE.md — admin (teacher) engine

## Responsibility
The **teacher-facing composition root** and dashboard backend. It is the one engine
allowed to hold grading *and* integrity/analysis at once, because the dashboard
shows them side by side. It owns:

- **bank management** — list / get / delete authored tests (delete guarded, below);
  `stats` (roster / attempts / active attempts) for the confirm dialogs.
- **content editor (no code)** — thin wrappers over `ContentService`'s editing API:
  update meta / section / item, add / remove / reorder, `duplicate_test`
  ("Создать черновик-копию"), JSON export / import, asset upload, and `preview_test`
  (the delivery projection, keyless). `_content()` maps `ContentEditError` to
  `AdminValidationError` / `AdminError` / `AdminStateError` (same Russian text) and logs
  `admin <action> target=… ok|refused`.
- **review queue** — list ingested `draft` tests and **approve** them. This is the
  single `draft -> published` human-approval gate (golden rule #5); approval is the
  ONLY way content goes live. Phase 9 ingestion only ever produces drafts.
  `unpublish` reverts it (guarded, below).
- **roster** — add students (one, or many via `add_students_bulk`); live roster
  status (assigned / in-progress / submitted / expired + the attempt deadline + the
  student's classroom **PIN**); reset / reopen (optionally with extra time) / remove;
  `regenerate_pin` («Сгенерировать новый PIN»).
- **results** — per-attempt score + advisory verdict + raw replay; per-test list
  ranked most-suspicious-first; CSV export; the printable student report. The
  verdict + integrity profile are READ from the frozen `AttemptReport` (computed
  once at finalize by `app/results`), never recomputed by an LLM here.
- **auth** — a single shared teacher password mints a short-lived HMAC-signed bearer
  token; every admin route requires it. Login is rate-limited (per IP + globally);
  the dev-default password/secret are refused fail-closed (below).

## Inputs / Outputs
- **In:** an authenticated teacher (bearer token); `test_id` / `attempt_id` /
  roster names. Reads the item bank, roster, attempts, and the append-only event
  stream through the existing repositories/services (plus a few batched read
  queries written with SQLAlchemy in `service.py` over `app.persistence.models` —
  read-only use; `app/persistence` itself is not edited from here).
- **Out:** `Test` / `RosterEntry` contracts, and the admin view DTOs in `models.py`
  (`ReviewDraft`, `RosterStatus`, `AttemptOverview`, `AttemptResult`, `TestStats`,
  `BulkAddResult`, `StudentReport`); CSV text for the export. The verdict is always
  presented next to the deterministic profile and the raw events (rule #6).
  `AttemptOverview` carries the stored `suspicion_score` / `confidence` /
  `ai_assist_max` (highest per-writing-item likelihood) / `verdict_model_id` — all
  None without a stored verdict; `AttemptResult.verdict_source` is `"stored"` or
  `"live_fallback"`.

## Must NOT
- **Let a cheating signal change a score (golden rule #2).** Grading and
  integrity/analysis are computed on independent paths and only *bundled*;
  "rank suspicious-first" reorders rows, it never edits a score.
- **Auto-publish (golden rule #5).** `approve` only flips an existing `draft`; it
  never invents content and refuses anything not currently a draft.
- **Send an answer key to a student.** This is a teacher surface; it serves
  authoring `Test`s for review, never to the runner.
- **Put integrity data on the student/parent report.** `StudentReport` carries
  scores, the stored per-skill feedback and writing marks only — no suspicion,
  verdict, events or devices; it is built without calling analysis or grading.
- **Grade or call an LLM on a read.** The results table, CSV and student report
  read the frozen `AttemptReport` + manual marks (the results table keeps its
  LLM-free deterministic fallback for an un-finalized attempt; the CSV leaves those
  cells empty). Only `regrade` / `finalize_missing` run the LLM — the writing
  grader, the feedback analyst AND the integrity analyst. `AdminService.__init__`
  raises if its read-path `AnalysisService` carries an analyst, and
  `build_admin_service` always builds it with `llm=None` (the `analysis_llm`
  argument goes to the regrade `ResultsService` only).
- **Publish from the editor.** Imports and copies are drafts in the review queue;
  editing never changes `status`; `approve` stays the one publish path.
- **Send a key through the preview.** `preview_test` returns `ClientTest` (built by
  `app.delivery.projection.project_test` over the default one-pool-per-section bank,
  `PREVIEW_SEED` option shuffle) — structurally keyless. The editor page itself does
  hold the authoring test (keys included): it is a teacher surface.
- **Add a persisted table / migration.** The view models are read-side DTOs (like
  `app/ingestion/models.py`), not contracts — no schema change (rule #4).

## Key invariants
- **Approval is the publish gate.** `approve(test_id)` → `ContentService.publish` →
  `draft -> published`, logged loudly. The only state transition to `published`.
- **Destructive actions are guarded server-side.** `delete_test` → 409 while the
  test has any attempt unless `force=True` (then attempts + roster are wiped
  explicitly, not via FK cascade); `unpublish` → 409 while an attempt is
  `in_progress` unless `force=True`. The dashboard reads `stats` first and tells
  the teacher how many attempts would be wiped before sending `force`.
- **Reopen keeps the deadline unless the teacher grants time.** Plain reopen keeps
  the original deadline (past it, the attempt re-expires at once).
  `reopen_attempt(..., extend_minutes=N)` sets `deadline = max(old, now + N)` for
  that one attempt, written through `AttemptRepository.update_attempt`, logged at
  INFO ("TEACHER EXTENDED"). This is a deliberate teacher override — golden rule #3
  forbids the student timer *pausing* (e.g. on a hidden tab), not a teacher
  granting extra time. It is the ONE caller allowed to pass a changed deadline to
  `update_attempt` (delivery always passes it back untouched).
- **The teacher's extension wins over the exam window.** Delivery caps the deadline
  at `ExamWindow.hard_close` only when it fixes it at start
  (`min(started_at + duration, hard_close)`); `reopen_attempt` does NOT re-apply
  that cap, so the result is exactly `max(old, now + N)` even past `hard_close`.
  Intended: the extension is an explicit, logged, per-student teacher action (the
  API bounds N to 1..1440 min), and delivery never re-reads the window for an
  existing attempt (it enforces the stored deadline only). Latent today — delivery
  runs with the permissive `_ALWAYS_OPEN` window — but when scheduled windows land,
  keep this precedence (or change it here, in the test and in the UI text together).
- **Only a finished attempt can be reopened.** `reopen_attempt` → 409
  (`AdminStateError`, WARNING log) unless the attempt is `submitted` / `expired`,
  or still `in_progress` in the DB but already past its deadline (lazy expiry not
  run yet — the roster shows it as "истёк"). An attempt being written inside its
  time is never logged as a reopen nor silently extended.
- **A reopened graded attempt is not shown with its stale grade.** While the
  attempt is `in_progress` and a report exists, `report_status` is `"reopened"`
  ("переоткрыт — ждёт сдачи"); the resubmit re-runs finalize (the report is older
  than the new finish). The results list likewise shows "—" (tooltip "от прошлой
  сдачи") in the suspicion / confidence columns instead of the previous submit's
  chip and "✍ AI?" marker; the API still carries the stored numbers (the detail
  shows them, labelled stale) and the server's suspicious-first order still uses
  them.
- **Polled views are batched.** `results_for_test` / `roster_status` load
  attempts, reports, manual marks and event counts in one query each per test
  (never per student); the suspicion columns come from those same batched reports.
- **The verdict is read, not computed.** List / CSV: `report.verdict` or nothing
  ("—" in the table, an empty CSV cell) — never a per-row analysis. "Suspicious
  first" sorts by the STORED `suspicion_score` (rows without one last, ties in
  roster order). Detail (`_integrity_view`): the stored verdict + stored profile
  verbatim; a crash-path report without a profile gets one re-extracted by
  `IntegrityService` (no LLM); with no stored verdict at all (still writing,
  finalize pending, a pre-`e5f6a7b8c9d0` report) the deterministic formula is
  computed for display through the LLM-free `AnalysisService`
  (`verdict_source="live_fallback"`, nothing persisted). Both recompute paths pass
  the submitted texts + the test's open_writing ids, so `WritingFeatures` appear.
  A re-opened attempt keeps showing its stored verdict (labelled "от прошлой
  сдачи") until the re-submit re-finalizes it.
- **The timeline speaks Russian for writing telemetry too.** `field_focus` /
  `field_blur` / `input_burst` / `paste_attempt` / `fullscreen_exit` each get a
  sentence (`_writing_event_label`); `writing_stats` flushes and short keyboard
  bursts of one field are folded into ONE "набор текста" line per focus session
  (`_TypingFold`). Capture only: integrity's thresholds are reused solely to pick
  what to highlight (⚠︎), and the raw `events` stay complete next to it (rule #6).
- **Manual marks need no grading pass.** `set_manual_grade` takes `max_points` from
  the stored report, else from the item definition (objective 1, writing/colour 5
  — the grader's own constants). Exception: every task of a B1 Writing Part 2
  "choose one" group is markable out of 5 (`_choose_one_max`); marking the uncounted
  task makes it the counted one (`GradingService.apply_override`).
- **Auth is enforced.** No admin route resolves without a valid, unexpired token;
  signature check is constant-time and the secret never leaves the server.
- **The dev-default password/secret are refused fail-closed.** While
  `TEACHER_PASSWORD` or `ADMIN_TOKEN_SECRET` is still the dev default, login AND
  every token are refused (503, ERROR log) — the default secret is public, so a
  token signed with it proves nothing. The single exception needs BOTH:
  1. `ENVIRONMENT` is *explicitly* "dev"/"test" (process env or `.env`).
     `Settings.environment` defaults to "production", so a deployment that never
     configured anything (`make run` behind ngrok with no `.env`) is refused instead
     of inheriting dev leniency. `.env.example` ships `ENVIRONMENT=dev`.
  2. the request is local (`auth.is_local_request`): the peer is a loopback IP AND
     it carries no proxy/tunnel forwarding header (`X-Forwarded-For`, `Forwarded`,
     `X-Real-IP`, `Via`, …). So `make run` + a browser on the same machine logs in
     with `change-me`; another device, or the far end of a tunnel, gets a 503 that
     says to set the two variables.
  Why both: the loopback check alone is defeated by anything on this host that
  relays without a header (a raw TCP tunnel, a bare reverse proxy — every client
  then *is* 127.0.0.1); the environment check alone would leave an explicit dev
  server wide open once it is tunnelled. Rule of thumb: the defaults are for the
  developer's own browser only — set both variables before sharing the dashboard.
- **Login is rate-limited per IP and globally** (`LoginRateLimiter`, sliding
  window `login_window_seconds`, default 300 s → 429 + Retry-After, WARNING log).
  `login_max_failures` (default 10) wrong passwords lock one client IP; a good login
  clears that IP. `login_global_max_failures` (default 30, 0 = off) caps wrong
  passwords from ALL clients together and is the real brute-force bound (rotating
  IPs buy nothing: ≤ 30 guesses / 5 min in total). While the global cap is hit even
  the right password is refused — accepting it would defeat the bound — so a
  distributed guesser can keep NEW logins locked; a teacher already signed in is
  unaffected (8 h token), and the cap can be raised via the env. The lockout check,
  the password compare and the failure count are ONE atomic step
  (`LoginRateLimiter.attempt`, under a lock); keys with no failure left in the
  window are dropped on every call. In-memory (single uvicorn worker), like the
  finalize locks.
- **Client IP = `request.client.host` — know your proxy.** uvicorn rewrites it from
  `X-Forwarded-For` only when the header comes from a trusted proxy
  (`--proxy-headers`, on by default; `--forwarded-allow-ips`, default `127.0.0.1`,
  which covers a local ngrok/cloudflared agent). Behind a proxy on another host, or
  one that sends no such header, EVERY client shares the proxy's address as one
  rate-limit key: ten wrong guesses by anyone then lock the teacher's login for the
  window. Run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy ip>` there
  (never `*` on a directly reachable port — the header is then attacker-controlled).
  The per-IP limit is deliberately lenient and the global cap carries the
  brute-force protection for exactly this reason.
- **PINs are teacher data (Phase 14).** `RosterStatus.pin` / the `RosterEntry` returned
  by add / bulk add carry the student's 4-char PIN (stored plaintext on purpose — see
  app/delivery/CLAUDE.md). `POST /admin/roster/{id}/pin` → `regenerate_pin`: a new PIN
  unique in the test, logged at INFO WITHOUT the PIN; only FUTURE starts need it (an
  open exam tab keeps its session token — reset the attempt to stop it); the API then
  COMMITS and only afterwards calls `get_pin_limiter().reset_entry`
  (`app.delivery.access`) so a locked-out student can start right away — never an
  unlocked entry with the old PIN still valid, nothing lifted if the commit fails. A
  reset keeps the PIN (retake with the same card). PINs never go into the results CSV
  or the student report. Dashboard: a «PIN» column (`code.pin`), «Сгенерировать новый
  PIN» per row (confirm explains "future starts only"; toast shows the new PIN) and
  «Печать PIN-карточек» → `apps/web/teacher-pins.html?test=<id>` (A4, 10 cut-out cards per
  sheet: test title, name, PIN, the share link, one-line how-to — plus Ukrainian /
  Russian for YLE tests; reads `/admin/tests/{id}` + `/admin/tests/{id}/roster` with the
  teacher token like `teacher-report.html`; every value through `esc()`).
- **Wrong-PIN monitor + «Снять блокировки» (Phase 14).** `GET
  /admin/tests/{id}/pin-activity` → `PinActivityResponse` (window minutes, threshold,
  wrong PINs in the window, `alert`, throttled IPs, and per student with wrong PINs or
  a lock: name, count, `locked_seconds` — never a PIN; locked first). `POST
  /admin/tests/{id}/pin-locks/clear` → `clear_test` on the delivery limiter: every
  student lock of the test + the throttle of every IP that failed on it (PINs
  unchanged), INFO `admin clear_pin_locks`. Both 404 for an unknown test. Dashboard:
  `loadRoster` also fetches the activity (a failure there never hides the roster) and
  `#pinAlert` shows «Много неверных PIN для этого теста за последние N мин…»,
  «Заблокированы: …», a throttled-address line and the counts per student; the
  «Снять блокировки» button (`#clearPinLocksBtn`) toasts the counts and reloads.
- **CSV cells cannot be formulas.** Every free-text cell of `results_csv`
  (`display_name` is typed by people) goes through `_csv_text`: a value starting
  with `=`, `+`, `-`, `@` (or their full-width forms), tab, CR or LF is prefixed
  with `'`, so Excel / LibreOffice / Sheets show it as text.
- **Every server value is escaped in the dashboard.** `teacher.js` /
  `teacher-report.html` build HTML strings, so each interpolated value — names,
  writing text, prompts, LLM summary / rationale / signals / model id, IP,
  user-agent, even numbers — passes through `esc()` (text and quoted attributes
  alike); asset ids are `encodeURIComponent`-ed into `/assets/…` (`assetUrl`);
  dialogs / toasts use `textContent`. New renderers must do the same.
- **Teacher-facing messages are Russian.** `AdminError` messages become the API's
  `detail` and are shown verbatim in the dashboard toast; log lines stay English.
- **Editor HTTP surface** (`apps/api/admin.py`, all teacher-only): `PATCH
  /admin/tests/{id}` (title/level/duration), `PATCH …/sections/{sid}`, `POST
  …/sections` (+ `position`), `DELETE …/sections/{sid}`, `POST …/sections/reorder`,
  `POST …/sections/{sid}/items` (+ `position`), `POST …/sections/{sid}/items/reorder`,
  `PUT …/items/{item_id}` (the whole authoring item), `DELETE …/items/{item_id}`,
  `POST …/duplicate`, `GET …/export.json` (attachment, WITH the key), `GET …/preview`
  (`ClientTest`), `POST /admin/tests/import` (multipart `test` + repeated `assets`,
  `?replace=&new_id=`), `POST /admin/assets` (multipart `file`). Edit bodies are plain
  JSON objects validated by the content engine (400 Russian, not FastAPI's 422);
  `?force=true` is the explicit "typo fix on a locked test" (published or with
  attempts). Upload caps are module-level (tests shrink them): per file
  `ASSET_MAX_BYTES` (50 MB, 400) / `IMPORT_JSON_MAX_BYTES` (5 MB, 400); per REQUEST
  `ASSET_REQUEST_MAX_BYTES` (55 MB) / `IMPORT_REQUEST_MAX_BYTES` (200 MB), enforced by
  `UploadLimitMiddleware` (registered in `apps/api/main.py`) BEFORE the body is
  buffered: a declared Content-Length over the cap → 413 (Russian JSON `detail`)
  without reading; otherwise the streamed bytes are counted and the 413 replaces
  whatever the app would answer (chunked / lying Content-Length). The two upload routes
  parse their multipart form themselves in a dependency that runs AFTER
  `require_teacher` (an anonymous body is never spooled), with Starlette `max_files`
  (`IMPORT_MAX_FILES` = 200 assets + the JSON → Russian 400; one file for `/admin/assets`)
  and `max_fields`; the spooled parts are passed to the service as file objects and
  streamed to storage. `ImportResult.unpublished` = a `replace` turned a published test
  back into a draft. Needs `python-multipart`.
- **Editor UI** (`apps/web/editor.js`, loaded after `teacher.js` and reusing its
  `api` / `guard` / `esc` / `assetUrl` / `renderStimulusReview`): «Редактировать» in the
  bank opens collapsible sections with per-type item forms (single_choice options +
  correct radio, gap_fill accepted / variants / «Неверные формы» (`rejected`, never
  accepted, authoring-only) as comma-or-line lists, matching letter
  from the section pool, open_writing prompt / bullets / word_min / rubric / grade
  mode, colour task), stimulus forms (text, matching pool "буква: текст" lines, audio /
  images with «Загрузить файл…» → `POST /admin/assets`), Save per item with the Russian
  error inline + toast, section / item add / delete / arrows (item arrows only where
  `can_reorder_items`). A locked test (published, or with attempts — `editorLocked()`
  mirrors `editor.is_locked`) shows a banner: «Создать черновик-копию», «Снять с
  публикации» (only when published AND no attempts — with attempts it would not unlock
  anything), and the «исправить только опечатки» checkbox (confirm explains option /
  A–H texts and the key stay locked → saves send `?force=true`). An import whose result
  has `unpublished` shows "тест снят с публикации и ждёт одобрения". «Предпросмотр как ученик» renders `GET …/preview`. The bank has
  «Экспорт JSON» and an «Импорт теста (JSON)…» box (`api(…, { form })` sends
  multipart). Every interpolated value goes through `esc()` / `assetUrl()`.
- **Log every admin action** at INFO (auditability): approve, unpublish, delete,
  add_student(s), reopen (+ extension), reset, remove, manual marks, results reads,
  CSV export, student report. Refusals log at WARNING.

## Layout
- `auth.py` — `TokenSigner` (mint/verify HMAC bearer token) + `verify_password` +
  `AuthError` + `insecure_defaults` / `is_local_request` / `is_loopback_host` (the
  fail-closed dev-default guard) + `LoginRateLimiter` / `LoginDecision`.
  `fastapi-users` is the documented upgrade path for multi-teacher.
- `models.py` — read-side DTOs. Compose contracts; persist nothing.
- `service.py` — `AdminService` (bank / review / roster / results / export / report)
  + `AdminError` (404) / `AdminValidationError` (400) / `AdminStateError` (409) +
  `build_admin_service` factory (wires every composed engine from a session).
- The HTTP surface lives in `apps/api/admin.py`; the dashboard in
  `apps/web/teacher.html` + `teacher.js` (Russian UI; one toast error path; 401 →
  sign out once; non-overlapping live polling that also refreshes the open detail)
  and the A4 print pages `apps/web/teacher-report.html` (student report) and
  `apps/web/teacher-pins.html` (PIN cards).
- Dashboard verdict UI (`teacher.js`): the results table shows the stored suspicion
  as a chip (ok < 0.3 <= warn < 0.6 <= bad) plus "✍ AI?" when `ai_assist_max` >= 0.6;
  the detail's "Анализ честности (рекомендательно)" section (`renderIntegrity`) sits
  directly above the timeline and the raw events: advisory disclaimer, suspicion /
  confidence bars, source ("Gemini <model>" / "формула без LLM"), summary, flags,
  the `error` note and one card per writing task (likelihood, signals, rationale,
  key `WritingFeatures`). `FLAG_LABELS` must hold a Russian label for EVERY flag
  the integrity / analysis code emits (a test scans the sources); unknown codes
  (Gemini's extras) are shown raw.

## Tests
`tests/test_admin.py` — auth enforced (401 without/with bad/expired token; login →
token works); approve flips draft→published and is the only publish path; results
endpoint returns score + verdict + event count; ranked suspicious-first; admin
actions logged; report-status chip; finalize-missing; reset / reopen.
`tests/test_admin_dashboard.py` — CSV (BOM, Cyrillic, per-skill, manual marks, no
grading, no formula cells), bulk add + dedupe, delete / unpublish guards + stats,
the login limiter (per IP, global cap across IPs, atomic under threads, no leaked
keys), the fail-closed dev-default guard (explicit env + loopback + no forwarding
header; unset env refused), reopen (409 while still writing, overdue allowed,
extension == max(old, now + N), never shortens, endpoint validation, "reopened"
status), student report (no integrity data, no grading/analysis), manual mark
max-points without grading, batched results reads. The HTTP fixtures run as a
loopback client in `environment="test"` (the token secret stays the dev default).
`tests/test_roster_pin.py` — PINs in add / bulk / roster (teacher-only, never on the
share-link list), regenerate (401 anonymous, old PIN 403, new PIN resumes the same
attempt, an open session survives, lock lifted — only after the commit, PIN not
logged), `pin-activity` / `pin-locks/clear` (401 anonymous, 404 unknown test, counts
per student, alert, no PINs, INFO log, the student gets in again), no PIN in the CSV,
reset drops the sessions but keeps the PIN, the PIN-cards page escapes its values.
`tests/test_admin_e2e.py` — Playwright (skips without Chromium). Smoke: login
(wrong password message), link button, the roster's PIN column matches the API,
«Сгенерировать новый PIN» changes it (toast), «Печать PIN-карточек» renders one card per
student with name + PIN + link, bulk add, sort + search, a 409 surfaced as a
Russian toast; the XSS test also opens the PIN cards.
`test_pin_lock_banner_and_clear_button`: 5 wrong PINs on Anna → the roster banner
lists her as locked with her count (no PIN), «Снять блокировки» toasts and clears it,
her right PIN works again. XSS: a student really named `<img src=x onerror=…>` plus a payload
injected into every string of the admin JSON (model id, summary, flags, signals,
rationale, timeline, devices, prompts, asset ids) executes nothing and adds no
element / `on*` attribute in the list, the detail, the test review and the print
report; a re-opened row shows "—" instead of the stale suspicion chip.
`tests/test_admin_editor.py` — every editor route is teacher-only (401); meta /
item / section / reorder / add / remove over HTTP; Russian 400 / 404 / 409; the
published guard + `?force=true`; duplicate → review queue → approve; export JSON
carries the key; multipart import round trip, collision / replace, assets stored +
served, missing assets listed, traversal names ignored, bad JSON, deep JSON → 400,
missing `test` part → 400, replace of a published test → `unpublished`; draft with
attempts locked over HTTP; option-text swap → 409; asset upload (type, magic bytes,
size cap, traversal name); upload limits: Content-Length over the cap → 413 with ZERO
`receive` calls (ASGI-level), a lying Content-Length / chunked body stopped by the
streamed count, import + asset 413 over HTTP (nothing stored), too many files → 400
(exactly the limit OK), two files on `/admin/assets` → 400, the upload reaches the
service as a stream; an anonymous upload is 401; the preview has no key field anywhere.
`tests/test_assets_api.py` — `/assets`: nosniff, `no-cache` + ETag, 304 on
`If-None-Match` (weak too), a replaced blob gets a new ETag, content-hash ETag fallback,
extension-less AAC not labelled mp3.
`tests/test_content_editor_e2e.py` (Playwright, skips without Chromium) — login,
import JSON + image via the dashboard, edit one gap_fill accepted answer and one option
text, a Russian inline error, preview (no key in the response), approve in the review
queue, then `exam.html?test=<id>` shows the edited text and no `/exam/` response
contains `correct` or the gap_fill key; after the student starts, the editor banner says
"уже есть работы учеников (1)" without «Снять с публикации», a forced key change shows
the inline «Создать черновик-копию» error, the copy opens as a free draft, and a
`replace` import over the approved copy toasts "снят с публикации и ждёт одобрения".
`tests/test_admin_verdict.py` — list / detail / CSV / student report read the stored
verdict with zero analyst calls (spy) and no recompute; the read-path analyst guard;
regrade is the only recompute; no stored verdict -> None in the list + the
deterministic live fallback with writing features in the detail (nothing persisted);
crash-path profile re-extraction; ranking + CSV by the stored suspicion (scores
untouched); the HTTP JSON; no integrity fields on the student report; a Russian
timeline sentence per writing event type (stats folded per focus session); every
emitted flag has a Russian label in `teacher.js`.
`tests/test_writing_telemetry_e2e.py` (Playwright) ends in the dashboard: a
finalized attempt with a blocked 300-char paste shows the "Анализ честности"
section, the writing card with the translated large-paste signal and the timeline
sentence above the raw events; a Gemini-shaped (intercepted) response renders the
"✍ AI?" marker; the print report shows no integrity text.
