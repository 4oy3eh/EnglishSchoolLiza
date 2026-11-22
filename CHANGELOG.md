# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security
- **Student PINs + per-attempt session tokens (phase 14).** Picking a name on the share
  link is no longer enough: every roster entry has a 4-character classroom PIN
  (`RosterEntry.pin`, alphabet without 0/O/1/I/L, `secrets`, unique per test, generated on
  every roster insert; migration `f6a7b8c9d0e1` adds the column, backfills existing rows
  and creates `attempt_sessions`). `POST /exam/roster/{id}/start` requires `{"pin": …}`
  (case-insensitive, spaces ignored; 400 missing / malformed, 403 wrong + `X-Pin-Tries-Left`,
  429 + `Retry-After` when locked) — a resume too — and returns a session `token` (stored
  as SHA-256 only). Every `/exam/attempts/{id}/…` call and the telemetry ingest
  `POST /attempts/{id}/events` now need it in `X-Attempt-Token` (sendBeacon: `?token=`,
  masked in the access log): missing / wrong → 401, the batch is dropped. Wrong PINs are
  rate-limited in memory: 5 per student in 5 min → 5-minute lock, plus a per-IP spray
  throttle (see Fixed) (WARNING logs). The share-link roster list still returns names + status only — never PINs.

### Added
- **IQ runner: step-by-step intro with practice, timer prominence, responsive layout
  (phase 17b).** Each subtest's intro now runs as three screens — *Instructions* →
  *Example* (the worked samples + the booklet's practice problems, answered and
  checked on the spot: right / wrong + the printed answer and explanation; never
  scored or saved) → *Ready* ("N questions · M min S s · about X s per question", the
  study note for memory subtests, "the clock does not stop if you leave this page",
  Start); Back between steps; the server's intro cap still covers all three (small
  "starts by itself in m:ss"). Contracts: `SectionIntro.practice` =
  `IntroPractice {prompt, images, kind text|choice|mark|drawing, options, solution,
  solution_images, explanation}` (public answers of unscored practice; the
  no-correct invariant test pins `solution` to this model only),
  `ClientSection.item_count` (kept while items are withheld), `GapFillItem.numeric`
  / `ClientGapFillItem.numeric` (numeric keyboard). All in JSON columns — no
  migration. `SECTION_INTRO_MAX_SECONDS` default 300 → 420 s (three screens +
  practice). Timer: big sticky header clock with a phase label, amber at 30 s, red at
  10 s, a one-time non-blocking "30 seconds left" toast, a "Time is up" interstitial
  before the next subtest after an expiry. Drawing: the page shows a thumbnail
  (page scrolls freely over it on phones); a tap opens a full-screen drawing overlay
  that fits either orientation, keeps the countdown, Undo / Clear / Confirm / Close
  (asks before discarding), auto-confirms when the time runs out. Responsive at phone
  portrait / landscape (compact header), tablet and desktop: no sideways page
  scroll, >= 44 px targets; teacher result card + printable report tables scroll
  inside their box on a phone. `scripts/build_iq_book.py` maps the booklet's practice
  problems into `practice` (T2 / T9 / T10 text, T3 / T4 choice — T4 practice rows cut
  from the practice picture —, T5 mark, T8 "try it" drawings), marks numeric
  `gap_fill`s, and its self-grade log uses `IQReport.notes` (was a stale `flags`).
  Tests: `tests/test_iq_intro_practice.py`, `tests/test_iq_runner_ux_e2e.py`, the IQ
  runner E2E updated for the steps + overlay (and a phone-width teacher check).
- **Phase 17 review hardening (IQ family).** *Answer size:* `PUT
  /exam/attempts/{id}/answers/{item}` refuses a str over `ANSWER_MAX_CHARS` (new
  setting, default 4 000 000 — above the biggest `colour_task` PNG data URL, 0.5-1.2 M
  chars for the YLE pictures) with 422, and a `drawing` answer over
  `contracts.content.DRAWING_MAX_RESPONSE_CHARS` (300 000, the grader's cap, now shared)
  with 413 + `X-Answer-Error: answer_too_large` (`AnswerTooLargeError`); both WARNING,
  nothing saved; the runner parks a 413 as "rejected" instead of retrying.
  *Study picture replay:* any asset referenced by some `Section.study` is refused by the
  public `GET /assets/{id}` (404, WARNING); `ClientStudyStimulus.asset_id` is replaced by
  `image_url` = `GET /exam/attempts/{id}/sections/{sid}/study/asset` (attempt token,
  only inside the study window, `Cache-Control: no-store`; 409 `study_not_open`
  afterwards). `exam-iq.js` fetches it as a blob and removes the `<img>` + revokes the
  object URL the moment the window ends. Teacher preview: `GET
  /admin/tests/{id}/sections/{sid}/study/asset` (bearer, no-store) shown in the
  editor's "Предпросмотр как ученик". Tests: `tests/test_iq_hardening.py`, the IQ e2e
  (blob picture, public 404, gone after the window), the Node drawing worst case.

- **IQ runner + teacher IQ views (phase 17.5 / 17.6, frontend).** New
  `apps/web/exam-iq.js` (loaded before `exam.js`, shares its save queue / i18n): a
  test whose sections all have clocks asks `GET …/sections` and, when `timed`, runs
  each subtest strictly in order — intro screen (instructions, samples, "Begin", the
  server's auto-start countdown) → begin → study stimulus shown big until
  `study_ends_at`, then removed → the whole subtest on ONE scrolling page with its own
  countdown (server deadlines + `server_now` offset, never paused) → "Finish this part"
  (confirm) or expiry → next intro; after the last one the runner submits. Any 409 /
  `X-Section-Error` re-reads the server state; a reload resumes intro / study /
  answering. `mark_toggle` = row + big "X" toggle (saves "1"/"0", ↑/↓ between rows);
  `drawing` = pen canvas over the picture (pointer events: mouse / touch / pen, no
  scroll while drawing, Undo / Clear, faint frame on the `lines` square), saved as
  `{"w","h","strokes"}` in picture pixels, points thinned and RDP-simplified under the
  grader caps; picture options in one row; `gap_fill` picture + small box, Enter → next
  question. Save loop: a section refusal (409 + `X-Section-Error`) drops that answer
  instead of halting the queue; drawings go to telemetry as `[drawing Nb]`. New
  strings in en / uk / ru (IQ UI is English). Non-timed tests never call `/sections`
  and run unchanged. Teacher: birth date per student on IQ tests (roster column with
  set / clear via `PUT /admin/roster/{id}/birth-date`, on single add, and
  `Имя; ГГГГ-ММ-ДД` lines in bulk add); IQ block on the result card and the printable
  report (subtests raw / max / scaled, composites I «левое» / II «правое полушарие» per
  the booklet with sums + IQ, overall e.g. "124+", bracket, age, notes, the
  non-clinical disclaimer); «Рисунки — проверить» cards: strokes over the picture
  (SVG), auto verdict + confidence, ✓ / ✗ override (existing manual grade → the IQ
  refreshes), toggleable target overlay; drawing thumbnails + ✓ / ✗ in the per-item
  table. Tests: `tests/test_iq_runner_e2e.py` (Playwright: full IQ flow incl. reloads,
  expiry auto-advance, canvas, override changes the IQ, report; non-IQ regression),
  `tests/test_iq_runner_js.py` (Node: payload caps / shape, timed detection).
- **Per-item picture on `gap_fill` + all-or-nothing section clocks (phase 17).**
  `GapFillItem.image` / `ClientGapFillItem.image` (optional asset id, like
  `MarkToggleItem.image`): checked as a safe asset id, counted in
  `referenced_asset_ids` (must exist on import; export / import / copies carry the
  file), projected to the client. Item fields live in the `items.data` JSON column —
  no migration. `editor.section_clock_errors`: an IQ test, or any test where some
  section has `time_limit_seconds`, must give it to EVERY section (a partly timed
  test used to fall back silently to one deadline); `study` still needs the
  section's clock. IQ book builder (`scripts/build_iq_book.py`, private sources only):
  Test 1 = 60 s study + 60 s recall (`time_limit_seconds` is the answering phase);
  one recall `gap_fill` per slot with its own cropped symbol picture ("Number for
  this symbol") instead of one numbered sheet, never the study grid's asset;
  `duration_minutes` = delivery's overall deadline Σ(intro cap + study + answering),
  checked against `app.delivery.sections`. Tests in `tests/test_iq_family.py`; the
  partly-timed delivery regression now stores its test directly (the editor refuses it).
- **IQ test family — timed subtests + teacher-side IQ (phase 17.2, backend of 17.4 /
  17.6).** A test whose served sections ALL have `time_limit_seconds` runs them
  strictly in order, each with its own server clock (never paused): an untimed intro
  (capped by `SECTION_INTRO_MAX_SECONDS`, default 300 s — then the section begins by
  itself), "Begin", an optional study window (`study_seconds`, the stimulus shown
  only then), the answering phase (`time_limit_seconds`), closed when finished early
  or at its deadline; the next intro starts at that moment. Overall deadline = Σ(intro
  cap + study + limit), window-capped; other tests are unchanged (a partly timed test
  ignores section clocks, WARNING). New table `attempt_sections` (contract
  `AttemptSection`, migration `f2a3b4c5d6e7`), written lazily with conditional
  statements (`app/delivery/sections.py` replays the rules; an abandoned tab's
  sections still close on time). Endpoints under `/exam/attempts/{id}`: `GET
  sections`, `POST sections/{sid}/begin` (+ the study stimulus inside the window),
  `GET sections/{sid}/study`, `GET sections/{sid}/items`, `POST
  sections/{sid}/finish`. Answers to a section that is not answering are refused
  (409, `X-Section-Error` + body `code`: `section_not_current` / `section_not_begun` /
  `section_study` / `section_closed` / `section_expired`; WARNING log). The bulk
  `ClientTest` withholds a timed section's items until it answers
  (`ClientSection.items_withheld`) and never carries study content. Teacher side:
  `AttemptResult.iq` / `StudentReport.iq` recomputed on every read from the merged
  grade (a drawing override or a birth-date change shows at once; not persisted),
  CSV IQ columns after `parts` (per subtest raw + scaled, composites, overall,
  bracket, notes, age), drawing strokes + target + confidence in the results
  payload, mark_toggle rows rendered; `PUT /admin/roster/{id}/birth-date`,
  `birth_date` on add student and `birth_dates` on bulk add (teacher-only, never on
  any student payload). `app.grading.build_iq_report` (pure core of `iq_report`).
  Tests: `tests/test_iq_delivery.py`.
- **IQ test family — backend contracts, grading and norms (phase 17.1/17.3/17.4).**
  Level `IQ` (not Cambridge: one paper `"iq"` covering every section, English UI, no
  Cambridge / writing scale). New item types: `mark_toggle` (a row to mark; authoring
  `correct: bool`; graded per section — each correct mark 1 point, each FALSE mark
  cancels one correct mark, floor 0; the cancellation is written into the per-row
  `ItemGrade`s) and `drawing` (canvas over a picture; authoring-only `target`:
  `region` zones (rect / polygon, `min_ink`, `min_inside_ratio`, `margin`) or `lines`
  reference segments in a square with `tolerance` + the 8 symmetries of the square).
  `app/grading/drawing.py` is a pure deterministic stroke grader returning a verdict
  + confidence; below 0.5 the item goes to `needs_review` and the existing
  `ManualGrade` override settles it; absurd payloads (size / shape / NaN caps) are 0 +
  review, never an exception. Sections gain `time_limit_seconds`, an untimed `intro`
  (text, pictures, samples) and a show-then-hide `study` stimulus — the bulk client
  payload only carries `ClientStudyInfo` (its length); the content is a separate
  `ClientStudyStimulus` for the later delivery step. `Test.norm_scoring`
  (authoring-only `NormScoring`: composites, raw→scaled bands per section, sum→score
  table per age group with open cells like `"130+"` / `"<70"`, mean of composites,
  brackets, `min_age`) and the pure `app/grading/norm_scoring.py` → `IQReport`
  (contract in `contracts/runtime.py`; `notes` such as `no_norms_for_age`,
  `under_min_age`, `above_table`). `GradingService.iq_report(result)` computes it from
  a (possibly overridden) result, age from the new teacher-only
  `RosterEntry.birth_date` at attempt start (`contracts.runtime.age_at`); not
  persisted yet. Editor rules for all of it (drawing geometry, a row to mark, study
  needs a section clock, norm tables ordered / monotonic / gap-free / covering the
  composite sections, age groups non-overlapping); `reid_test` rebases the norm
  tables' section ids. Migration `e1f2a3b4c5d6` (tests.norm_scoring, sections
  time_limit_seconds / intro / study, roster_entries.birth_date). Tests:
  `tests/test_iq_family.py` on a synthetic fixture (`tests/iq_fixture.py`), the
  no-key invariant extended to targets and norm tables.
- **Whole test or only some parts (papers), the teacher's choice.** Each section's
  Cambridge paper is derived from level + skill (`contracts/papers.py`, pure): YLE and
  A2 Key — Listening / Reading & Writing; B1 Preliminary — Reading / Writing /
  Listening; B2 First / C1 / C2 — Reading & Use of English / Writing / Listening. Per-paper
  minutes: optional explicit `Test.paper_minutes` (must name every paper and sum to
  `duration_minutes`), else `duration_minutes` split proportionally to the official
  times (A2 60/35, B1 45/45/36, B2 75/80/40, C1 & C2 90/90/40, YLE R&W 20/30/40 + L
  ~20/25/25; integer largest-remainder split that always sums exactly). The roster entry
  carries `papers` (None = all) and the attempt snapshots it at start (migration
  `d0e1f2a3b4c5`: `tests.paper_minutes`, `roster_entries.papers`, `attempts.papers`,
  nullable JSON). Delivery serves only those papers' sections (authored order and
  randomisation kept), their tracks only, refuses their items otherwise (404), and fixes
  the deadline at start to the sum of their minutes (still capped by the window).
  Grading, feedback, results, CSV, the student report and the B2–C2 skills average /
  Cambridge scale cover only the served papers; choose-one writing applies only when
  Writing is served. Teacher API: `GET /admin/tests/{id}/papers`, `PUT
  /admin/tests/{id}/paper-minutes` (timing meta: allowed on a published test with
  work), `papers` on add / bulk add, `PUT /admin/roster/{id}/papers`, `PUT
  /admin/tests/{id}/roster/papers` (whole class; started students skipped). Unknown or
  absent paper → 422; change after the start → 409 (a reset unlocks it). Dashboard:
  «Части теста» panel («Все части» by default, tick parts, «Применить ко всему классу»),
  a «Части» roster column with an inline editor (🔒 with the reason once started), a
  «Частично: Listening» chip in the results and the detail, a trailing `parts` CSV
  column, «Только: Listening (N мин)» on the PIN cards, the label on the print report,
  a timeline metadata line "Выданы только части: …"; the editor gets «Время по частям»
  with a live «сумма = N мин» check. Students never choose. Tests: `tests/test_papers.py`,
  `tests/test_papers_e2e.py` (Playwright: teacher assigns Listening only → the student
  sees only Listening). Review hardening: a partial B2–C2 attempt shows per-skill values
  only — no overall average / overall Cambridge score («общий балл не считается — сданы
  не все части», detail, print report, empty CSV `skills_avg_percent`); a manual mark on
  an item of an unserved part is refused (400) and choose-one maxima are computed over
  the served sections only; `start` now claims the roster entry first (conditional
  UPDATE) and reads the papers after it, so a teacher's change either applies or gets
  409, never lost; the roster row warns «выбранных частей больше нет в тесте — будет
  выдан весь тест»; one `_roster_row` helper builds every roster row. Known limitation:
  `/assets/{id}` is not attempt-scoped — assets of unserved parts are fetchable by id
  (no answer data in assets). Tests: `tests/test_papers_hardening.py`.
- **Matching-pool pictures.** `PoolOption.image` (a picture per word — YLE word boxes)
  and pool-level context `MatchingPoolStimulus.images`, both served (not answer data).
  Runner: word-bank chips show the picture above the word (alt = the word), the filled
  answer box a small one, letter buttons and the «Choose from» list a thumbnail; the
  pool's pictures render above its passage. Teacher review, editor («буква: текст |
  файл.png» + pictures field) and `scripts/review_html.py` show them; asset checks
  (missing / unreferenced / safe id) include them. No migration (JSON fields).
