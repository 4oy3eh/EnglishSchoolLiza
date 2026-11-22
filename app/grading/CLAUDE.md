# CLAUDE.md — grading engine

## Responsibility
Turn an attempt's stored answers into a **score**. Two families:

* **Deterministic** — `single_choice`, `matching` (compare the canonical response to
  the authored `correct`), `gap_fill` (normalize + `accepted` + `accepted_variants` +
  `rejected` + a length-aware edit distance for acceptable misspellings). Pure,
  reproducible.
* **Writing** — `open_writing` graded by an injected `LLMGrader` (rubric-based) or
  routed to manual review. Real LLM impl logs model id + token usage + latency
  (golden rule #8). Tests inject a deterministic mock.

Plus a **manual override** path and a **needs-review queue** (items a human must
finish: every `open_writing` graded manually, anything the LLM flags).

## Inputs / Outputs
- **In:** an `attempt_id`; the `Attempt` + its `Answer` rows (canonical responses —
  delivery already de-shuffled single_choice and resolved the matching pool key);
  the authoring `Test` (for `correct` / `accepted` / `accepted_variants` / `rubric`).
- **Out:** a `GradingResult` (`contracts/runtime.py`) — one `ItemGrade` per item,
  plus `score` / `max_score` / `needs_review`. Nothing is persisted in this phase.

## Must NOT
- **Read or touch integrity data** (golden rule #2). Grading must never import
  `app/telemetry` or `app/integrity`, and a cheating signal must never move a score.
- **See display order.** Responses are already canonical; grading compares keys.
- **Let an answer key reach a caller of the client projection** — grading reads the
  authoring `Test` directly; it is server-side only and never serialized to a student.
- Persist a grades table / add a migration (out of scope for Phase 5).

## Key invariants
- **Deterministic grading is pure & reproducible:** same answers + same authored key
  => same `ItemGrade`. No randomness, no clock, no I/O.
- **Per-item points.** An item's optional `points` (1..20, authoring-only) is its
  weight; None = the historical defaults: one point per objective item
  (`DETERMINISTIC_POINTS`), 5 for open_writing / colour_task (`DEFAULT_WRITING_POINTS`).
  `item_max_points(item)` is the single answer (admin uses it for manual marks).
  Objective items are all-or-nothing (`objective_points`). Writing: when `points`
  differs from the grader's scale, `scale_writing_score` maps the grader's band
  deterministically — `awarded * points / grader_max` rounded HALF-UP to a whole mark,
  clamped to [0, points] (a 2-mark Movers sentence: 5 -> 2, 4 -> 2, 3.5 -> 1, 2.5 -> 1,
  1 -> 0); same scale = the grader's mark verbatim. `awarded` is `0` when unanswered.
  Tests: `tests/test_item_points.py`.
- **gap_fill matching order:** exact (normalized) `accepted` / `accepted_variants`
  -> exact (normalized) `rejected` = wrong -> number equivalence (below; a
  `rejected` form of the same value = wrong, else an accepted one = correct) ->
  typo tolerance against accepted + variants, ONLY when the item's spelling mode is
  "tolerant" (last: a misspelt number word, `canonical_numbers(..., typo_budget)`).
- **Numbers as digits or words** (`numbers.py`, the teacher's global rule — no
  per-item variants needed): `canonical_numbers` rewrites number WORDS into digits
  on both sides, so they compare EXACTLY BY VALUE ("twelve" == "12", "twenty-one" ==
  "twenty one" == "21", "a hundred" == "one hundred" == "100", "one hundred and
  five" == "105"; "seven" != "11", "13" != "30", "fifteen" != "50"). Cardinals
  0..999 (tens + unit by space or hyphen, "a"/unit + "hundred", optional "and"),
  ordinals as the last word ("third" == "3rd", "twenty-first" == "21st",
  "hundredth" == "100th"), and number words inside longer answers ("twelve apples" ==
  "12 apples"). Applies in EVERY spelling mode and with `fuzzy=False` (it is value
  equality, not spelling tolerance). Digits are never rewritten ("05", "7.30"), and
  "thousand"+, fractions and one-word "twentyone" are not parsed. A misspelt number
  word ("twelv", "ninteen", "fourty") counts only in tolerant mode, and only when
  `nearest_number_word` maps it unambiguously: not a number word / known word, same
  first letter, within that number word's own typo budget, and the single closest
  candidate (Damerau-Levenshtein, tie-break Indel: "fourty" -> forty, not fourth;
  any remaining tie = ambiguous = wrong). Exception, the KEY's form decides: when the
  closest candidates are the cardinal and the ordinal of ONE number ("eigth": eight /
  eighth), `prefer=number_form(key)` picks the form the key uses — key "eight" / "8"
  -> eight, key "eighth" / "8th" -> eighth (`grade_gap_fill` tries each key with its
  own form; `_token_typo` passes the key token's form). Ties between different
  numbers are never resolved this way. The same guard applies on the literal-key
  path (`_token_typo` on a number-word key token), so a typo is never accepted for
  a number it does not point at. Debug log: "correct via number equivalence".
  Tests: `tests/test_grading_numbers.py`.
- **Spelling mode** (`spelling_mode(item, skill, level=)`): `GapFillItem.spelling`
  ("exact" / "tolerant", authoring-only — `ClientGapFillItem` has no such field), else
  derived from the section's SCORE skill (`Section.score_skill` = `reported_skill` or
  `skill`) and the test level, both threaded in by `GradingService.grade`: `reading` /
  `writing` / `use_of_english` -> exact (Cambridge A2 Reading Part 5 / B1 Reading Part
  6 open cloze, YLE R&W, B2-C2 Use of English "correct spelling is essential"),
  `listening` -> tolerant at A2 / B1 / YLE / **B2** ("minor spelling errors are not
  penalised") but **exact at C1 / C2** (`EXACT_LISTENING_LEVELS`: "Correct spelling is
  expected at this level" / "must be spelled correctly"; list US + UK spellings in
  `accepted`), no skill -> tolerant (historical behaviour). `GradingService(fuzzy=False)`
  = exact only everywhere.
- **Typographic punctuation** (`normalize.fold_typography`, used by `normalize` and
  `kwt.kwt_tokens`, so responses AND accepted / variants / rejected / KWT halves):
  ’ ‘ ʼ ‛ ′ ´ ` ＇ -> `'`, “ ” „ ‟ « » ″ ＂ -> `"` — a phone's "don’t" equals "don't".
- **gap_fill `max_words`** (open cloze / word formation = 1; B2 listening may use 3):
  a hard rule for an answer that is NOT itself an accepted key (an exact key always
  wins): offering two answers (`/`, `\`, `|`: "in/on") or more words than allowed is
  wrong. Words are split on WHITESPACE (`kwt.answer_word_count`: "7.30", "2,000", "3.5"
  are one word) and counted like Cambridge (contractions = two words — "didn't" is 2 —
  except `can't` = 1 at B2 only; `cannot` 1; a possessive "Tom's" 1, a pronoun "it's"
  2). Punctuation around the answer is ignored for these items (stripped from both ends
  of response + keys).
  `stem_word` (word formation) is display-only — grading never reads it.
- **Key word transformation** (`kwt.py`, `grade_key_word_transformation(item, resp,
  level=)`, method `key_word_transformation`, `points` default 2 = `kwt_points` /
  `item_max_points`): hard rules first, each -> 0 with a Russian `feedback`: blank; two
  answers (`/ \ |`); word count (key word included, contractions as above) outside
  `contracts.content.kwt_word_limits(item, level)` (item `min_words` / `max_words`, else
  B2 2-5, C1 3-6, C2 3-8, other levels 2-5); key word missing or changed (it must be a
  word of the answer, or inside an unambiguous contraction of it: NOT in "didn't").
  Then the halves: the response (casefolded, punctuation dropped, unambiguous
  contractions expanded on both sides: "didn't" == "did not", "can't" == "cannot" ==
  "can not"; `'d` / `'s` compared literally) is tried at EVERY word split point
  against every `KwtAnswer` (part A variants | part B variants); the best number of
  correct halves (0/1/2) counts, `awarded = points * halves / 2`. Spelling strict, no
  typo tolerance. `ItemGrade.feedback` = "2/2: обе части верны" / "1/2: одна часть
  верна, другая нет" / "0/2: …слов 7, допустимо 2–5" etc. (shown on the teacher's
  review row). The teacher override (`apply_override`, 0..max) works as for gap_fill.
- **Writing level scales** (`writing_scales.py` + `llm_gemini.py`): the grader gets
  `level=` (`LLMGrader.grade_writing(item, response, *, level=None)`; Mock / Anthropic
  accept and ignore it). B2 First / C1 Advanced / C2 Proficiency are marked on the
  Cambridge four subscales — Content (common to all levels), Communicative
  Achievement, Organisation, Language — each 0-5 whole marks = 0-20, with the
  handbook descriptors verbatim (`CEFR_DESCRIPTORS`; band 5 = the exam's CEFR level,
  3 = one below, 1 = two below; 4 / 2 "share features"), length guidance (B2 140-190,
  C1 220-260, C2 240-280 / 280-320 — never a hard limit), C2 Part 1's two input texts
  are just the task text. Response schema: four INTEGER marks + feedback; each mark
  clamped / rounded to 0-5, summed; feedback "Content 4 · Communicative Achievement 3 ·
  Organisation 3 · Language 3 = 13/20. …". A2 / B1 / YLE keep the holistic 0-5 band.
  `GradingService._grade_writing` scales the grader's mark onto
  `writing_points(item, level)` whenever they differ: the item's `points`, else **20**
  for an open_writing task of a B2-C2 test (the 0-20 mark verbatim), else 5;
  `item_max_points(item, level)` gives the same number (admin manual marks, the
  choose-one check). `editor.semantic_warnings` logs a WARNING (never refuses) for a
  B2-C2 writing task without `points`. The Anthropic grader is not wired (apps/api and
  app/results build `GeminiWritingGrader`); given a B2-C2 level it logs a WARNING that
  it has no four-subscale prompt. Both paths
  fence the task and the student's text with a per-request nonce (`fence`, an
  engine-local copy of app/analysis's) and log model + level + tokens + latency.
- **Typo rule** (`typo_match` / `_token_typo`): Damerau-Levenshtein ≤ 0 for keys of
  ≤ 4 letters, ≤ 1 for 5-7, ≤ 2 for ≥ 8; a key or answer with a digit is exact;
  multi-word answers are compared word by word (each word within its own budget; a
  different word count only tolerates a missing/extra space). A response word is
  NEVER typo-accepted when it (a) changes the first letter ("goat"/"coat",
  "packet"/"jacket"), (b) is a known word (`vocabulary.is_known_word`: "rain" for
  "train", "boats" for "boots", "stairs" for "stars", "fiend" for "friend"), (c) is
  another form of the key word (`vocabulary.is_inflection_of`: -s/-es/-ed/-ing,
  -er/-est, agentive -r, -ly, -ful/-fully, -y, ordinal -th, -man/-men, two number
  words, two forms of one irregular verb; derivational suffixes only count over a
  KNOWN base, so "stor"/"story" stays a typo question), or (d) is at least as close
  to a `rejected` form. The teacher's manual override is the safety net. Golden
  cases from the real fixture keys: `tests/test_grading_rules.py`.
- **Known-word list** (`vocabulary.py`, loaded once at import, pure afterwards):
  `data/common_words.txt` — ~3,270 everyday A1–B2 English base words, hand-authored
  for this project from general knowledge (function words, numbers, days/months,
  topic vocabulary, common verbs + irregular forms, adjectives, adverbs, confusable
  minimal pairs); NOT derived from any licensed word list, so license-clean.
  `data/bank_words.txt` — every word in `deploy/fixtures/*.json` texts / prompts /
  options / keys, `accepted_variants` excluded (they are deliberate misspellings);
  regenerate with `python -m app.grading.vocabulary` (a test fails when stale). Both
  are expanded with regular forms (`word_forms`: -s/-es, -ed, -ing, -er, -est with
  e-drop / y->i / one-syllable CVC doubling only — so "stoped", "runing",
  "begining", "potatos", "nineth" are never "known"). Tests added later through the
  editor are not in the bank list; the common list + rule (a) carry them.
- **Choose-one writing (B1 Writing Part 2):** the `optional` open_writing items of
  one section are a group that counts exactly ONE task toward `score` AND
  `max_score` (`_grade_choose_one`). One written -> graded, the blank ones get NO
  grade (downstream treats a missing grade as "the unchosen task"); several written
  -> all graded, the highest counts (tie: first), every other one is stored with
  `awarded=0, max_points=0` (`ItemGrade.max_points` allows 0 for exactly this),
  `needs_review=True` and feedback "не засчитано: выбрано одно задание (оценка x/y)";
  none written -> the first one counts 0 of `DEFAULT_WRITING_POINTS`,
  `needs_review=False`.
- **Writing routing:** `grade_mode="manual"` -> needs-review (awarded 0, pending);
  `grade_mode="llm"` -> the `LLMGrader`; with no grader injected an llm item falls
  back to needs-review rather than guessing.
- **Manual override** replaces one `ItemGrade` (method `open_writing_manual`), clears
  its `needs_review`, and recomputes the result totals. Returns a new result; inputs
  are not mutated.
- **Choose-one switch** (`apply_override` on a group with an uncounted task): a mark
  on any task of the group is out of the group's full maximum (the counted task's
  `max_points`, else `DEFAULT_WRITING_POINTS`). Marking the UNCOUNTED task makes it
  the counted one; the other becomes uncounted (`awarded=0, max_points=0`,
  `needs_review=False`, feedback "не засчитано: выбрано другое задание (оценка
  x/y)" keeping its previous mark). With several stored teacher marks in the group
  the most recently marked (`ManualGrade.graded_at`, tie: first in the section)
  counts, so the admin overlay is order-independent. This is the one place the
  override reads the repositories (the test's groups, cached per test id, and the
  attempt's stored marks) — and only for a result that holds an uncounted writing
  task, so ordinary attempts cost no query. Admin `set_manual_grade` /
  `_item_max_points` return 5 for every task of the group (`_choose_one_max`); the
  dashboard card and the print report show the uncounted task as "не засчитано
  (выбрано другое задание)", never "0 / 0".

- **mark_toggle (IQ, `mark_toggle.py`, method `mark_toggle`).** Graded per SECTION
  (`grade_mark_toggle_section`, called by `GradingService.grade` for the section's
  mark_toggle rows): response "1" = marked, anything else ("0", "", none) =
  unmarked. A `correct=True` row is worth 1 and earns it when marked; a
  `correct=False` row is worth 0 (`max_points=0`), but every FALSE mark cancels one
  correct mark of the same section, floor 0 (book: "1 point per correct X";
  marking everything earns 0). Section raw = max(0, hits − false marks). Recorded
  per item so `score` stays the plain sum: the false-marked row gets feedback
  "ложная отметка: −1 …"; the cancelled rows are the LAST correctly marked rows in
  authored order (deterministic): `awarded=0, max_points=1`, feedback "погашено
  ложной отметкой (<id>)". `item_max_points` = 1 / 0. An override replaces one
  row's grade like any objective item. Tests: `tests/test_iq_family.py`.
- **drawing (IQ, `drawing.py`, method `drawing`, `points` default 1,
  all-or-nothing).** Pure and deterministic (no ML / LLM). Response JSON
  `{"w","h","strokes":[[[x,y],...],...]}` in canvas px (canvas = the picture).
  `region`: strokes resampled; ink in PICTURE WIDTHS; correct when ink ≥ `min_ink`,
  inside-zone share (zones dilated by `margin`) ≥ `min_inside_ratio`, and (with
  `require_all_zones`) each zone ≥ min_ink / zones. `lines`: strokes mapped into the
  square, RDP-simplified (ε = tol/4); per allowed symmetry (identity, or all 8 with
  `allow_transforms`) the worst reference segment's coverage (share of 24 samples
  within `tolerance` of ink) must be ≥ 0.85 and unexplained ink (farther than
  `tolerance` from every segment; ink on the printed border ignored with
  `ignore_border`) ≤ 12 %; the best symmetry wins. **Confidence**: per criterion a
  margin 0 at the threshold → 1 at its "clear" value; correct = min margin,
  incorrect = max margin of the failed criteria. `< REVIEW_BELOW` (0.5) →
  `needs_review` (teacher settles it with the existing `ManualGrade` override).
  Blank → 0, confident. Unreadable / absurd payload (not JSON, bad shape, NaN, >
  300 strokes / 5 000 points / 300 000 chars, points far off the canvas) → 0 +
  needs_review, never an exception; work caps (20 000 samples, 1 500 simplified
  segments) keep an adversarial scribble cheap. Feedback (Russian) names the
  numbers + the symmetry used.
- **Norm scoring (IQ, `norm_scoring.py`, pure).** `compute_iq_report(raw_by_section,
  NormScoring, age)` → `IQReport`: raw (rounded half-up) → scaled band per section →
  composite = sum of its sections' scaled → the by-age table row with that sum, the
  age group's column → overall = mean of composites (book: (I + II) / 2) → bracket.
  Open cells: "130+" = at least 130 (`open_bound="above"`), "<70" = below 70; the
  number is used in the arithmetic and the overall shows "125+" / "<75" (both kinds
  → plain number + `mixed_open_bounds`). A sum past the table's last / first row
  takes that row and is open above / below (`above_table` / `below_table`). Notes (`IQReport.notes`)
  explain every gap: `no_birth_date`, `under_min_age`, `no_norms_for_age`,
  `section_not_graded:<id>`, `no_scale_table:<id>`, `raw_above_scale_table:<id>`,
  `raw_below_scale_table:<id>`, `raw_not_in_scale_table:<id>`,
  `incomplete_composite:<key>`, `sum_not_in_table:<key>`, `no_norm_value:<key>`,
  `overall_incomplete`. Raw and scaled are reported even without norms.
  `GradingService.iq_report(result, at=None)` maps item → section
  (`section_raw_scores` over the SERVED test), takes the age from
  `RosterEntry.birth_date` at the attempt start (`contracts.runtime.age_at`) and
  returns None for a test without `norm_scoring`. Call it on the result AFTER the
  teacher's overrides — recomputing is just calling it again. Not persisted: the
  admin recomputes it on every read (`AttemptResult.iq` / `StudentReport.iq`); the
  pure core `build_iq_report(test, result, papers=, birth_date=, when=)` serves the
  CSV without per-row reads. Never reads integrity data.

- **Only the served papers are graded.** `grade()` restricts the test with
  `contracts.papers.served_test(test, attempt.papers)` first: items of papers the
  teacher did not assign get no grade, `max_score` covers the served ones, and a B1 /
  B2–C2 choose-one writing group exists only when Writing was served.

## Layout
- `normalize.py` — text normalization for gap_fill (casefold + whitespace collapse).
- `numbers.py` — number equivalence (`canonical_numbers`, `nearest_number_word`).
- `deterministic.py` — pure `grade_single_choice` / `grade_matching` / `grade_gap_fill`
  + the typo helpers (`allowed_typos`, `typo_match`, `spelling_mode`).
- `kwt.py` — key word transformation grading + Cambridge word counting
  (`kwt_tokens`, `count_words`, `has_key_word`, `TWO_ANSWERS_RE`).
- `writing_scales.py` — the B2 / C1 / C2 Writing assessment scales (data + prompt text).
- `vocabulary.py` + `data/*.txt` — the known-word list, `word_forms`,
  `is_inflection_of`, and the `bank_words.txt` regenerator (`python -m
  app.grading.vocabulary`).
- `llm.py` — `WritingGrade`, the `LLMGrader` protocol, and `MockLLMGrader` (deterministic).
- `llm_anthropic.py` — real `AnthropicWritingGrader` (lazy SDK import; logs id+tokens+latency).
- `llm_gemini.py` — real `GeminiWritingGrader` (httpx; the wired one). The API key
  goes in the `x-goog-api-key` header, never in the URL; exception text is scrubbed
  (`_redact`, an engine-local copy) before it is logged, and the stored
  `ItemGrade.feedback` on a failure names only the exception TYPE.
- `mark_toggle.py` — per-section mark_toggle grading (false marks cancel correct ones).
- `drawing.py` — the deterministic drawing grader (`parse_drawing`,
  `evaluate_drawing` → `DrawingVerdict`, `grade_drawing`).
- `norm_scoring.py` — `compute_iq_report` (+ `parse_cell`, `scaled_for`).
- `service.py` — `GradingService` (assemble result, manual override, review queue,
  `iq_report`) + `GradingError` + `section_raw_scores`.

## Tests
`tests/test_iq_family.py` (synthetic fixture `tests/iq_fixture.py`) — mark_toggle
penalty goldens; drawing region (hit / miss / scribble outside / too little ink /
just enough → review / every zone), lines (exact, one stroke, wobbly, rotations only
when allowed, mirror of a chiral figure, tolerance edge 0.07 / 0.09, far shift, stray
line, missing segment, border tracing, borderline → review, blank, absurd payloads);
norm scoring (full marks + "140+", age bands, "<70", missing cell, no birth date,
under min age, no group, beyond table, incomplete composite, mixed bounds); service
grade + IQ report; override of a review drawing recomputes the report.
`tests/test_levels_b2_c2.py` — KWT (full / half / none, limits by level, key word
missing / changed, contractions incl. B2 can't, split points, two answers, ’ and ʼ,
points), gap_fill `max_words` / spelling by level / typographic quotes, the B2-C2
Gemini scale (criteria sum + clamp, fence, verbatim descriptors) and the A2 path.
`tests/test_grading_numbers.py` — digits <-> words (every value 1..999, ordinals,
multi-word answers), different numbers never match, `rejected` wins, misspelt number
words (tolerant + unambiguous only), the real Starters / A2 Key number keys.
`tests/test_grading.py` — golden objective cases; gap_fill normalization +
acceptable-misspellings; writing routed to a mocked grader; manual override + review
queue; an assertion that the engine imports no integrity/telemetry module.
`tests/test_grading_rules.py` — gap_fill goldens from `deploy/fixtures/*.json` keys
(listening misspellings accepted; reading exact; different real words / word forms /
first-letter changes rejected even when tolerant; `rejected` list; `spelling`
per item vs derived from the skill; authoring-only fields; the word lists: size,
bank list up to date, no misspelling golden is a known word) and the B1 choose-one
cases (one / both / none written, tie, no grader, the teacher's switch of the
counted task — direct, stored marks in any order, admin `set_manual_grade` + the
detail and student report).
