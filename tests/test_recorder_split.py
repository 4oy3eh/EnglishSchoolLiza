"""recorder.js: one invalid event must not kill a batch of good ones.

Runs `apps/web/recorder.js` under Node (like `test_recorder_timeout.py`) against a
stub `fetch` that behaves like the ingest endpoint's typed payload validation: a
request containing an invalid event answers 422, otherwise 202.

* A 422 on a multi-event batch is bisected and re-sent: only the invalid event(s)
  are dropped (console warning), every good event is delivered once, in order.
* A transient failure in the middle of the split re-queues everything not yet
  delivered at the front (order kept), persists it and backs off; the next send
  finishes the job.
* A lone rejected event is dropped (nothing to split), as before.
* `writing_stats` map keys (`added` / `inserts` / `max_insert`) are cut to 48
  chars and capped at 32 distinct types, like the server requires — so a
  script-dispatched inputType cannot make the server reject the stats event.

Skipped when `node` is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RECORDER = Path(__file__).resolve().parents[1] / "apps" / "web" / "recorder.js"

_PRELUDE = r"""
const path = process.argv[2];
globalThis.window = globalThis;
const store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const warnings = [];
console.warn = (...a) => { warnings.push(a.join(" ")); };
const delivered = [];   // client_ts of every event the "server" stored, in order
const requests = [];    // size of every request
let outage = 0;         // answer 503 to this many upcoming requests
globalThis.fetch = (url, init) => {
  const events = JSON.parse(init.body).events;
  requests.push(events.length);
  if (outage > 0) { outage -= 1; return Promise.resolve({ ok: false, status: 503 }); }
  if (events.some((e) => e.payload && e.payload.bad)) {
    return Promise.resolve({ ok: false, status: 422 });
  }
  for (const e of events) delivered.push(e.client_ts);
  return Promise.resolve({ ok: true, status: 202 });
};
require(path);
const mk = (i, bad) => ({
  type: "interaction", client_ts: "t" + i, item_id: "q", duration_ms: null,
  payload: bad ? { bad: true } : {},
});
const settle = () => new Promise((r) => setTimeout(r, 50));
const snapshot = () =>
  store["exam:telemetry:a1"] ? JSON.parse(store["exam:telemetry:a1"]).map((e) => e.client_ts) : [];
