# CLAUDE.md — analysis engine

## Responsibility
**Advisory cheating-likelihood verdict.** Layer 3 of the three integrity layers
(golden rule #6): `telemetry` (capture, Phase 6) → `integrity` (deterministic
features, Phase 7) → **`analysis` (LLM verdict, this engine)**.

It consumes the Phase 7 `IntegrityProfile` (it does **not** re-derive features) plus
the raw event segments behind the strongest signals, and asks an injected
`AnalysisLLM` for an `AnalysisVerdict`. The verdict is **advisory only**: it informs
the teacher and never moves a score.

## Inputs / Outputs
- **In:** an `attempt_id`. The service reads the attempt's events via
  `EventRepository.list_events` (the shared seam telemetry owns) and its
  `IntegrityProfile` via `IntegrityService` (Phase 7). `flag_segments` selects the
  raw segments worth surfacing.
- **Out:** an `AnalysisVerdict` (`contracts/runtime.py`): `suspicion_score`,
  `confidence`, `flags[]`, `summary`, `model_id`. Nothing is persisted in this phase
  (mirrors Phase 5 grading / Phase 7 integrity — persisting the verdict with the
  attempt is an Admin/Phase-10 concern; that adds a table ⇒ the rule #4 ritual
  (regen JSON Schema + Alembic migration together) when it lands).

## Must NOT
- **Touch grading or any score (golden rule #2).** No `app.grading` import; the
  verdict carries no score and must never trigger a regrade. A cheating signal must
  never automatically change a score. A test scans this package's imports to enforce
  the boundary, and a behavioural test asserts grading is untouched.
- **Re-derive integrity features.** Read the `IntegrityProfile` from Phase 7; do not
  recompute latencies / hidden intervals / pacing here.
- **Invent evidence.** Flagged segments carry the *raw* events that drove them, so the
  teacher's replay is auditable next to the verdict (rule #6).
- **Hard-depend on the SDK.** The real analyst (`llm_anthropic.py`) imports
  `anthropic` lazily and is not re-exported from `__init__.py`; tests inject
  `MockAnalysisLLM`.

## Key invariants
- **Advisory only.** `AnalysisVerdict` is input for the teacher; it never mutates a
  `GradingResult`. Grading ⊥ Integrity stays true end to end.
- **Deterministic segment selection.** `flag_segments(profile, events)` is a pure
  function: long bounded hides (≥ `LONG_HIDDEN_MS`) and fast post-return answers
  (≤ `POST_RETURN_FAST_MS`, reused from integrity), long-hidden-first, each pass in
  the profile's order.
- **No-analyst fallback.** With no `AnalysisLLM` injected the service returns a
  neutral zero-suspicion / zero-confidence verdict (and logs a WARNING) rather than
  guessing — mirrors grading's no-grader path.
- **Every LLM call logs** model id + token usage + latency (golden rule #8); the real
  analyst clamps the model's `suspicion_score`/`confidence` into `[0, 1]`.
- **This is the LLM layer**, so (unlike telemetry/integrity) it MAY import
  `anthropic`; the import guard here forbids only `app.grading`.

## Layout
- `segments.py` — `FlaggedSegment` + pure `flag_segments(profile, events)` + thresholds.
- `llm.py` — `VerdictDraft`, the `AnalysisLLM` protocol, and `MockAnalysisLLM`
  (deterministic blend of systematicity + hidden time).
- `llm_anthropic.py` — real `AnthropicAnalyst` (lazy SDK import; structured output;
  logs id+tokens+latency).
- `service.py` — `AnalysisService.analyze(attempt_id)` on top of `EventRepository`
  + `IntegrityService`.
- `__init__.py` — public surface (no SDK import).

## Tests
`tests/test_analysis.py` — segment-flagging goldens; the mock's monotone suspicion
(cheaty profile > honest profile); verdict validates against the schema and stays in
range; no-analyst neutral fallback; the service reads through the repo + integrity;
an AST import-guard asserting no `app.grading` import; and a behavioural assertion
that running analysis leaves the grading score untouched (rule #2).
