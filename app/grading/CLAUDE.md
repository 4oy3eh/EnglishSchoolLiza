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
  -> exact (normalized) `rejected` = wrong -> typo tolerance against accepted +
  variants, ONLY when the item's spelling mode is "tolerant".
- **Spelling mode** (`spelling_mode`): `GapFillItem.spelling` ("exact" /
  "tolerant", authoring-only — `ClientGapFillItem` has no such field), else derived
  from the section skill threaded in by `GradingService.grade`: `reading` /
  `writing` -> exact (Cambridge A2 Reading Part 5 / B1 Reading Part 6 open cloze and
  YLE R&W need correct spelling), `listening` -> tolerant (recognisable spelling is
  accepted; set `spelling: "exact"` on a word spelled out letter by letter), no
  skill -> tolerant (historical behaviour). `GradingService(fuzzy=False)` = exact
  only everywhere.
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

## Layout
- `normalize.py` — text normalization for gap_fill (casefold + whitespace collapse).
- `deterministic.py` — pure `grade_single_choice` / `grade_matching` / `grade_gap_fill`
  + the typo helpers (`allowed_typos`, `typo_match`, `spelling_mode`).
- `vocabulary.py` + `data/*.txt` — the known-word list, `word_forms`,
  `is_inflection_of`, and the `bank_words.txt` regenerator (`python -m
  app.grading.vocabulary`).
- `llm.py` — `WritingGrade`, the `LLMGrader` protocol, and `MockLLMGrader` (deterministic).
- `llm_anthropic.py` — real `AnthropicWritingGrader` (lazy SDK import; logs id+tokens+latency).
- `llm_gemini.py` — real `GeminiWritingGrader` (httpx; the wired one). The API key
  goes in the `x-goog-api-key` header, never in the URL; exception text is scrubbed
  (`_redact`, an engine-local copy) before it is logged, and the stored
  `ItemGrade.feedback` on a failure names only the exception TYPE.
- `service.py` — `GradingService` (assemble result, manual override, review queue) +
  `GradingError`.

## Tests
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
