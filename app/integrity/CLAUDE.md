# CLAUDE.md — integrity engine

## Responsibility
**Deterministic feature extraction.** Reduce an attempt's append-only event stream
to a reproducible `IntegrityProfile`. This is layer 2 of the three integrity layers
(golden rule #6): `telemetry` (capture, Phase 6) → **`integrity` (deterministic
features, this engine)** → `analysis` (LLM verdict, Phase 8).

It computes numbers — per-question latency, hidden intervals, the post-return
pattern, pacing variance, systematicity — and **judges nothing about guilt**.

## Inputs / Outputs
- **In:** an `attempt_id`; the attempt's `IntegrityEvent`s, read back via
  `EventRepository.list_events` (telemetry owns the raw stream; integrity only reads
  it — it does not import `app/telemetry`).
- **Out:** an `IntegrityProfile` (`contracts/runtime.py`): `question_timings[]`,
  `hidden_intervals[]`, `total_hidden_ms`, `pacing_cv`, `systematicity_rate`.
  Nothing is persisted in this phase (mirrors Phase 5 grading: persisting a profile
  is an Admin/Phase-10 concern).

## Must NOT
- **Call an LLM or judge guilt.** No `anthropic`/`instructor`/`openai`, no suspicion
  score, no flags. That is `app/analysis` (Phase 8). A test scans this package's
  imports to enforce it.
- **Touch grading** (golden rule #2): no `app.grading` import; a cheating feature must
  never feed a score.
- **Use the clock, randomness, or I/O in the extractor.** `features.py` is pure:
  same events → identical profile.
- **Trust client time for decisions.** Ordering and all durations use the trusted
  `server_ts` (falling back to `client_ts` only if unstamped), mirroring the
  server-authoritative timer (rule #3).

## Key invariants
- **Deterministic ordering.** `extract_profile` is a pure function (same input list
  → same profile). Events are *stably* sorted by trusted `server_ts`, so the
  canonical ingest order from `EventRepository.list_events` (rows by id) is preserved
  on ties: events sharing an identical `server_ts` keep their ingest order rather than
  being reordered. Distinct-timestamp streams are fully order-independent; for ties
  the guarantee is tied to the repository's canonical stream, not to an arbitrary
  permutation of it.
- **Hidden intervals** pair each `visibility_hidden` with the next
  `visibility_visible`; an unclosed hide (tab closed) is dropped (no measurable end).
  `total_hidden_ms` is the sum of bounded intervals.
- **A "question" item** is any `item_id` carrying an `interaction`/`answer_change`
  event. Audio events (`audio_play`/`audio_seek`) reference a *section*, so they
  never create a `QuestionTiming`.
- **Per-question latency** = first-answer time − first-sighting time (≥ 0). A
  question seen but never answered has `latency_ms = 0` and is excluded from pacing.
- **`post_return_latency_ms`** is set only when the last visibility transition before
  the first answer was `visibility_visible` (the student had just returned): it is
  the visible→answer gap. Answering while hidden / before any return → `None`.
- **`pacing_cv`** = population std / mean of answered questions' latencies (0.0 for
  < 2 answered questions or a zero mean).
- **`systematicity_rate`** = fraction of questions whose `post_return_latency_ms` is
  ≤ `POST_RETURN_FAST_MS` (default 2000 ms) — the "left, came back, answered
  instantly" pattern repeated across questions.

## Layout
- `features.py` — pure extractor (`extract_profile`) + helpers + thresholds.
- `service.py` — `IntegrityService.profile(attempt_id)` on top of `EventRepository`;
  logs the profile summary at INFO.
- `__init__.py` — public surface (`IntegrityService`, `extract_profile`,
  `POST_RETURN_FAST_MS`).

## Tests
`tests/test_integrity.py` — synthetic event-stream goldens map to expected feature
values (latency, hidden intervals, post-return, pacing CV, systematicity);
determinism (shuffled input → identical profile); the service reads through the
repository; and an AST import-guard asserting no LLM / grading / analysis import.
