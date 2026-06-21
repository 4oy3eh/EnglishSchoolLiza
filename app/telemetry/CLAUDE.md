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

## Not in scope here (deferred)
- **Auth / attempt validation / rate-limiting on ingest.** The endpoint is a thin
  capture sink and does not verify `attempt_id` or the caller (keeps telemetry
  decoupled, rule #6). That belongs to the link-issuing surface in Phases 10/11.

## Layout
- `schema.py` — `ClientEvent` / `EventBatch` ingest transport (engine-local, not a
  cross-engine contract). `ClientEvent` structurally cannot carry `server_ts`.
- `service.py` — `TelemetryService.record_batch` / `list_events`, on top of
  `EventRepository`.
- HTTP: `apps/api/telemetry.py` mounts `POST /attempts/{attempt_id}/events`.
- Recorder: `apps/web/recorder.js` (capture + batch + `sendBeacon`/`fetch`).

## Tests
`tests/test_telemetry.py` — batch persists with both timestamps; a client-supplied
`server_ts` is ignored/stamped server-side; one WARNING per event; the engine
imports no grading/integrity/analysis. `tests/test_telemetry_e2e.py` — Playwright:
backgrounding the tab produces a `visibility_hidden` event (skips if Playwright is
not installed).
