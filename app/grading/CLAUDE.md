# CLAUDE.md — grading engine

## Responsibility
Turn an attempt's stored answers into a **score**. Two families:

* **Deterministic** — `single_choice`, `matching` (compare the canonical response to
  the authored `correct`), `gap_fill` (normalize + `accepted` + `accepted_variants` +
  a rapidfuzz threshold for acceptable misspellings). Pure, reproducible.
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
- **One point per objective item** (`DETERMINISTIC_POINTS`); writing carries its own
  `max_points` from the grader. `awarded` is `0` when unanswered.
- **gap_fill matching order:** exact (normalized) `accepted` -> exact (normalized)
  `accepted_variants` -> rapidfuzz ratio ≥ threshold against `accepted`. Short
  answers (< `MIN_FUZZY_LEN`) skip the fuzzy step to avoid false positives.
- **Writing routing:** `grade_mode="manual"` -> needs-review (awarded 0, pending);
  `grade_mode="llm"` -> the `LLMGrader`; with no grader injected an llm item falls
  back to needs-review rather than guessing.
- **Manual override** replaces one `ItemGrade` (method `open_writing_manual`), clears
  its `needs_review`, and recomputes the result totals. Returns a new result; inputs
  are not mutated.

## Layout
- `normalize.py` — text normalization for gap_fill (casefold + whitespace collapse).
- `deterministic.py` — pure `grade_single_choice` / `grade_matching` / `grade_gap_fill`.
- `llm.py` — `WritingGrade`, the `LLMGrader` protocol, and `MockLLMGrader` (deterministic).
- `llm_anthropic.py` — real `AnthropicWritingGrader` (lazy SDK import; logs id+tokens+latency).
- `service.py` — `GradingService` (assemble result, manual override, review queue) +
  `GradingError`.

## Tests
`tests/test_grading.py` — golden objective cases; gap_fill normalization +
acceptable-misspellings; writing routed to a mocked grader; manual override + review
queue; an assertion that the engine imports no integrity/telemetry module.
