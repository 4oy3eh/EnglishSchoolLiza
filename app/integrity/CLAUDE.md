# CLAUDE.md — integrity engine

## Responsibility
**Deterministic feature extraction.** Reduce an attempt's append-only event stream
to a reproducible `IntegrityProfile`. This is layer 2 of the three integrity layers
(golden rule #6): `telemetry` (capture, Phase 6) → **`integrity` (deterministic
features, this engine)** → `analysis` (LLM verdict, Phase 8).

It computes numbers — per-question latency, hidden intervals, the post-return
pattern, pacing variance, systematicity, per-writing-item typing features — plus
**deterministic threshold flags** on writing items (facts like "400 chars appeared
without keystrokes", documented below), and **judges nothing about guilt**: turning
flags into a likelihood is `app/analysis`.

## Inputs / Outputs
- **In:** an `attempt_id`; the attempt's `IntegrityEvent`s, read back via
  `EventRepository.list_events` (telemetry owns the raw stream; integrity only reads
  it — it does not import `app/telemetry`).
- **In (optional attempt data):** `answers` (item_id -> submitted text) and
  `writing_items` (the test's open_writing ids) — passed by the results layer via
  the analysis service. The text is attempt data, not grading. Without them the
  writing items are derived from the writing telemetry (`field` == open_writing)
  and the final text from the last `answer_change`.
- **Out:** an `IntegrityProfile` (`contracts/runtime.py`): `question_timings[]`,
  `hidden_intervals[]`, `total_hidden_ms`, `pacing_cv`, `systematicity_rate`,
  `device_*`, `paste_attempt_count`, `fullscreen_exit_count`, `writing[]`
  (`WritingFeatures`). The results finalize stores the profile the verdict was
  computed from in `attempt_reports.integrity`.

## Must NOT
- **Call an LLM or judge guilt.** No `anthropic`/`instructor`/`openai`/`httpx`, no
  suspicion score. Flags are threshold FACTS only; the likelihood is `app/analysis`.
  Tests scan this package's imports to enforce it (no analysis/grading/telemetry).
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

- **Replays are dropped before anything is counted.** The recorder is
  at-least-once (unsent events are resent after a reload, a timed-out batch is
  retried), so the stream can hold the same capture twice. `dedupe_events`
  (`writing.py`) removes exact replays — identical `(type, client_ts, item_id,
  duration_ms, canonical JSON payload)`, first occurrence kept, `server_ts` NOT in
  the key (a replay is ingested later). `extract_profile` applies it right after
  ordering (question timings, paste / fullscreen counts, device summary and the
  writing features all see the deduped stream) and `extract_writing_features`
  applies it again (idempotent) for direct callers. The service logs how many
  replays were ignored. The raw stream is untouched (the replay shows everything).

## Writing features (`writing.py`)
Per writing item (the test's open_writing ids when given, else items with writing
telemetry). All numbers come from `writing_stats` (summed — they are additive
deltas), `input_burst`, `paste_attempt`, `field_focus`, `answer_change` and the
hide/blur/fullscreen events; all time is trusted `server_ts`.
- **typed vs non-keyboard.** Keyboard inputTypes = `insertText`,
  `insertCompositionText` (mobile IME), `insertLineBreak`, `insertParagraph`.
  `historyUndo/Redo` restore text and count as neither. Every other inputType
  (insertFromDrop/Paste, insertReplacementText, "" = script-dispatched, …) is
  non-keyboard. A single keyboard-type `input_burst` larger than
  `KEYBOARD_MAX_INSERT` (12 chars) is reclassified as non-keyboard (an extension's
  `execCommand('insertText', essay)`, autofill, dictation committing a phrase);
  a swipe/autocorrected word stays below it.
- **unexplained_chars** = final length − (typed + non-keyboard + history − deleted),
  floored at 0, only when stats exist: text that entered the field with NO input
  event (programmatic value set) — or telemetry that was lost (advisory!).
- **typed_ratio** = typed / (typed + non_keyboard + unexplained) (None w/o stats);
  **revision_ratio** = deleted / added.
- **max_snapshot_growth(_ms)**: largest length growth between consecutive
  `answer_change` snapshots; baseline = the later of the previous snapshot and the
  latest `field_focus` (which carries the length). Note `answer_change` is debounced
  600 ms, so measured rates are conservative (real rates are higher).
- **inter-key p50/p90**: linear interpolation in the summed fixed-bucket histogram
  (`IK_BUCKET_EDGES_MS`, must equal telemetry's and recorder.js `IK_EDGES` — a test
  asserts it); the open last bucket returns 10 000. Mean = sum / count.
- **chars_per_active_minute** = typed / active_ms (only when active >= 1 s);
  active = sum of inter-key gaps <= 2 s.
- **time_to_first_char_ms** = first `answer_change`/`input_burst` − first
  `field_focus` (includes the 600 ms save debounce when no burst).
- **writing window** = first `field_focus` (else first snapshot) → last
  `answer_change`/`input_burst`. `hidden_ms_while_writing` / `blur_ms_while_writing`
  = overlap with paired hide→show / blur→focus intervals that BEGAN while this item
  was the recorder's current item; `away_count` = max(#hides, #blurs);
  `fullscreen_exits_while_writing` counts exits on this item inside the window.
- **telemetry completeness** (a capture FACT, not a suspicion):
  `uncovered_snapshot_growth` = chars the `answer_change` snapshots show arriving
  at human typing speed minus the chars any input event accounts for (typed +
  non-keyboard + history), floored at 0. A snapshot step counts as "typed" when it
  is < 40 chars (`SNAPSHOT_MIN_GROWTH`; time ignored, snapshots of one ingest batch
  share a server timestamp) or not faster than 600 chars/min
  (`GRADUAL_GROWTH_MAX_CPM`); a paste / programmatic insert is ONE fast jump and is
  excluded. `recorder_restarts_while_writing` = max(#`pagehide`, #`device_info`)
  strictly inside the writing window (a reload; the first load's `device_info`
  precedes the first focus). `telemetry_gap` = uncovered >= 20 chars
  (`TELEMETRY_GAP_MIN_CHARS`) OR at least one restart.
- **text stats** (final text; level-mismatch evidence only): `word_count`
  (`[A-Za-z]+('[A-Za-z]+)*`), `sentence_count` (segments split on `.!?` that hold a
  word, min 1), `mean_sentence_length`, `type_token_ratio` (distinct lowercase /
  words), `long_word_share` (>= 8 letters). A truncated snapshot with no submitted
  text -> zeros.

### Flags (fixed order; thresholds in `writing.py`)
| flag | condition |
|---|---|
| `text_appeared_without_typing` | (a) stats exist and OBSERVED non_keyboard chars alone >= 40 AND >= 25 % of the final length; OR (b) a snapshot grew >= 40 chars at > 1500 chars/min (works without stats); OR (c) stats exist, (non_keyboard + unexplained) clears the same thresholds AND there is no `telemetry_gap` |
| `telemetry_gap` | route (c) would fire but `telemetry_gap` is true: the text is "unexplained" only by subtraction while keystroke telemetry was lost (reload / dropped batch). Raised INSTEAD of `text_appeared_without_typing` (never both); weak (analysis weight 0.15 vs 0.5). Routes (a)/(b) are positive observations and are never downgraded. Known trade-off: a reload also downgrades a silent programmatic insert that produced no input event and no fast snapshot jump — advisory, the analyst still sees `unexplained_chars` |
| `scripted_input` | >= 1 input event with `isTrusted=false` |
| `large_paste_attempt` | a blocked paste/drop of >= 50 chars |
| `typing_speed_implausible` | typed >= 100 chars at > 600 chars per active minute |
| `away_while_writing` | max(hidden, blurred) overlap with the writing window >= 30 s |
| `fullscreen_exit_while_writing` | >= 1 fullscreen exit inside the window |
| `no_input_telemetry` | no `writing_stats`, no `answer_change`, no burst, yet a final text >= 40 chars (answer reached the server bypassing the runner) |

## Layout
- `features.py` — pure extractor (`extract_profile`) + helpers + thresholds.
- `writing.py` — pure per-writing-item features + flags (`extract_writing_features`)
  + every writing threshold above.
- `service.py` — `IntegrityService.profile(attempt_id, *, answers=None,
  writing_items=None)` on top of `EventRepository`; logs the profile summary at
  INFO, one line per writing item, and a WARNING per flagged writing item.
- `__init__.py` — public surface (`IntegrityService`, `extract_profile`,
  `POST_RETURN_FAST_MS`).

## Tests
`tests/test_integrity.py` — synthetic event-stream goldens map to expected feature
values (latency, hidden intervals, post-return, pacing CV, systematicity);
determinism (shuffled input → identical profile); the service reads through the
repository; and an AST import-guard asserting no LLM / grading / analysis import.
`tests/test_writing_integrity.py` — writing goldens (honest typist vs. paste/away/
bulk-insert stream), unexplained text, snapshot route without stats, truncated
snapshots, no-telemetry answers, scripted + implausibly fast typing, determinism
under reordering, histogram edges shared with telemetry + recorder.js; replayed
batches leave the profile identical (`dedupe_events`); lost stats / a reload inside
the writing window give `telemetry_gap` and the weak flag, while an observed bulk
insert or a fast snapshot jump keeps the strong one; the gap reaches the prompt.
`tests/test_integrity_invariants.py` — layer import guards + no score fields.