"""

_SPLIT = _PRELUDE + r"""
(async () => {
  const rec = new window.ExamRecorder.Recorder({ attemptId: "a1" });
  // 9 events, #5 invalid; then a lone invalid one.
  rec.queue = [0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => mk(i, i === 5));
  rec.flush(false);
  const during = { sending: rec._sending };
  await settle();
  const first = {
    delivered: delivered.slice(), requests: requests.slice(), queued: rec.queue.length,
    sending: rec._sending, splitting: rec._splitting, inflight: rec._inflight.length,
    failures: rec._failures, snapshot: snapshot(), warnings: warnings.slice(),
  };
  delivered.length = 0; requests.length = 0; warnings.length = 0;
  rec.queue = [mk(20, true)];
  rec.flush(false);
  await settle();
  const lone = {
    delivered: delivered.slice(), requests: requests.slice(), queued: rec.queue.length,
    sending: rec._sending, warnings: warnings.slice(),
  };
  delivered.length = 0; requests.length = 0; warnings.length = 0;
  // Two invalid events in one batch, both isolated.
  rec.queue = [30, 31, 32, 33, 34, 35].map((i) => mk(i, i === 30 || i === 34));
  rec.flush(false);
  await settle();
  const two = {
    delivered: delivered.slice(), queued: rec.queue.length, warnings: warnings.slice(),
  };
  process.stdout.write(JSON.stringify({ during, first, lone, two }));
})();
"""

_TRANSIENT = _PRELUDE + r"""
(async () => {
  const rec = new window.ExamRecorder.Recorder({ attemptId: "a1" });
  rec.queue = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => mk(i, i === 6));
  rec.flush(false);          // request 1: 422 (whole batch)
  await Promise.resolve();   // let the 422 start the split
  // The split's 1st half [0..3] lands, then the server has an outage.
  const origFetch = globalThis.fetch;
  let n = 0;
  globalThis.fetch = (url, init) => {
    n += 1;
    if (n === 2) outage = 1;
    return origFetch(url, init);
  };
  await settle();
  const after = {
    delivered: delivered.slice(), queue: rec.queue.map((e) => e.client_ts),
    sending: rec._sending, splitting: rec._splitting, failures: rec._failures,
    backoff: rec._retryAt > Date.now(), snapshot: snapshot(),
  };
  rec.queue.push(mk(9, false));   // recorded while backing off
  rec._retryAt = 0;
  rec.flush(false);
  await settle();
  const healed = {
    delivered: delivered.slice(), queued: rec.queue.length, sending: rec._sending,
    failures: rec._failures, snapshot: snapshot(),
    dropped: warnings.filter((w) => w.includes("rejected")).length,
  };
  process.stdout.write(JSON.stringify({ after, healed }));
})();
"""

_KEYS = _PRELUDE + r"""
const listeners = {};
const el = {
  value: "", isConnected: true,
  addEventListener: (k, f) => { listeners[k] = f; },
  removeEventListener: () => {},
};
const rec = new window.ExamRecorder.Recorder({ attemptId: "a1" });
rec.trackWriting(el, "w1", "open_writing");
const type = (inputType, text) => {
  el.value += text;
  listeners.input({ inputType, isTrusted: false });
};
type("x".repeat(100), "ab");          // a 100-char scripted inputType, +2 chars
type("constructor", "c");             // must be an ordinary key
type("__proto__", "d");
for (let i = 0; i < 40; i++) type("t" + i, "z");   // more than 32 distinct types
type("insertText", "y");
rec.flushWriting("test");
const stats = rec.queue.filter((e) => e.type === "writing_stats")[0].payload;
const burst = rec.queue.filter((e) => e.type === "input_burst")[0].payload;
const keys = (m) => Object.keys(m);
process.stdout.write(JSON.stringify({
  added: stats.added, inserts: stats.inserts, max_insert: stats.max_insert,
  nAdded: keys(stats.added).length, nInserts: keys(stats.inserts).length,
  nMax: keys(stats.max_insert).length,
  longest: Math.max(...keys(stats.added).map((k) => k.length)),
  total: Object.values(stats.added).reduce((a, b) => a + b, 0),
  untrusted: stats.untrusted_inputs, burstType: burst.input_type.length,
  json: JSON.stringify(stats.added).length > 0,
}));
"""


def _run(tmp_path: Path, script: str) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(script, encoding="utf-8")
    out = subprocess.run(
        ["node", str(harness), str(RECORDER)],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    return json.loads(out)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_422_batch_is_split_and_only_the_invalid_event_is_dropped(tmp_path: Path) -> None:
    result = _run(tmp_path, _SPLIT)
    assert result["during"] == {"sending": True}
    first = result["first"]
    # Every good event delivered exactly once, in the original order; #5 dropped.
    assert first["delivered"] == ["t0", "t1", "t2", "t3", "t4", "t6", "t7", "t8"]
    # Bisection, not 9 single sends: 1 (whole) + halves down to the bad event.
    assert first["requests"][0] == 9 and first["requests"][1] == 5
    assert len(first["requests"]) <= 9 and first["requests"].count(1) <= 3
    assert first["queued"] == 0 and first["inflight"] == 0
    assert first["sending"] is False and first["splitting"] is False
    assert first["failures"] == 0 and first["snapshot"] == []
    assert any("dropped 1 'interaction' event" in w for w in first["warnings"])
    assert any("dropped 1 invalid of 9 events" in w for w in first["warnings"])

    # A lone rejected event: nothing to split — dropped with the old warning.
    lone = result["lone"]
    assert lone["delivered"] == [] and lone["requests"] == [1]
    assert lone["queued"] == 0 and lone["sending"] is False
    assert any("rejected with HTTP 422; dropped 1 events" in w for w in lone["warnings"])

    two = result["two"]
    assert two["delivered"] == ["t31", "t32", "t33", "t35"] and two["queued"] == 0
    assert any("dropped 2 invalid of 6 events" in w for w in two["warnings"])


@needs_node
def test_transient_failure_during_the_split_requeues_and_recovers(tmp_path: Path) -> None:
    result = _run(tmp_path, _TRANSIENT)
    after = result["after"]
    # First half landed; the outage hit the second half: it is back at the FRONT
    # of the queue, in order (the invalid #6 included — not yet proven invalid).
    assert after["delivered"] == ["t0", "t1", "t2", "t3"]
    assert after["queue"] == ["t4", "t5", "t6", "t7"]
    assert after["sending"] is False and after["splitting"] is False
    assert after["failures"] == 1 and after["backoff"] is True
    assert after["snapshot"] == ["t4", "t5", "t6", "t7"]  # persisted like any failed send
    healed = result["healed"]
    # No good event lost or duplicated; the invalid one dropped; new events follow.
    assert healed["delivered"] == ["t0", "t1", "t2", "t3", "t4", "t5", "t7", "t9"]
    assert healed["queued"] == 0 and healed["sending"] is False
    assert healed["failures"] == 0 and healed["snapshot"] == []
    assert healed["dropped"] >= 1


@needs_node
def test_writing_stats_map_keys_are_bounded_like_the_burst_payload(tmp_path: Path) -> None:
    result = _run(tmp_path, _KEYS)
    assert result["longest"] == 48  # the 100-char inputType was cut
    assert "x" * 48 in result["added"] and result["added"]["x" * 48] == 2
    assert result["burstType"] == 48
    # At most 32 distinct types; the overflow folds into "other" (nothing is lost).
    assert result["nAdded"] == result["nInserts"] == result["nMax"] == 32
    assert result["added"]["other"] >= 1
    assert result["total"] == 2 + 1 + 1 + 40 + 1  # every inserted char still counted
    # Prototype names are ordinary integer-valued keys.
    assert result["added"]["constructor"] == 1 and result["added"]["__proto__"] == 1
    assert all(isinstance(v, int) for v in result["added"].values())
    assert result["untrusted"] == 44

    # ...and the server's typed model accepts exactly that shape.
    from datetime import UTC, datetime

    from app.telemetry.schema import ClientEvent

    ClientEvent(
        type="writing_stats",
        client_ts=datetime(2026, 6, 21, tzinfo=UTC),
        item_id="w1",
        payload={
            "added": result["added"],
            "inserts": result["inserts"],
            "max_insert": result["max_insert"],
        },
    )
