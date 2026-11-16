# CLAUDE.md — feedback engine

## Responsibility
**Per-skill pedagogical feedback** for a finished attempt: given the grading result
+ the authoring `Test` + the student's answers, produce a `SkillReport` per skill
(reading / listening / writing) — score + short Russian strengths/weaknesses prose —
plus one overall summary for the teacher.

This is the **academic-performance axis** ("how well did they do"), distinct from
both grading (the score) and integrity/analysis (the cheating verdict). It is
computed **once** when an attempt is finalized (on submit) by `app/results` and
persisted as an `AttemptReport`, so the dashboard never re-runs the LLM on load.

## Inputs / Outputs
- **In:** a `Test`, a `GradingResult`, and the `{item_id: response}` answer map.
- **Out:** `(list[SkillReport], summary, model_id)`. Scores are copied straight from
  the `GradingResult`; the LLM writes only the qualitative prose.

## Must NOT
- **Read telemetry/integrity or any cheating signal** (golden rules #2/#6). No
  `app.telemetry` / `app.integrity` / `app.analysis` import. A feedback verdict must
  never move a score — the numbers come from grading, the LLM only adds prose.
- **Recompute a grade.** It consumes the `GradingResult`; it never re-grades.
- **Hard-depend on the SDK.** `llm_gemini.py` imports `httpx` only and is not
  re-exported from `__init__.py`; tests inject `MockFeedbackAnalyst`.

## Key invariants
- **Scores are deterministic** — summed from the `GradingResult` per skill, never
  invented by the LLM.
- **No-analyst fallback.** With no `FeedbackLLM` injected the service returns
  scores-only reports (empty prose) and logs a WARNING, rather than guessing.
- **Russian output** for the teacher; one LLM call per attempt.
- **Every LLM call logs** model id + token usage + latency (golden rule #8).

## Layout
- `llm.py` — `SkillBreakdown` / `ItemContext` inputs, `ReportDraft` / `SkillVerdict`
  outputs, the `FeedbackLLM` protocol, and the deterministic `MockFeedbackAnalyst`.
- `llm_gemini.py` — real `GeminiFeedbackAnalyst` (httpx; structured JSON; Russian).
  The API key goes in the `x-goog-api-key` header, never in the URL; exception text
  is scrubbed (`_redact`, an engine-local copy) before it is logged. The error is
  re-raised and scrubbed again by `app/results` before it is stored.
- `service.py` — `FeedbackService.build_report(test, grading, answers)`.
- `__init__.py` — public surface (no SDK import).

## Tests
`tests/test_feedback.py` — per-skill grouping + deterministic scores; the mock's
prose tiers (strong vs weak); no-analyst scores-only fallback; an import guard
asserting no integrity/telemetry import; the report validates against the schema.