- **Option-shuffle opt-out.** `SingleChoiceItem.shuffle_options` (default true); false
  keeps the authored order for every student ("yes / no", "true / false / doesn't
  say") — the displayed→canonical map still applies, so grading is unchanged. Editor
  checkbox «перемешивать варианты», shown in the review page. Tests:
  `tests/test_pool_images_shuffle.py`.
- **Runner: B2 / C1 / C2 item shapes (phase 15).** `apps/web/exam.js` + `exam.html`.
  **Key word transformation**: lead-in sentence, the KEY WORD as a bold capital chip,
  the second sentence split at its gap (`_{3,}|\.{3,}|…+`) with ONE inline text box,
  the rubric "Use between {min} and {max} words, including the word given." and an
  informational whitespace word count (green in range, amber over — never blocking);
  saved as a plain string through the gap_fill path (debounced save, navigator, review,
  resume, `paste_attempt` with field `gap_fill`, length only). **gap_fill**
  `stem_word` → "(COMPETE)" beside the box and inside the inline passage slot;
  `max_words` → "One word only." / "No more than N words.". **matching_pool**
  `each_option_once` → an option already used by another question is greyed and a
  gentle, never blocking note "A is also your answer to question 3 …" shows on both
  questions (re-picking stays allowed); without the flag the "→ 3" badge stays as
  before. A pool's own **`audio`** opens / continues listening tracks exactly like an
  `audio_asset` (`sectionAudio` in `buildModel`: C1/C2 Listening Part 4 Task 1 + 2 =
  one track). **Inline gap slots now also in `passage_text`** (KET 2016 Writing Part 8
  note forms "(51)…(55)", word-formation passages), but there only gap_fill questions
  and only by explicit numbering (question number or a prompt starting "51 …" /
  "(17)") — never by order, so a notice that happens to say "(1)" stays text. A blank
  passage no longer renders an empty box. New strings in en / uk / ru. Tests: more
  Node-harness cases in `tests/test_runner_answer_aids.py` and the Playwright
  `tests/test_runner_b2_e2e.py` (synthetic B2 test with word-formation, KWT,
  each-option-once and pool-audio parts). Review fixes: an inline passage slot shows a
  sentence / paragraph option as "C — The trip had…" (letter + ~28-character clip, the
  aria-label too) and only short options in full; "gap N" / leading-number prompts
  link only when they cover EVERY numbered gap (else by order, as before); a gap
  number repeated in a passage gets one slot (first occurrence); the word bank needs
  ≥ 3 matching questions. A KWT paste stays `field: "gap_fill"` (the telemetry
  `FieldKind` enum is `open_writing | gap_fill`).
- **B2 First (+ for Schools), C1 Advanced, C2 Proficiency (phase 15).** Levels
  `B2_FIRST` / `C1_ADVANCED` / `C2_PROFICIENCY` (contracts, editor, dashboard, report;
  English runner UI). New item type **`key_word_transformation`** (lead-in `prompt`,
  `key_word`, `gapped_sentence` with one gap, `answers` = list of part A | part B
  patterns with per-half variants, optional `min_words` / `max_words`, `points` default
  2): 1 mark per correct half (best over every pattern and split point), 0 when the word
  count (key word included; contractions = two words, `can't` = one at B2 only) is
  outside B2 2–5 / C1 3–6 / C2 3–8, the key word is missing / changed or two answers are
  given; strict spelling, case / punctuation ignored; per-half verdict in the grade's
  feedback, "◐ 1/2" + 0/1/2 override buttons on the dashboard. gap_fill `stem_word`
  (word formation, served) and `max_words` (served; longer answers and "in/on" are
  wrong); spelling defaults: Use of English exact, listening exact at C1 / C2 (B2 stays
  tolerant). Matching pool `each_option_once` (gapped text vs reusable multiple
  matching, served for the runner) and an optional pool `audio` so C1/C2 Listening
  Part 4's two parallel tasks share ONE track. Section `reported_skill`
  (`use_of_english` for Reading and Use of English Parts 2–4; migration
  `c9d0e1f2a3b4` adds `sections.reported_skill`): feedback, results detail, CSV
  (`use_of_english_*` + `skills_avg_percent` for B2–C2) and the student report show Use
  of English; the B2–C2 detail adds per-skill totals, their average (no Speaking) and an
  approximate Cambridge English Scale estimate from the official conversion anchors.
  Writing at B2–C2 is graded by Gemini on the Cambridge four subscales (Content,
  Communicative Achievement, Organisation, Language × 0–5 = 20, handbook descriptors
  verbatim), scaled to the item's `points`; task and student text are now nonce-fenced
  in the grading prompt at every level. The editor creates / edits all of it and JSON
  export / import round-trips every new field. Student-runner rendering of the new
  item type follows (payload documented in `app/delivery/CLAUDE.md`).
- **Easier answering in the student runner (phase 15, view only).** `apps/web/exam.js` +
  `exam.html`; the answer model and every PUT payload are unchanged (matching = option
  key, gap_fill = string, single_choice = displayed index), so grading, telemetry and
  integrity are untouched. (1) Matching buttons show the letter AND the option text
  (short pools in a wrapping grid, longer ones as a list; texts over 90 characters are
  clipped to ~56 with "…", the full text stays in the "Choose from" list) and a "→ 3"
  badge when another question of the part already uses that option — never blocked.
  (2) **Word bank** for parts with ≥ 2 matching questions whose options are all short
  (≤ 3 words, ≤ 24 characters — Flyers R&W Part 1, KET Listening Part 5): answer boxes
  + a sticky bank of word chips; the next empty box is picked automatically, tap a chip
  to place it, tap it again (or ✕) to clear, reuse allowed; desktop (fine pointer) can
  drag a chip onto a question (custom MIME type, never a text drop); keyboard: Enter on
  a box moves into the bank, Enter places, Escape returns; a polite live region
  announces "“jam” is now in question 3."; on phones a long bank becomes a two-row strip
  that scrolls sideways. (3) **Gapped passages** (`gapped_text`, `matching_pool` with
  text): each numbered gap ("(19) ......", "…(1)…", "(3) ___") becomes an inline slot
  mirroring the current answer (typed text live, chosen option text); tapping it scrolls
  to and focuses the question, and each such question has "↑ In the text". Gaps are
  linked by the displayed question number (A2/B1), else "gap N" in the prompt, else
  1..k within the part (YLE); "(0)" examples and passages without markers render as
  before. New UI strings in en / uk / ru; ≥ 44 px targets; `prefers-reduced-motion`
  respected; less chrome padding on phones. Tests: `tests/test_runner_answer_aids.py`
  (Node harness over the real exam.js helpers: every bundled gapped passage links all
  its gaps in order, text never changed, word-bank detection, clipping) and
  `tests/test_runner_answer_aids_e2e.py` (Playwright: tap / reuse / clear / drag /
  keyboard / resume / telemetry `answer_change`, phone taps without sideways scroll,
  gap mirrors and jumps).
- **gap_fill numbers as digits or words (global rule).** `app/grading/numbers.py`
  `canonical_numbers` rewrites number words to digits on both sides, so "12" ==
  "twelve", "21" == "twenty-one" == "twenty one", "105" == "one hundred and five",
  "3rd" == "third", "12 apples" == "twelve apples" for every accepted answer, in every
  spelling mode — compared exactly by value ("seven"/"11", "13"/"30", "fifteen"/"50"
  stay wrong); `rejected` still wins. A misspelt number word ("twelv", "fourty") is
  accepted only in tolerant mode and only when it unambiguously misspells that number
  (the same guard now applies to a number-word key on the literal typo path). DEBUG log
  "correct via number equivalence". Tests: `tests/test_grading_numbers.py`.
- **Per-part listening audio.** Each listening section may carry its own recording
  (Cambridge YLE ships one mp3 per part — no more concatenating them on ingest). Inside a
  listening block a section with a different `AudioAssetStimulus.asset_id` starts a new
  track; the same file or a non-audio part continues it, so every existing test (one long
  mp3 repeated on each part) is still exactly one track (`app/delivery/audio.py`
  `listening_tracks`). Runner: tracks play strictly in order (a later part says its
  recording waits for the current one, with «Go to …»); a locked track is never stopped by
  navigation; the next track starts by itself when the student is on its part right after
  the previous one ended (sound-check / first track / resume after reload → Start button);
  "Recording N of M"; all new strings in EN / УКР / РУС. Progress is stored per track:
  new table `attempt_audio_progress` (contract `AudioTrackProgress`, migration
  `b8c9d0e1f2a3`, atomic max upsert); `POST /exam/attempts/{id}/audio-progress` takes
  `{asset_id, seconds}` (unknown track → 400) and `/state` returns `audio_progress`
  `{asset_id: seconds}`, so a reload resumes the right track at the right point. The old
  `{seconds}` body still works (legacy column, applied to the first track by the new
  runner) so an exam tab open during the deploy keeps its place. Editor: per-part audio
  hint; validation refuses one file in non-consecutive parts (A, B, A) and a sound-check
  equal to its track.
- **Per-item points.** Authoring items get an optional `points` (1–20, never served; None
  = the old defaults: objective 1, writing / colouring 5). Grading: a right objective
  answer earns all its points; a writing item's 0–5 grader band is scaled to its points
  deterministically (half-up to a whole mark, e.g. a 2-mark Movers R&W Part 6 sentence:
  5 → 2, 3.5 → 1, 1 → 0); manual-review / colour tasks are out of their points. Teacher
  manual marks are validated against them (`app.grading.item_max_points` when no report),
  and the CSV / student report totals follow. Editor: a «Баллы» field on every question
  (empty = default); the «задание на выбор» tasks of one section must be worth the same.
  Stored in the item JSON — no migration; JSON Schema regenerated.
- **Scheduled exam window per test.** `Test` gains optional `opens_at` / `closes_at`
  (timezone-aware, stored UTC) and `late_start_grace_minutes` (0–1440), stored as columns on
  `tests` (migration `a7b8c9d0e1f2`; existing tests stay always-open). Delivery now builds the
  window from the test (`ExamWindow.for_test`; `_ALWAYS_OPEN` is gone): a NEW start outside
  `[opens_at, closes_at + grace]` is 403 with `X-Exam-Window: not_yet_open|closed` (+
  `X-Exam-Opens-At`), checked BEFORE the PIN so typos on a closed test are not counted; a
  resume is never window-checked; the deadline stays `min(start + duration, closes_at +
  grace)`, fixed at start (golden rule #3) — changing the window never moves a running
  deadline, and the recovery sweep is unchanged. `GET /exam/tests/{id}/info` adds
  `window_state`, `opens_at`, `closes_at`, `starts_until`, `server_now`; the runner shows
  "Opens: …" / "Closed" / "Start until …" on the picker and the matching PIN-screen
  message, in EN / УКР / РУС for YLE tests, and re-checks at the opening time. Teacher:
  `PUT /admin/tests/{id}/window`, `POST …/window/open-now` (`{"minutes": N}`),
  `POST …/window/close-now`, `GET /admin/server-time`; the dashboard gets an «Окно экзамена»
  panel (datetime pickers in the teacher's local time, grace, «Открыть сейчас на N минут»,
  «Закрыть сейчас»), a window column in the bank, the server clock (with a skew warning) in
  the header, and the window on the PIN cards. The window is schedule meta, not content:
  `ContentService.set_schedule` writes only the three columns (no lock, no `force`, even on a
  published test with attempts) and the editor's lock ignores it
  (`editor.SCHEDULE_FIELDS` / `is_schedule_only_change`).
- **Pre A1 Starters and A2 Flyers levels.** `Level` adds `PRE_A1_STARTERS` / `A2_FLYERS`
  (the `level` column is a plain string — no migration). They get the YLE uk/ru runner UI
  and trilingual PIN cards (codes already listed), Russian labels in the dashboard / editor
  / student report, a level description in the Gemini integrity prompt, a mention in the
  feedback prompt, and the ingestion CLI `--level` accepts every `Level`. YLE Reading &
  Writing parts are authored as `skill="reading"` sections, like Movers.
- **PIN screen in the runner.** After picking a name the student types the PIN (auto
  upper-case, clear English errors with tries left, lock message, «This is not me»). The
  token lives in the existing localStorage resume state, so a reload resumes without the
  PIN; a missing / rejected token brings the PIN screen back and resumes the same attempt
  (save queue, telemetry, review, audio, timer unchanged). The old "are you really …?"
  confirm is replaced by the PIN.
- **Teacher dashboard: PINs.** The roster table shows each student's PIN,
  «Сгенерировать новый PIN» per student (`POST /admin/roster/{id}/pin` — only future starts
  need it; an open exam tab keeps working; it also lifts a wrong-PIN lock) and
  «Печать PIN-карточек» (`apps/web/teacher-pins.html`: A4, 10 cut-out cards per page with
  name, test, link and PIN). PINs stay out of the results CSV.
- **Movers UI in Ukrainian / Russian.** For YLE levels (`A1_MOVERS`, and Starters / Flyers
  codes once they exist) the runner's start screen offers EN / УКР / РУС; the choice is
  remembered and translates every UI string (buttons, navigator, review, save indicator,
  timer warnings, audio instructions, PIN screen, errors, done screen, the monitoring
  notice) and `<html lang>` — never the test content. Other tests stay English with no
  switcher. New `GET /exam/tests/{id}/info` (title + level) for the start screen.
- **Content editor — fix and add tests without code.** New `ContentService` editing API
  (`app/content/service.py` + pure helpers in `app/content/editor.py`): update test
  metadata, sections (title, skill, stimulus text / pool / audio / images), items (prompt,
  options + correct key, gap_fill accepted + variants, matching letter, open_writing
  prompt / bullets / word_min / rubric), add / remove / reorder sections and items. Every
  write validates the whole test against the contracts plus semantic checks and refuses
  with a Russian 400. Items in a section with a shared passage / recording / pictures
  can't be reordered (golden rule #7).
- **Published-test safety.** A test that is published OR already has student attempts
  (a draft after unpublish included) only takes cosmetic text fixes (title, prompts,
  stimulus text, bullets — never option / matching-pool texts), and only with an
  explicit `?force=true` (logged at WARNING); key / option / item-set / structure changes
  are refused (409) — without attempts unpublish first or **«Создать черновик-копию»**,
  with attempts only the copy (`POST /admin/tests/{id}/duplicate` → `<id>-v2`, a draft in
  the review queue).
- **JSON import / export.** `GET /admin/tests/{id}/export.json` (authoring JSON with the
  key, teacher-only) and `POST /admin/tests/import` (multipart Test JSON + asset files;
  stored as a **draft** — never auto-published; id collision refused unless `?replace=true`
  and no attempts; missing asset ids listed). `POST /admin/assets` uploads mp3 / png /
  jpg / webp (≤ 50 MB, magic bytes must match, safe flat ids, never overwrites). Request
  bodies are capped before buffering (413: 200 MB per import, 55 MB per upload), at most
  200 asset files per import, and uploads are streamed to storage.
  CLI: `make export-test id=…` / `make import-test file=… assets=dir`
  (`app/content/transfer.py`). `docs/INGEST_VIA_CLAUDE_CODE.md` §6a: Claude Code now
  delivers a Test JSON + assets and imports it; the per-test Python builder is legacy.
- **Dashboard editor (Russian)** — `apps/web/editor.js`: «Редактировать» / «Экспорт JSON»
  in the bank, «Импорт теста (JSON)…», collapsible sections with per-type forms, file
  upload buttons, Save per item with inline Russian errors, and «Предпросмотр как
  ученик» (`GET /admin/tests/{id}/preview`, the keyless delivery projection).
- **gap_fill typo tolerance by key length** (grading): Damerau-Levenshtein matching
  scaled to the accepted answer's length, with guards so a different inflection, an
  irregular-verb form or a confusable real word is never typo-accepted; new optional
  authoring-only `GapFillItem.rejected` list (forms the key marks wrong) — editable in
  the dashboard editor («Неверные формы»), never served to students.
  `GradingService(fuzzy_threshold=…)` became `fuzzy: bool`.
- **B1 Writing Part 2 "choose one" counts exactly one task** (grading): when both
  optional tasks are written, one is graded and counted, the other is graded but counts
  0 (`ItemGrade.max_points` is now `ge=0`).
- **Student review screen before submit** (runner): "Review & submit" (`#reviewBtn`)
  opens a review panel that counts answered / unanswered questions before the final
  "Submit test" (`#submitBtn`); plus accessibility labels on the runner's controls.
- **Timer warnings** at 10 and 2 minutes left (runner).
- `ContentRepository.replace_test` / `count_attempts` / `ids_in_use_elsewhere` /
  `test_exists`. New dependency `python-multipart` (FastAPI file uploads).
  **Contract changes:** `GapFillItem.rejected` (new optional list), `GapFillItem.spelling`
  (+ the bundled wordlist it uses) and `ItemGrade.max_points` now `ge=0` — JSON Schema
  regenerated (`contracts/jsonschema/`). **No Alembic migration:** items, stimuli and
  attempt reports are stored in JSON columns, so the table shapes are unchanged.

### Deprecated
- The legacy `{seconds}` body of `/audio-progress` (no `asset_id`, written only by a
  runner page opened before per-part audio shipped) and `attempts.audio_progress_seconds`
  will be removed in the next phase.

### Fixed
- **Typographic apostrophes / quotes in answers.** Phones type ’ ‘ ʼ “ ”: the grader now
  folds them to ASCII ' / " (`app/grading/normalize.fold_typography`) for responses AND
  accepted / variants / rejected keys and key word transformation halves, so "don’t"
  equals "don't".
- **A stopped listening recording can no longer freeze every later part.** If the
  browser refuses to resume a locked track after a pause (iOS screen lock, an audio
  interruption) or the file fails to load / decode (`error` event, 404, network drop),
  the runner no longer stays "playing" forever: the bar shows «▶ Resume listening» (the
  same play, from where it stopped — no play is spent); a second consecutive load error
  skips the track with «The recording could not be played — tell your teacher» (EN / УКР /
  РУС) so the next parts' recordings stay reachable. Each case is a telemetry note
  (`audio_play` with `payload.note` = `play_refused` / `load_error` / `skipped`, a ⚠︎
  Russian line in the teacher timeline) and a console warning.
- **Replays are no longer thrown away by the next recording.** With `plays=2` the next
  track used to start by itself the moment track k ended; now it auto-starts only when
  track k has used all its plays. Otherwise part k offers «↻ Play it again» / «Go to the
  next recording», and the next part shows «… can still be played again (n left)» with a
  replay button (starting the next recording still forfeits it).
- **Choose-one manual mark limit with mixed points.** The uncounted Writing Part 2 task
  was markable up to the largest writing task of the WHOLE attempt (Part 1 worth 10 → 8
  accepted on a 5-point Part 2 task, then silently dropped by grading); it is now out of
  its own group's maximum (`_choose_one_max` over `GradingService.choose_one_groups`).
  The dashboard finds the counted sibling by the new `ItemResponse.section_id` (titles may
  repeat). A review row of an ungraded item shows `item_max_points(item)` instead of 1.
  Editor: «одинаковые баллы» for choose-one tasks compares their worth (unset == 5).
- **`POST /exam/attempts/{id}/audio-progress` `seconds` is bounded** (0..86400, strict
  int; NaN / Infinity / 1e30 / fractions → 422) on both the per-track and the legacy path —
  an unbounded value overflowed SQLite (500).
- **`scripts/review_html.py` script-context XSS.** Values embedded in the inline script
  (test id) went through `json.dumps`, which leaves `</script>` intact; `<`, `>`, `&` are
  now `\u`-escaped (`js_str`). New tests (hostile id / title / prompt / options / NOTES,
  key highlight, missing-asset WARNING) and `make review-test file=...`
  (docs/INGEST_VIA_CLAUDE_CODE.md §6a).
- Per-track audio progress logs one INFO when a track's first point lands, DEBUG for the
  ~5 s updates (a class of 30 no longer floods the session console). PIN cards: the exam
  window reads «с … / до …» instead of English "from / until".

- **A shared school IP can no longer lock a whole class out of the PIN screen.** The
  hard per-IP lock (30 wrong / 5 min, refused even the right PIN, cleared only by a restart)
  is replaced by a spray throttle: an IP is slowed (30 s, doubling to 5 min) only when ≥ 5
  DIFFERENT students it failed on stay unresolved > 2 min; a right PIN resolves its student
  and forgives one other. Per-student lock unchanged. New per-test wrong-PIN monitor (≥ 40 in
  10 min → `PIN ALERT` WARNING + dashboard banner «Много неверных PIN…» with counts per
  student), «Снять блокировки» (`POST /admin/tests/{id}/pin-locks/clear`), «new PIN» lifts
  the lock only after the commit; limiter lives in `app/delivery/access.py`. Also: backups
  turn Windows file errors into `BackupError` (one rename retry) and use UTC names; a stale
  `var/session.pid` with a reused pid no longer blocks restore; `/exam/tests/{id}/info` +
  `/roster` serve published tests only; telemetry ingest answers 401 (not 404) for an
  unknown attempt; the runner's audio warning is escaped.
- **Editor guard: a draft with attempts no longer takes key / structure changes**
  (`app/content/editor.py` `check_edit_allowed`). Previously a draft that students had
  already written (e.g. after unpublish) accepted any change with only a WARNING, and a
  later «Переоценить» re-scored finished work against the new key while the attempt
  layout was rebuilt from the edited sections. Now: 409 with a Russian message pointing
  to «Создать черновик-копию»; cosmetic fixes need `force=true` (WARNING). The editor
  banner / confirm say so and hide «Снять с публикации» when it would not help.
- **Option and matching-pool texts are part of the key** (`KEY_BEARING_CONTAINERS`):
  swapping the texts of A and B changed the right answer without touching `correct`, and
  was accepted as a "typo fix" on a published test. Stimulus / prompt / title text stays
  cosmetic.
- **Upload size caps now apply before the body is buffered** (`UploadLimitMiddleware`,
  `apps/api/admin.py`): Content-Length over the cap → 413 without reading, streamed
  count otherwise (chunked / lying Content-Length); Starlette `max_files` / `max_fields`
  set (≤ 200 assets per import → 400); the form is parsed after the teacher check;
  asset parts are streamed to storage in chunks (`StorageBackend.put_stream` / `digest`)
  instead of being held in memory together.
- `import_test`: absurdly nested JSON → Russian 400 instead of a 500 (`RecursionError`);
  `replace=true` over a published test logs a WARNING and returns `unpublished: true`,
  shown in the dashboard ("тест снят с публикации и ждёт одобрения").
- `upload_asset`: the name is reserved atomically (`put_stream(exclusive=True)`, a hard
  link that fails if the target exists), so concurrent same-name uploads never overwrite
  each other.
- `/assets/{id}`: `X-Content-Type-Options: nosniff`; `Cache-Control: public, no-cache` +
  a strong `ETag` (304 on `If-None-Match`) instead of `max-age=3600`, so a blob replaced
  by an import is never served stale.
- mp3 sniffing checks the MPEG Layer III header bits — AAC/ADTS, MP1/MP2 and random
  `FFE…` bytes are no longer accepted as `.mp3`.
- Runner review panel: the parts list is rebuilt only when it changed (and focus is
  restored by question id when it does), so save ticks no longer drop keyboard focus.

### Changed
- **The deterministic rule engine is the cheating verdict of record** (owner's
  decision: the marks are readable without AI). New `app/integrity/rules.py` turns the
  integrity profile into explainable `RuleHit`s (code, Russian title, severity
  сильный/средний/слабый, an explanation built from the actual numbers — e.g. "ушёл со
  вкладки 9 раз; 7 ответов дал в течение 2 с после возврата" — and references to the
  timeline events). Rules: systematic tab-switching, time away, writing (text appeared
  without typing, large paste attempt, scripted input, implausible typing speed, away
  while writing — downgraded by `telemetry_gap`), device switch, fullscreen exits,
  possible screenshot. Overall suspicion, ranking and the results chip come only from the
  rules (bounded combination; weak signals alone are capped below "высокая"). All
  thresholds are constants documented in `app/integrity/CLAUDE.md`. New `RuleHit`
  contract inside the stored verdict JSON — no migration.
- Dashboard "Анализ честности": rule cards first, strongest first, each linking to and
  highlighting its timeline moments; Gemini output shown separately as
  "Второе мнение (Gemini) по письму".
- **YLE durations follow one rule** (school asked for extra time; timer unchanged):
  ceil(listening audio min) + official R&W (20/30/40) + 10 min, rounded up to 5 —
  Starters 2018 vol1/vol2 55/55, Movers 70/75, Flyers 80/85 (drafts). Explained in
  Russian in `docs/OPERATIONS.md`.

### Added
- **`ANALYSIS_LLM=off|writing`** (default `writing`; no key behaves as `off`). In
  `writing` mode Gemini is asked only about writing items and can never change the
  overall suspicion; in `off` mode no analysis call is ever made. Writing grading and
  per-skill feedback are unaffected.
- **Possible-screenshot weak signal.** The recorder captures only the chord name of
  PrintScreen / macOS Cmd+Shift+3/4/5 / Windows Win+Shift+S (`screenshot_key`, typed
  payload); integrity also derives the "overlay blur" pattern (focus lost without the tab
  being hidden, back within seconds). Always weak — the explanation says it can't be
  confirmed and a phone photo of the screen is undetectable.

### Security
- **Gemini API key no longer leaks.** All three Gemini clients (writing grader,
  feedback, analysis) send the key in the `x-goog-api-key` header instead of the URL
  (`httpx` error text used to carry `?key=…` into logs, `AttemptReport.error`, the stored
  verdict and the dashboard). Per-engine redaction scrubs keys from every logged or
  stored exception string and traceback. **If old logs were shared, rotate the key.**
- **Prompt-injection hardening for the analyst.** Student text and task prompts are
  fenced with a per-request nonce, the system prompt treats fenced content as data, a
  deterministic detector raises `instruction_injection_attempt`, and the LLM verdict can
  never drop below 0.8 × the deterministic formula (`apply_deterministic_floor`).
- **Teacher login fails closed.** `ENVIRONMENT` now defaults to `production`; the dev
  default password / token secret are accepted only with an explicit dev/test
  environment, from a loopback peer, with no forwarding headers — set
  `TEACHER_PASSWORD` and `ADMIN_TOKEN_SECRET` for any tunnel or network use. Login rate
  limit: 10 failures per IP and 30 globally per 5 min, atomic, no per-IP state leak.
- **CSV export escapes formula cells** (`= + - @`, tab, CR/LF), and the dashboard
  escapes every server value (XSS regression test with a hostile student name).

### Fixed
- Integrity extractor drops exact telemetry replays before computing features;
  `telemetry_gap` (new `WritingFeatures` fields) downgrades "text appeared without typing"
  when the recording has holes, instead of raising a false strong flag.
- Recorder bisects a 422-rejected batch and drops only the bad events; payload numeric
  fields have upper bounds.
- Stored LLM verdicts are reused when the event stream is unchanged (fingerprint), and
  the automatic sweep retries a failed report at most 3 times; teacher regrade always
  recomputes. A crashed analysis stores no verdict ("—") instead of a fake clean 0.0.
- Reopen is refused (409) for an attempt still running; reopened rows show "—" for the
  stale suspicion.

### Fixed
- **No duplicate paid finalize.** `finalize_attempt` now holds the per-attempt lock for
  the whole run and re-reads the report INSIDE it (fresh session, plain SELECT): the
  submit/expiry background task, the recovery sweep and "Дооценить все" skip an attempt
  that already has a `complete` report (the sweep also skips a `failed` one inside the
  10-min cooldown), so a sweep/button that read a stale candidate list never pays Gemini
  twice. The sweep also leaves attempts finished < 60 s ago to their background task.
  The explicit single-attempt regrade still always re-runs.
- **Roster shows auto-expired students as finished.** `RosterStatus` gains `expired`
  (free-string column: no migration); `mark_expired` moves the roster entry
  `in_progress -> expired` in the same transaction, the recovery sweep heals older rows
  (`sync_expired_roster_status`), the student picker shows them "(done)" and disabled,
  and the teacher roster labels them "expired (time up)".
- **Lost expiry race returns fresh state.** When another writer wins the
  `in_progress -> expired` race, delivery re-reads the attempt from the DB
  (`get_attempt(..., fresh=True)`) instead of the stale identity-mapped row.
- **Tests never sweep the dev DB.** An autouse fixture sets `SKIP_STARTUP_SWEEP=1`;
  the recovery loop now waits 5 s (interruptible) before its first sweep.
- **Submit results no longer vanish (P0 persistence fixes).** Root causes found in the
  post-season audit and closed in one change set:
  - **Finalize failures are persisted.** `finalize_attempt` used to swallow every
    exception, leaving a submitted attempt with no `AttemptReport` and the dashboard
    stuck on "⏳ оценивается…". It now records a `status="failed"` row with the error
    text (new `AttemptReport.error`, Alembic `d4e5f6a7b8c9`) and the deterministic grade,
    in a second transaction after the rollback. New `ResultsService.finalize_missing`,
    `POST /admin/tests/{id}/finalize-missing` and a **startup sweep thread**
    (`sweep_missing_reports`, skip with `SKIP_STARTUP_SWEEP=1`) recover attempts whose
    background finalize was lost to a restart. A per-attempt lock (`app/results/locks.py`,
    single-process) stops a teacher regrade racing the background finalize.
  - **Gemini calls retry transient failures** (timeouts / 429 / 5xx, 1 s + 3 s backoff,
    60 s timeout) in both the writing grader and the feedback analyst, each retry logged
    with the model id.
  - **SQLite hardening** (`app/core/db.py`): `journal_mode=WAL`, `synchronous=NORMAL`,
    `busy_timeout=10000`, `foreign_keys=ON`, 10 s connect timeout — concurrent student
    saves + telemetry + the background finalize no longer trip "database is locked",
    and dependents cascade on every engine.
  - **Startup migration check** (`apps/api/main.py`): the API refuses to boot when
    `alembic_version` is behind the script head (logs both revisions + `make migrate`;
    override with `ALLOW_UNMIGRATED_DB=1`); a `create_all` dev/test DB only warns.
  - **Fixture reload guard**: `load_test` / `load_fixtures` skip (WARNING) any test that
    already has attempts unless `--force` / `RELOAD_FIXTURES_FORCE=1` — a restart with
    `RELOAD_FIXTURES=1` can no longer wipe a live exam session.
  - **Telemetry ingest** returns 404 for an unknown attempt instead of a 500 FK error
    that dropped the whole batch.
- **Student runner never loses an answer silently** (`apps/web/exam.js`,
  `recorder.js`, `exam.html`): answers go through a dirty-map save queue (latest value
  per item, exponential backoff, re-send if changed in flight, mirrored to
  `localStorage` and resent after a reload), a header indicator shows
  Saved / Saving… / "⚠ N answers not saved — retrying", submit and the timer's
  auto-submit flush debounced text and drain the queue (bounded 5 s) first, and a 409
  resolves "Time's up" vs "Already submitted" from `/state`. The recorder keeps events
  queued until a 2xx, re-queues with backoff, splits batches at 50 KB, truncates
  `answer_change` text over 2 KB (`truncated: true`, head/tail kept), snapshots the
  unsent queue to `localStorage` and resends it on the next load. Audio progress
  reports retry too. Playwright E2E covers both network-blip + reload paths.
- **Abandoned attempts are expired and graded** (`app/delivery`, `app/results`,
  `apps/api`). An attempt whose student closed the tab used to stay `in_progress`
  past its deadline forever (dashboard "pending", never finalized). New
  `DeliveryService.expire_overdue` (+ `AttemptRepository.list_overdue_attempt_ids` /
  conditional `mark_expired`) runs first in `sweep_missing_reports` and the teacher's
  "Дооценить все", in its own committed transaction, before the per-attempt finalizes.
  A lazy expiry from `/state`, `/start` (resume) or a late save/submit now commits
  and schedules the background `finalize_attempt` exactly once per transition (the
  late save/submit still answers 409, same body). The API runs the sweep at startup
  and then every `RECOVERY_SWEEP_INTERVAL_S` (default 300 s) on a daemon thread that
  stops on shutdown (`SKIP_STARTUP_SWEEP=1` disables it). `recorder.js` aborts a
  telemetry fetch with no response after 15 s and retries it with backoff, so a hung
  request can no longer wedge the sender for the rest of the page.

### Changed
- **The dashboard reads the stored cheating verdict — never an LLM on a read**
  (`app/admin`, `apps/web/teacher.*`). The results list, the attempt detail and the
  CSV `suspicion_score` column now take the verdict + integrity profile frozen on the
  `AttemptReport` at finalize (the list in its one batched report query) instead of
  re-running the analysis per attempt on every load / live poll. An attempt with no
  stored verdict (still writing, finalize pending, a pre-`e5f6a7b8c9d0` report) shows
  "—" in the list and sorts last; its detail computes the deterministic formula for
  display only (`verdict_source="live_fallback"`, nothing persisted). "Suspicious
  first" ranks by the stored score. `AdminService` refuses a read-path
  `AnalysisService` that carries an analyst; **Переоценить** / **Дооценить все** stay
  the only admin paths that run an LLM (they recompute and re-store the verdict).
  `AttemptOverview.suspicion_score` / `confidence` are now nullable, plus
  `ai_assist_max` / `verdict_model_id`; `AttemptResult.verdict_source`. The verdict
  still never moves a score (golden rule #2).
- **Teacher dashboard is Russian and fails loudly** (`apps/web/teacher.*`,
  `apps/api/admin.py`, `app/admin`): every action runs through one `guard()` error
  path that ends in a dismissible toast (the API's `detail` is Russian), a 401 signs
  out once, live mode never overlaps a slow poll and refreshes the open detail in
  place, and the results table sorts / searches client-side. `results_for_test` and
  `roster_status` are batched (`_result_rows`: one query each for attempts, reports,
  manual marks and event counts — never per student). Timeline sentences and
  `AdminError` messages are Russian; log lines stay English.
- **Destructive teacher actions are guarded server-side**: deleting a test with
  attempts and unpublishing a test students are writing return 409 unless
  `?force=true` (the UI reads `GET /admin/tests/{id}/stats` first and says how many
  attempts would be wiped); `POST /admin/login` is rate-limited per IP
  (`login_max_failures` / `login_window_seconds` -> 429 + Retry-After) and refuses the
  dev-default password / token secret outside `dev` / `test` (503).
- **Telemetry payloads for the writing events are typed** (`app/telemetry/schema.py`,
  `extra="forbid"`): a payload carrying content (`text`, `data`, …) is rejected with
  422, so "no keys, no clipboard text stored" is structural. Other event types'
  payloads are untouched. `exam.html` discloses that typing patterns (not content)
  are monitored.
- **Teacher dashboard grading status** (`app/admin`, `apps/web/teacher.*`): results rows
  expose `attempt_status`, `report_status ∈ {pending, complete, failed, missing}` and
  `report_error`; chips ⏳ / ✓ / ⚠ ошибка (error in tooltip) / ⚠ нет оценки, a per-row
  **Переоценить** for failed/missing, a test-level **Дооценить все (N)** button, and the
  results table refreshes after a manual mark. `AdminError` now maps to 400
  (validation) / 409 (state conflict) instead of 404 everywhere; a manual grade on an
  unfinished attempt is rejected (409).

### Added
- **AI-assist / outside-help detector for writing tasks (advisory).** Capture ->
  features -> verdict, the three layers kept separate (golden rule #6):
  - **Writing telemetry** (`apps/web/recorder.js`, `exam.js`): `field_focus`,
    `field_blur`, `writing_stats` (additive per-window aggregates by `inputType`,
    keydown count, inter-key histogram, pauses, active typing time, script-dispatched
    inputs), `input_burst`, `paste_attempt` (paste/drop is BLOCKED; only its length is
    recorded) and `fullscreen_exit` — shapes and counts, never text.
  - **`WritingFeatures`** per open_writing item in `IntegrityProfile.writing`
    (`app/integrity/writing.py`, pure + deterministic): typed vs non-keyboard vs
    unexplained chars, paste attempts, typing speed, time away while writing,
    fullscreen exits, text statistics and seven threshold flags
    (`text_appeared_without_typing`, `scripted_input`, `large_paste_attempt`,
    `typing_speed_implausible`, `away_while_writing`,
    `fullscreen_exit_while_writing`, `no_input_telemetry`). Facts, not guilt.
  - **`GeminiAnalyst`** (`app/analysis/llm_gemini.py`): httpx REST, JSON-schema
    output, temperature 0, retries on timeout / 429 / 5xx, logs model id + tokens +
    latency (rule #8). It gets the profile, raw flagged segments, the writing tasks +
    texts, the level and the objective % — never an item grade — and returns a
    Russian summary plus a per-writing-item `WritingAssessment`
    (`ai_assist_likelihood`, `signals`, `rationale`); likelihoods are capped at 0.95.
  - **Deterministic fallback verdict** (`app/analysis/fallback.py`, `model_id=None`):
    a documented noisy-OR over the integrity flags, used when no `GEMINI_API_KEY` is
    set or the analyst fails (then with `AnalysisVerdict.error`). The feature works
    offline and an LLM outage never breaks a finalize.
  - **Stored once at finalize**: `app/results` computes the profile + verdict after
    grading and persists both on `attempt_reports` (`integrity`, `verdict` JSON
    columns, Alembic `e5f6a7b8c9d0`; contracts `AttemptReport.integrity` / `.verdict`,
    new `WritingFeatures` / `WritingAssessment` + regenerated JSON Schema). An analysis
    failure never fails or changes the grade (golden rule #2).
- **"Анализ честности (рекомендательно)" in the attempt detail** (`apps/web/teacher.*`):
  suspicion / confidence bars, the source ("Gemini <model>" or "формула без LLM"),
  the Russian summary, flags translated through one table covering every flag the
  integrity / analysis code emits (unknown model flags fall back to the raw code),
  the analyst `error` note, and a card per writing task — likelihood
  (низкая / средняя / высокая), signals, rationale and the key features (typed vs
  appeared-without-typing chars, paste attempts + max length, typing speed, time
  away, fullscreen exits). A one-line disclaimer says the verdict is advisory and
  never changes the grade; the timeline and the raw events sit right below it
  (golden rule #6). The results table shows the suspicion as a coloured chip and a
  "✍ AI?" marker when any writing task's likelihood is >= 0.6.
- **Writing telemetry in the human timeline** (`app/admin`): Russian sentences for
  the new events — blocked paste / drop with its length, non-keyboard and scripted
  inserts, fullscreen exits, field focus / blur — with `writing_stats` flushes folded
  into one "набор текста" line per focus session. The raw events stay complete.
- **Teacher dashboard tools** (`app/admin`, `apps/api/admin.py`, `apps/web/`): CSV
  export `GET /admin/tests/{id}/results.csv` (UTF-8 BOM, per-skill columns, stored
  data only); a printable A4 student / parent report (`teacher-report.html`,
  `GET /admin/results/{attempt}/report`) with scores, per-skill feedback and writing
  marks and NO integrity data; bulk roster add (`POST /admin/tests/{id}/roster/bulk`,
  one name per line, duplicates reported); re-open with extra time
  (`POST /admin/roster/{id}/reopen` `{extend_minutes}` — a teacher override, never a
  paused timer) with a "переоткрыт — ждёт сдачи" status instead of the stale grade.
- **Grade-once-on-submit + per-skill feedback report (perf fix for the dashboard).**
  Grading used to run on *every* dashboard read — including a synchronous Gemini call
  per `open_writing` item — and `results_for_test` recomputed the full per-attempt
  detail (triple event load, full replay build) for every student, re-fired every 5 s
  by live mode. Now an attempt is **finalized once**: a new `app/results` engine grades
  it (objective + Gemini writing) and a new `app/feedback` engine asks Gemini for a
  Russian per-skill strengths/weaknesses report (reading / listening / writing), and
  the result is persisted as a frozen `AttemptReport` (new `attempt_reports` table,
  Alembic `c3d4e5f6a7b8`). The delivery submit endpoint schedules this as a background
  task (committing the submit first so SQLite can't deadlock the task against the
  request's own transaction); the student sees "submitted" instantly. The dashboard
  now **reads the stored report** — the live overview is a single batched query with
  no LLM and `COUNT(*)` event counts; the read-path grader is LLM-free. A teacher
  **"Переоценить"** button (`POST /admin/results/{attempt}/regrade`) re-runs the
  one-shot finalize for pre-feature attempts or a failed feedback LLM. New contracts
  `AttemptReport` / `SkillReport` + `ItemGrade.feedback`; the attempt detail shows the
  skill analysis and a grading-status chip. Engine boundaries hold (golden rules #2/#6):
  grading never reads integrity, feedback never reads a cheating signal, and a feedback
  verdict never moves a score (its numbers come straight from the `GradingResult`).
- **Manual grading (teacher marks, persisted)** — the attempt detail now has a
  **Writing — read & grade** section (each `open_writing` as its own card: task +
  content points + the student's full text + a 0–5 mark selector) and a **✓/✗ override**
  on typed `gap_fill` rows (auto fuzzy-match can miss an acceptable spelling). Marks
  persist in a new `manual_grades` table (Alembic `a1b2c3d4e5f6`) and are overlaid onto
  the auto score via the pure `GradingService.apply_override` — grading stays auto-only
  per its boundary, and a cheating signal still never moves a score (golden rule #2). New
  `ManualGrade` contract + `ManualGradeRow`; `PUT /admin/results/{attempt}/grades/{item}`;
  reset/remove wipe an attempt's marks. Writing shows its 0–5 score (not pass/✗); objective
  items show ✓/✗ from points. New `ItemResponse` fields `bullet_points` / `word_min` / `manual`.
- **Human-readable activity timeline** (`app/admin`, `apps/web/teacher.*`): the attempt
  detail now shows a decoded, chronological "what they did" list next to the raw event
  JSON — each event as a plain sentence with the question it touched ('Q1: answered
  "C — …"', 'Left the exam tab', 'Returned (away 9s)', 'blocked paste'), `single_choice`
  indices de-shuffled to the option the student saw. Tab-leave/focus/device events are
  highlighted. The raw `events` stay as the machine/script view (now collapsed under a
  `<details>`). New `TimelineEntry` DTO; review lookups refactored into a shared
  `_ReviewContext`. Capture only — it judges nothing (golden rule #6).
- **Device / session forensics** (telemetry → integrity → dashboard, no migration).
  The recorder captures a `device_info` fingerprint at each (re)start — user-agent,
  platform, screen, viewport, time zone, language, CPU/memory, touch — and the
  telemetry ingest **server-stamps the client IP** onto it (server-observed, never
  trusted from the body, like `server_ts`). The integrity extractor adds deterministic
  `device_count` / `device_changed` to `IntegrityProfile` (a resume on a different
  machine flags a switch — capture→feature, judges nothing, golden rule #6). The
  dashboard shows the device/IP captures and a "device changed" warning. New
  `EventType` `device_info` + `DeviceCapture` DTO; the student start screen now carries
  a monitoring disclosure. Schema regenerated; no Alembic migration (event `type` is a
  free string column, the profile/DTOs aren't persisted).
- **Student controls** (`app/admin`, `apps/web/teacher.*`): **reset attempt** (wipe an
  attempt + its answers/events and return the entry to `not_started` for a genuine
  retake), **remove student** (delete a roster entry + its attempt), and **live
  monitoring** (auto-refresh the roster/results every 5 s). New repository deletes
  (`delete_attempt` / `delete_roster_entry`, explicit dependents — SQLite has no FK
  cascade) + admin endpoints `POST /admin/roster/{id}/reset`, `DELETE /admin/roster/{id}`.
- **Teacher dashboard — per-question review & test vetting** (`app/admin`, `apps/web/teacher.*`).
  - **Attempt breakdown**: the attempt detail now lists every question with the student's
    answer vs the answer key, ✓/✗, points, the **full writing text** for `open_writing`, and
    the **change history** ("tried: B → C") decoded from the `answer_change` replay
    (`single_choice` displayed indices are de-shuffled back to canonical via the attempt
    seed, mirroring `delivery._default_bank`). New read-side DTOs `ItemResponse` /
    `ResponseChange` on `AttemptResult` (composed from contracts; nothing persisted, no
    migration — golden rule #4). Teacher surface, so it carries `correct` (rule #1 forbids
    that only on the student runner); no integrity signal touches a score (rule #2).
  - **View test**: an Item-bank "View test" action renders the full authored test
    (stimuli, audio players, options, answer key) so a teacher can vet composition before
    approving — the human gate (rule #5) is no longer blind to content.

### Changed
- **Listening UX rework (student runner)** — the runner now renders one **section
  (= part)** at a time with all its questions, grouped into **Reading / Writing /
  Listening** blocks (skill pill + part label) so students always know which paper
  they're in. A **question navigator** shows answered (filled) vs unanswered (empty)
  squares per part and jumps on click. Listening is now **one continuous recording**:
  the `<audio>` lives at the block level (`#audioBar`, built once), so moving between
  parts never stops or restarts it; the main track is **locked** (single play, no
  pause/seek/restart) and an optional **sound-check** (`preview_asset_id`) is freely
  replayable before the test track starts. (`apps/web/exam.html`, `apps/web/exam.js`.)
- **Contract — `AudioAssetStimulus`** (`contracts/content.py`): default `plays` is now
  **1** (Cambridge recordings bake in the double play); added optional
  `preview_asset_id` (replayable opening explanation / sound-check) and `locked`
  (main track can't be paused/seeked/restarted once started). Additive, inside the
  `stimulus` JSON column → JSON Schema regenerated, **no Alembic migration** needed.
  Stimulus is shared by the authoring and client families, so the keyless projection
  is unchanged (golden rule #1).

### Added
- **Phase 12 — Wire-up, seed, E2E, dockerize** (the final phase).
  - **Asset-serving route** (`apps/api/assets.py`) — `GET /assets/{asset_id}` streams
    stimulus blobs (listening mp3 / option & sign images) through the shared
    `StorageBackend` (`FilesystemStorage` now, MinIO later), closing the Phase-11 known
    gap so the runner's `<img>`/`<audio>` resolve. Unauthenticated like the rest of
    delivery — assets are stimulus content, never the answer key (golden rule #1) — with
    extension-then-magic-byte content typing and 404 for missing/traversal-unsafe ids.
    Tests (`tests/test_assets_api.py`, 4).
  - **Demo seed** (`app/content/seed.py`, `make seed`) — loads one **human-approved**
    A2 Key demo test (the shape ingestion emits: all four item types + a listening
    section) with self-contained asset bytes (generated WAV + PNG) written through the
    storage backend, then **explicitly publishes** it (the golden-rule-#5 approval step)
    and builds a roster. Idempotent. Tests (`tests/test_seed.py`, 4).
  - **Dockerization** (`Dockerfile`, `docker-compose.yml`, `.dockerignore`) — full local
    stack: **api + postgres + redis + minio + arq worker**, all unbuffered so every
    service streams clean logs to `docker compose` (CLAUDE.md logging rules). The api
    boots with `alembic upgrade head` → seed → uvicorn, so the runner has content
    immediately. `app/ingestion/worker.py` is the arq `WorkerSettings` entry point (the
    only module importing `arq`; the job stays arq-free) and starts healthy even without
    `ANTHROPIC_API_KEY`/ASR backends, logging that ingestion jobs need them.
  - **Full honest-vs-cheat E2E** (`tests/test_e2e_full.py`) — boots the seeded API and
    drives two real attempts through real browser telemetry (recorder.js → ingest →
    integrity → analysis): an honest path (human pauses, never leaves the tab) and a
    cheat path (background the tab, return, answer instantly per question). The gate
    holds: the cheat attempt's `suspicion_score` is higher than the honest one's and
    non-trivial (systematicity + `fast_post_return` flags), honest stays near zero, and
    the advisory verdict never carries a score (golden rule #2). Skips cleanly without
    Chromium.
  - **Wiring/config**: `apps/api/main.py` includes the assets router; `Settings` gains
    `redis_url` + `minio_*`; `requirements.txt` adds `redis`/`minio`; `arq`/`minio` are
    mypy-ignored (they live only in the worker image).
- **Phase 11 — Student runner frontend + delivery HTTP surface** (`apps/api/delivery.py`,
  `apps/web/exam.*`).
  - **Delivery router** (`apps/api/delivery.py`) — the exam runtime's browser-facing API,
    wiring the existing `DeliveryService` (no schema/engine change). Unauthenticated
    behind the per-test share link, students are identified by `roster_entry_id`:
    `GET /exam/tests/{id}/roster` (pick-your-name), `POST /exam/roster/{id}/start`
    (create-or-resume), `GET /exam/attempts/{id}/test` + `/items/{item_id}` (keyless
    `Client*` projections — golden rule #1), `GET /exam/attempts/{id}/state` (the
    server-authoritative timer — golden rule #3), `PUT .../answers/{item_id}`
    (displayed → canonical), and `POST .../submit`. `DeliveryError` subclasses map to
    distinguishable HTTP codes (404 / 403 window / 409 expired-or-finalized).
  - **Exam window is permissive this phase** (`_ALWAYS_OPEN`): there is no scheduled
    window on the authoring `Test`, so the per-attempt deadline is driven purely by
    `duration_minutes`. Scheduled windows would live on the `Test` contract later.
  - **Student runner** (`apps/web/exam.html` + `exam.js`): same-origin SPA —
    pick-your-name landing, **refresh-safe resume** (active attempt kept in
    `localStorage`, re-hydrated from `/state`), **one-at-a-time** rendering of all four
    item types, a **display-only countdown** reconciled against `/state` every 15 s that
    **auto-submits at zero** (the server has the last word on expiry — golden rule #3),
    and a listening **audio player with a per-stimulus replay limit** (`plays`, default 2).
  - **Client-side integrity** (capture, never judge — golden rule #6): requests element
    fullscreen on the first gesture where supported (**skipped on iOS**, degrades
    gracefully), blocks copy/cut/paste/contextmenu, and wires the Phase-6 `recorder.js`
    so visibility/blur/answer-change/audio events reach the append-only ingest sink.
    The runner only ever consumes keyless payloads (golden rule #1).
  - **Wiring**: `apps/api/main.py` now includes the delivery router alongside telemetry
    (Phase 6) and admin (Phase 10).
  - Tests (`tests/test_delivery_api.py`, 5): roster lists names with no answer key; the
    full start → serve-keyless → save (displayed→canonical) → submit flow; refresh
    resumes the same attempt; a crossed deadline expires the attempt server-side and a
    late submit is rejected (409); unknown attempt → 404.
- **Phase 10 — Admin (teacher) API + dashboard** (`app/admin/`, `apps/api/admin.py`,
  `apps/web/teacher.*`).
  - `AdminService` — the teacher-facing **composition root**: bank management
    (list/get/delete tests), the **review-queue publish gate**, roster management +
    live status, and a results view. The one engine allowed to hold grading *and*
    integrity/analysis at once, because the dashboard shows them side by side — it
    only *bundles* them and never feeds one into the other (golden rule #2).
  - **Review queue → approve** is the single `draft → published` path (golden
    rule #5). `approve(test_id)` refuses anything not currently a draft, so the
    human-approval gate can't be a silent no-op; `unpublish` reverts published→draft
    with the same status guard. This is where Phase 9's ingested drafts get consumed.
  - **Results** — `attempt_result(attempt_id)` returns the deterministic score, the
    advisory `AnalysisVerdict`, the integrity `IntegrityProfile`, the event count, and
    the **raw event replay** so the verdict is auditable next to the evidence (rule #6).
    `results_for_test(test_id)` ranks attempts **most-suspicious-first** — reordering
    rows only, never touching a score (rule #2).
  - **Auth** (`auth.py`): a single shared teacher password mints a short-lived
    **HMAC-signed bearer token** (`TokenSigner`, constant-time verify + expiry); no user
    table / migration. Every admin route requires it via `require_teacher`;
    `fastapi-users` is the documented multi-teacher upgrade path.
  - **Read-side DTOs** (`models.py`: `ReviewDraft`/`RosterStatus`/`AttemptOverview`/
    `AttemptResult`) compose existing contracts and persist nothing — no schema change
    (mirrors `app/ingestion/models.py`; golden rule #4). Engine contract in
    `app/admin/CLAUDE.md`.
  - **Teacher dashboard** (`apps/web/teacher.html` + `teacher.js`): same-origin SPA —
    sign in, approve drafts, manage roster, and browse results ranked suspicious-first
    with a raw-replay detail view next to the advisory verdict.
  - Every admin action logs at INFO (approve/delete/add_student/results reads, login);
    auth failures at WARNING.
  - New settings: `teacher_password`, `admin_token_secret`, `admin_token_ttl_seconds`,
    `assets_dir` (wires `FilesystemStorage` into the admin service).
  - Tests (`tests/test_admin.py`): auth enforced (401 without/with bad/expired token;
    login→token works); approve flips draft→published and is the only publish path;
    `unpublish` guarded; results endpoint returns score + verdict + event count;
    suspicious-first ranking with score independent of suspicion (rule #2); admin
    actions logged.
- **Phase 9 — Ingestion engine** (`app/ingestion/`).
  - `IngestionPipeline.run(request)` — a **pure orchestrator** over injected seams that
    turns a question-paper PDF + answer-key PDF (+ optional listening mp3) into a
    validated **draft** `contracts.Test`: (a) extract text + image crops, (b) structure
    into a `DraftTest` via the LLM, (c) parse the answer key and merge by question
    number to set authoritative `correct`/`accepted`, (d) ASR + align listening items to
    audio spans, (e) validate. Each step logs progress to cmd.
  - `IngestionService.ingest(request)` — runs the pipeline, stores the image crops as
    assets via the content `StorageBackend`, and persists the `Test` as a **draft**
    through `ContentService` (the review queue). It **never publishes** —
    `draft → published` is a human-approved Admin/Phase-10 action (golden rule #5).
  - **Draft models** (`models.py`): `DraftTest`/`DraftSection`/`Draft*Item` mirror the
    authoring items but **carry no answer key** — the LLM is structurally incapable of
    inventing `correct`. `answer_key.parse_answer_key` + `merge_key` are the only path
    that sets it, from the answer-key PDF (authoritative, golden rule #5). Both are pure;
    a keyed item with no key entry or a `correct` not among its options raises
    `ValueError` — a malformed extraction is rejected rather than published.
  - **Seams + mocks** (Protocol + Mock + lazy real impl, mirroring grading/analysis):
    `PdfExtractor`/`MockPdfExtractor` (real `PyMuPdfExtractor`, lazy `fitz`),
    `LLMStructurer`/`MockLLMStructurer` (real multimodal `AnthropicStructurer`, lazy
    `anthropic`, `messages.parse` structured output, logs id+tokens+latency, golden
    rule #8), `Asr`/`MockAsr` (real `WhisperXAsr`, lazy `whisperx`). None are re-exported
    from `__init__.py`, so importing the package pulls no optional/heavy backend (an AST
    import guard asserts this).
  - `align_items_to_spans` — pure, deterministic mapping of each listening item to an
    `AudioSpan`. The span is **ingestion-internal metadata** (carried in `IngestionResult`,
    not a `contracts/` field) so no schema change/migration is needed this phase
    (deferred to Admin/Phase-10, same pattern as Phases 5/7/8).
  - `jobs.ingest_pdf` — an `arq` task entry point (arq-free at import time); `cli.py`
    (`python -m app.ingestion.cli`) wires the real seams for `make ingest path=… key=…
    audio=…` and prints each pipeline step. Engine contract in `app/ingestion/CLAUDE.md`.
  - Tests (`tests/test_ingestion.py`): a golden extract→structure→merge yields the
    expected section/item counts & types; the key merge fills `correct`/`accepted` +
    misspelling variants; a missing or out-of-range key is rejected; the result is always
    `draft` (never published); ASR alignment is deterministic and in range; crops are
    stored as assets; an AST import guard over the public surface.
  - Deps: `arq`, `pymupdf` added to `requirements.txt` (whisperx noted, install
    per-platform); `fitz`/`whisperx` `ignore_missing_imports` mypy overrides.
- **Phase 8 — Analysis engine** (`app/analysis/`).
  - `AnalysisService.analyze(attempt_id)` — layer 3 of the integrity pipeline
    (`telemetry` → `integrity` → **`analysis`**, golden rule #6). It consumes the
    Phase 7 `IntegrityProfile` via `IntegrityService` (it does **not** re-derive
    features) plus the raw event segments behind it, and asks an injected
    `AnalysisLLM` for an advisory `AnalysisVerdict` (`suspicion_score`, `confidence`,
    `flags[]`, `summary`, `model_id`). With no analyst injected it returns a neutral
    zero-suspicion verdict and logs a WARNING (mirrors grading's no-grader fallback).
  - `flag_segments(profile, events)` — a **pure, deterministic** selector of the raw
    segments worth surfacing: long bounded hides (≥ `LONG_HIDDEN_MS`) and fast
    post-return answers (≤ `POST_RETURN_FAST_MS`, reused from integrity), each
    carrying the raw `IntegrityEvent`s in its window so the teacher's replay stays
    auditable next to the verdict (rule #6).
  - `AnalysisLLM` protocol + `MockAnalysisLLM` (deterministic blend of systematicity
    + total hidden time, monotone so a cheatier profile scores higher) for the
    verdict path; tests inject the mock.
  - Real `AnthropicAnalyst` (`llm_anthropic.py`) — lazy `anthropic` import, structured
    output via `messages.parse` with adaptive thinking, clamps the model's
    `suspicion_score`/`confidence` into `[0, 1]`, and logs model id + token usage +
    latency (golden rule #8). Not re-exported from `__init__.py`, so importing the
    package never needs the SDK.
  - **Advisory only — never touches a score (golden rule #2):** the engine imports no
    `app.grading` (asserted via an AST import scan); `AnalysisVerdict` carries no
    score, and a behavioural test confirms a re-grade is byte-for-byte identical
    after analysis runs. Being the LLM layer, it MAY import `anthropic` (unlike
    telemetry/integrity). Nothing is persisted (mirrors Phase 5/7; storing the verdict
    with the attempt is an Admin/Phase-10 concern). Engine contract in
    `app/analysis/CLAUDE.md`.
  - Tests (`tests/test_analysis.py`): segment-flagging goldens (incl. short-hide and
    honest-stream negatives); the mock's determinism, range, and monotonicity; the
    no-analyst neutral fallback; the service reading through the repo + integrity and
    validating against the schema; the no-score-field + grade-untouched invariants;
    and the import-graph guard.
- **Phase 7 — Integrity engine** (`app/integrity/`).
  - `extract_profile(attempt_id, events)` — a **pure, deterministic** reducer from an
    attempt's `IntegrityEvent` stream to an `IntegrityProfile`: per-question latency +
    interaction count, hidden intervals + durations (`total_hidden_ms`), the
    post-return pattern (`visible -> answer` latency), per-question pacing coefficient
    of variation, and a systematicity rate across questions. Events are ordered by
    trusted `server_ts` with the ingest index as a stable tie-breaker, so the same
    events always yield the same profile regardless of input order.
  - `IntegrityService.profile(attempt_id)` reads the append-only stream via
    `EventRepository.list_events` (telemetry owns the raw stream; integrity only reads
    it — it does not import `app/telemetry`) and logs the profile summary at INFO.
  - Feature rules: a "question" item is any `item_id` carrying an `interaction`/
    `answer_change` event (audio events reference a *section*, never a question);
    latency = first-answer − first-sighting (unanswered → 0, excluded from pacing);
    `post_return_latency_ms` is set only when the last visibility transition before the
    answer was `visibility_visible`; `pacing_cv` is population std/mean of answered
    latencies; `systematicity_rate` is the fraction of questions answered within
    `POST_RETURN_FAST_MS` (2000 ms) of returning to the tab.
  - **Deterministic, no LLM, judges nothing about guilt (golden rules #2, #6):** the
    engine imports no `app.grading` / `app.analysis` / LLM SDK (asserted via an AST
    import scan). The advisory verdict is Phase 8 (`app/analysis`). Nothing is
    persisted (mirrors Phase 5). Engine contract in `app/integrity/CLAUDE.md`.
  - Tests (`tests/test_integrity.py`): synthetic event-stream goldens for every
    feature; threshold boundary; audio/unanswered/unclosed-hide/empty edge cases;
    determinism under reordering; the service reading through the repository; and the
    import-graph guard.
- **Phase 6 — Telemetry engine** (`app/telemetry/` + recorder in `apps/web/`).
  - `TelemetryService.record_batch(attempt_id, batch)` appends a batch of recorder
    events to the append-only store via `EventRepository`, which stamps the trusted
    `server_ts` and logs each event at **WARNING** (golden rule: integrity events
    surfaced loudly). `list_events` reads the stream back in ingest order.
  - Ingest transport (`schema.py`): `EventBatch` of `ClientEvent`. `ClientEvent`
    has **no `server_ts` field** (`extra="forbid"`), so a client structurally cannot
    forge the trusted server timestamp — the same way delivery's `Client*`
    projections make "no answer key to the client" structural.
  - HTTP: `POST /attempts/{attempt_id}/events` (`apps/api/telemetry.py`), append-only,
    returns `{ingested: n}`. `apps/web` is served as static assets so the recorder
    posts same-origin.
  - Browser recorder (`apps/web/recorder.js`, vanilla JS, no build step): captures
    `visibility_hidden`/`visibility_visible` (+hidden duration), `window_blur`/
    `window_focus`, `pagehide`, per-question `interaction`, `answer_change`, and
    `audio_play`/`audio_seek`. Batches on a timer and flushes synchronously via
    `navigator.sendBeacon` on hide/pagehide; telemetry failures never break the UI.
  - **Capture only — no judgment (golden rule #6):** the engine imports no
    `app.grading` / `app.integrity` / `app.analysis` (asserted via an AST import
    scan). Feature extraction (Phase 7) and the LLM verdict (Phase 8) are separate
    layers. Engine contract in `app/telemetry/CLAUDE.md`.
  - Tests: batch persists with both `client_ts` and a server-stamped `server_ts`;
    append-only accumulation; one WARNING per event; a posted `server_ts` is
    rejected (422); import-graph cleanliness; **Playwright E2E** drives the real
    recorder in headless Chromium, backgrounds the tab, and asserts a
    `visibility_hidden` event reaches the store (skips if Playwright is absent).
    Added dev deps: `playwright`, `pytest-playwright`.
- **Phase 5 — Grading engine** (`app/grading/`).
  - `GradingService.grade(attempt_id)` reads the `Attempt`, its canonical `Answer`
    rows, and the authoring `Test`, grades every item, and assembles a
    `GradingResult` (one `ItemGrade` each + `score` / `max_score` / `needs_review`).
  - Deterministic graders (`deterministic.py`, pure & reproducible): `single_choice`
    and `matching` compare the canonical response to the authored `correct`;
    `gap_fill` normalizes (`normalize.py`: casefold + whitespace) then matches
    `accepted` -> `accepted_variants` -> a rapidfuzz ratio (default 85, configurable)
    for unlisted acceptable misspellings, skipping the fuzzy step for short answers.
    One point per objective item.
  - `open_writing` graded behind an injected `LLMGrader` protocol (`llm.py`):
    `MockLLMGrader` is deterministic for tests; the real `AnthropicWritingGrader`
    (`llm_anthropic.py`, lazy SDK import, kept out of the package `__init__`) uses
    structured outputs on `claude-opus-4-8` and logs model id + token usage +
    latency (golden rule #8). `grade_mode="manual"` (or an llm item with no grader
    injected) routes to needs-review instead of guessing.
  - Manual override (`apply_override`) regrades one item as `open_writing_manual`,
    clears its `needs_review`, and recomputes totals (returns a new result, no
    mutation); `review_queue` lists items a human must finish.
  - Golden rule #2 held: the engine imports no `app/telemetry` or `app/integrity`
    (asserted in tests); a cheating signal can never move a score. Engine contract
    in `app/grading/CLAUDE.md`. Added deps: `rapidfuzz`, `anthropic`.
  - Tests: objective goldens; gap_fill normalization + acceptable-misspellings +
    threshold; writing via a mocked grader (llm / manual / no-grader fallback);
    score assembly, review queue, manual override; import-graph cleanliness.
- **Phase 4 — Delivery engine** (`app/delivery/`).
  - `DeliveryService` drives the attempt lifecycle: `start` (validate exam
    window + grace, then **create-or-resume** — a roster entry never gets a
    second attempt; reopening the link resumes), serve (`client_test` /
    `serve_item`), `save_answer`, `get_state` (server timer), `submit`.
  - `projection.py`: pure authoring -> `Client*` projection that strips the answer
    key and applies the per-attempt `OptionShuffle` to `single_choice` options —
    the enforcement point for golden rule #1 (no `correct` to the client).
  - The per-attempt `AttemptLayout` is recomputed from `Attempt.seed`
    (`derive_seed(test_id, roster_entry_id)`) on every call, so a resume serves
    the identical draw; `bank_builder` is injectable (default: one singleton pool
    per section, delivering the whole authored test plus option shuffle).
  - `ExamWindow` (opens/closes + `grace_seconds`, which extends the close);
    deadline = `min(started_at + duration, window close)`, fixed at start. The
    timer is server-authoritative and never pauses (golden rule #3): `get_state`
    flips a crossed-deadline attempt to `expired`; late `save_answer`/`submit`
    raise `AttemptExpiredError`. `save_answer` maps the displayed option index
    back to the canonical key before persisting.
  - `DeliveryError` hierarchy (window / expiry / state / not-found) so callers can
    distinguish rejection modes. Full lifecycle logs at INFO.
  - `AttemptRepository` extended with `update_attempt` / `update_roster_entry`
    (write-back of existing columns). Engine contract in
    `app/delivery/CLAUDE.md`.
  - Schema change (golden rule #4): a **unique constraint** on
    `attempts.roster_entry_id` enforces one attempt per roster entry at the DB,
    so a concurrent double-start fails loudly instead of forking two attempts;
    `start()` catches the violation and resumes the winner. Migration
    `25c1f9debd77_attempt_unique_per_roster_entry`.
  - Tests: served payload has no `correct`; window/grace enforced; refresh
    resumes the same attempt; displayed->canonical round-trip; timer expiry; late
    submit/save rejected.
- **Phase 3 — Content engine** (`app/content/`).
  - `ContentService` facade for item-bank CRUD (create/read/list/publish/
    unpublish/delete) plus asset storage, built on the existing
    `ContentRepository`/`AttemptRepository` (extended with `list_tests`,
    `set_status`, `delete_test`, `list_roster_entries`).
  - `StorageBackend` interface + `FilesystemStorage` for asset blobs keyed by
    `asset_id` (MinIO slots in later); asset ids are validated against path
    traversal.
  - Pooling (`pooling.py`): `TestBank`/`SectionPool` bank shape and a pure,
    seed-driven `build_attempt_layout` producing the per-attempt `AttemptLayout`
    (drawn `section_ids` + per-item `OptionShuffle`). Randomization is at the
    section-pool level plus a `single_choice` option shuffle; item order within a
    section is never touched (golden rule #7). The permutation is recomputed from
    `Attempt.seed` (not stored as its own column); a SHA-256 keyed RNG gives
    independent per-pool/per-item streams.
  - Roster + assignment (`roster.py`): `RosterService` manages a test's named
    roster; `derive_seed(test_id, roster_entry_id)` gives each student a stable
    seed (reproducible across resumes), constrained to signed 32-bit for
    Postgres portability.
  - Every pooling/assignment decision logs at INFO via the shared logger.
  - Engine contract in `app/content/CLAUDE.md`.
  - Tests: two seeded students get different valid section sets; option
    permutation round-trips displayed-index -> canonical; layout reproducible
    from a seed; storage round-trip + traversal guard; roster/seed assignment.
- **Phase 2 — Persistence & migrations.**
  - `app/core/db.py`: sync SQLAlchemy 2.0 engine + `sessionmaker`, declarative
    `Base`, and `session_scope` / `get_session` helpers. DB URL comes from
    `Settings.database_url` (sqlite by default, Postgres via env) — added
    `database_url` / `db_echo` to `app/core/config.py` and `.env.example`.
  - ORM models in `app/persistence/models.py` mirroring the contracts
    (golden rule #4): normalized `tests`/`sections`/`items` (polymorphic answer-key
    and stimulus parts stored as contract-governed JSON), and flat
    `roster_entries`/`attempts`/`answers`/`integrity_events`. A `UtcDateTime`
    column type normalizes all datetimes to UTC-aware on read/write so the
    server-authoritative timer (rule #3) and event ordering (rule #6) stay
    comparable on sqlite, which otherwise drops `tzinfo`.
  - Thin repository layer in `app/persistence/repository.py`
    (`ContentRepository`, `AttemptRepository`, `EventRepository`) translating
    between contract models and rows, logging at the persistence boundary
    (events logged at WARNING per CLAUDE.md); `EventRepository` is append-only and
    stamps `server_ts` on ingest.
  - Alembic initialized (`alembic.ini`, `migrations/env.py` driven by app settings
    + `Base.metadata`) with the initial schema migration; `make migrate` applies
    clean and `alembic check` reports no drift.
  - Added `sqlalchemy`, `alembic`, `psycopg[binary]` to `requirements.txt`.
  - Tests: create/read round-trips per aggregate against an in-memory sqlite DB,
    section/item order preservation, answer upsert, and append-only event ingest
    with `server_ts` stamping.
- **Phase 1 — Contracts (schemas).**
  - Authoring item-bank contracts in `contracts/content.py`: `Test`, `Section`
    (stimulus union: `passage_text` | `audio_asset` | `image_set` | `gapped_text` |
    `matching_pool`), `Item` (type union: `single_choice` | `gap_fill` | `matching` |
    `open_writing`), and `Option` (text | image) — these carry the answer key
    (`correct`, `accepted`, `accepted_variants`, `rubric`).
  - Parallel student-facing client family (`ClientTest`/`ClientSection`/`ClientItem`/
    `ClientOption`) that is structurally incapable of carrying an answer key, plus
    client options that drop the canonical `key` (golden rule #1).
  - Runtime contracts in `contracts/runtime.py`: `Attempt`, `Answer`,
    `IntegrityEvent`, `GradingResult` (+ `ItemGrade`), `IntegrityProfile`
    (+ `QuestionTiming`, `HiddenInterval`), `AnalysisVerdict`, `RosterEntry`.
  - JSON Schema generation (`contracts/export_jsonschema.py` -> `contracts/jsonschema/`)
    driven by a `REGISTRY`, wired to `make schema`.
  - Tests: valid/invalid fixtures parse/raise; the no-`correct` invariant walks every
    client-facing schema; committed JSON Schema is asserted in sync with the models.
- **Phase 0 — Scaffold & tooling.**
  - Repo layout per `CLAUDE.md` (engine package skeleton under `app/`, `contracts/`,
    `apps/`, `tests/`, `migrations/`).
  - `pyproject.toml` (ruff + mypy + pytest config) and `requirements.txt`.
  - Canonical `app/core/logging.py` (console logger to stdout) and
    `app/core/config.py` (pydantic-settings) + `.env.example`.
  - FastAPI app `apps/api/main.py` with `GET /health` that logs the request via the
    shared logger and returns `{"status": "ok"}`.
  - Pre-commit hooks (ruff, ruff-format, mypy) and one health-endpoint test.
