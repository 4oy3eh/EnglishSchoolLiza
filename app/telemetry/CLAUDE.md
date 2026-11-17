# CLAUDE.md — telemetry engine

## Responsibility
**Capture only.** Receive the browser recorder's batched behavioral events for an
attempt and append them to the event stream. Nothing else. This is layer 1 of the
three integrity layers (golden rule #6): `telemetry` (capture) → `integrity`
(deterministic features, Phase 7) → `analysis` (LLM verdict, Phase 8).

## Inputs / Outputs
- **In:** an `attempt_id` (from the URL path) + an `EventBatch` of `ClientEvent`s
  POSTed by `apps/web/recorder.js`.
- **Out:** appended `IntegrityEvent` rows via `EventRepository`, each carrying both
  `client_ts` (browser) and a server-stamped `server_ts` (trusted).

## Must NOT
- **Judge, score, or extract features.** No latency math, no suspicion, no
  thresholds. That is `app/integrity` (Phase 7) and `app/analysis` (Phase 8).
- **Import `app.grading`, `app.integrity`, or `app.analysis`** (golden rule #6 /
  #2). A test scans this package's source to enforce it.
- **Trust client-supplied server time.** `ClientEvent` has no `server_ts` field
  (`extra="forbid"`); the repository is the single place that stamps it.
- **Mutate or delete events.** The stream is append-only.

## Key invariants
- **Both timestamps persist.** `client_ts` is recorded as sent; `server_ts` is
  stamped on ingest and is authoritative.
- **One WARNING per event.** `EventRepository.add_event` logs each integrity event
  at WARNING (events are surfaced loudly); the service logs the batch at INFO.
- **Server-authoritative time** mirrors the server-authoritative timer (rule #3):
  the client clock is captured but never trusted for decisions.
- **`answer_change` payload carries the raw new value on purpose.** It is a second,
  telemetry-only copy of the response (the canonical answer still lives in
  `Answer`/`AnswerRow` via delivery) so Phase 7 can derive answer-change frequency /
  thrash without joining grading data. Telemetry never grades it — capture only.
  Long text (> 2 KB) arrives truncated by the recorder: `{value: head, tail,
  length, truncated: true}` — still the raw value, just bounded.
- **Replayed events may duplicate; integrity counts are capture-only.** The
  recorder keeps unsent events in localStorage and resends them on the next load
  (Phase 13); a keepalive request that was in flight when the page died and
  still landed is then sent twice (bounded: one chunk per load — while the
  network is known-bad the recorder skips `sendBeacon`, whose "accepted" proves
  nothing, and keeps everything snapshotted instead). A send with no response
  after 15 s is aborted (`AbortController`) and retried with backoff, so a batch
  the server did receive may also arrive twice.
  There is no server-side dedupe (no client event ids yet) and this sink stays
  append-only, so duplicates are tolerated over lost events. The integrity layer
  drops exact replays itself before counting (`app.integrity.writing.dedupe_events`:
  same type / client_ts / item / duration / payload); this sink still must not
  assume uniqueness.
- **Every numeric capture field is bounded** (`schema.py`; sanity caps, not
  judgments): counts and lengths <= 10 000 000 (`_MAX`); millisecond fields of a
  writing window (`window_ms`, `ik_sum_ms`, `ik_mean/p50/p90_ms`, `active_ms`)
  <= 24 h (`_MAX_MS`); the top-level `duration_ms` <= 7 days (`_MAX_DURATION_MS`,
  still a 32-bit integer). An out-of-range event is a 422.
- **One bad event does not cost a batch.** The ingest validates the whole batch,
  so a single invalid event answers 422 for all of them; the recorder then bisects
  the batch and re-sends it (`_resendSplit`), dropping only the event(s) the server
  keeps rejecting (console warning). It also cuts `writing_stats` map keys to 48
  chars and folds anything beyond 31 distinct inputTypes into `"other"`, so a
  script-dispatched inputType cannot make the stats event invalid.

- **Writing-field events carry shapes, never content.** `field_focus` /
  `field_blur` (field kind + length), `writing_stats` (additive per-window
  aggregates keyed by the raw `InputEvent.inputType`: chars added, insert counts,
  max single insert, deleted chars, keydown COUNT, inter-key histogram over
  `IK_BUCKET_EDGES_MS` + mean/p50/p90, pauses > 2 s / > 10 s, active typing ms,
  untrusted (script-dispatched) inputs), `input_burst` (one input that added
  >= 2 chars: inputType + length + trusted), `paste_attempt` (a BLOCKED paste/drop:
  kind + LENGTH of the attempted text), `fullscreen_exit`. `schema.py` validates
  each of these payloads against a typed model with `extra="forbid"` and stores
  the normalized dump, so a payload carrying text (`text`, `data`, …) is a 422 —
  "no key identities / clipboard content stored" is structural. Other event types'
  payloads are untouched. The recorder flushes `writing_stats` on blur, every
  10 s, on hide/pagehide/stop and before submit (`flushWriting` + `drain`), so
  the submit-time finalize sees the last window.

## Not in scope here (deferred)
- **Server-side dedupe of replayed events** (client-generated event ids). See the
  invariant above; a later phase can add an idempotency key to `ClientEvent`.
- **Auth / attempt validation / rate-limiting on ingest.** The endpoint is a thin
  capture sink and does not verify `attempt_id` or the caller (keeps telemetry
  decoupled, rule #6). That belongs to the link-issuing surface in Phases 10/11.

## Layout
- `schema.py` — `ClientEvent` / `EventBatch` ingest transport (engine-local, not a
  cross-engine contract). `ClientEvent` structurally cannot carry `server_ts`.
- `service.py` — `TelemetryService.record_batch` / `list_events`, on top of
  `EventRepository`.
- HTTP: `apps/api/telemetry.py` mounts `POST /attempts/{attempt_id}/events`.
- Recorder: `apps/web/recorder.js` (capture + batch + `sendBeacon`/`fetch`;
  `trackWriting` / `flushWriting` / `pasteAttempt` / `drain`; fullscreen exits).
  `apps/web/exam.js` blocks paste/drop and wires `trackWriting` on open_writing
  textareas; `exam.html` discloses that typing patterns (not content) are monitored.

## Tests
`tests/test_writing_integrity.py` — typed writing payloads (no `text`/`data` key
accepted, bounded maps and numeric fields, legacy payloads untouched);
`tests/test_recorder_split.py` (Node) — a 422 batch is bisected and only the invalid
event dropped, a transient failure mid-split re-queues in order, `writing_stats`
keys are bounded; `tests/test_writing_telemetry_e2e.py`
— Playwright on the real runner: typing + a blocked 300-char paste produce
`field_focus` / `writing_stats` / `paste_attempt{length: 300}` / `field_blur`, and
the pasted text is stored nowhere.
`tests/test_telemetry.py` — batch persists with both timestamps; a client-supplied
`server_ts` is ignored/stamped server-side; one WARNING per event; the engine
imports no grading/integrity/analysis. `tests/test_telemetry_e2e.py` — Playwright:
backgrounding the tab produces a `visibility_hidden` event (skips if Playwright is
not installed).
