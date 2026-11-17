"""recorder.js: a hung telemetry fetch is aborted and retried like a failure.

Runs `apps/web/recorder.js` under Node with a stub `fetch` that never answers
(it only rejects when its AbortSignal fires). Without the timeout the sender stays
`_sending` forever and the queue wedges for the rest of the page's life; with it,
the request is aborted after `fetchTimeoutMs`, the chunk is re-queued at the front
and the backoff kicks in. A follow-up send against a healthy stub drains the queue.
Skipped when `node` is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RECORDER = Path(__file__).resolve().parents[1] / "apps" / "web" / "recorder.js"

_HARNESS = r"""
const path = process.argv[2];
globalThis.window = globalThis;
const store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
console.warn = () => {};
let mode = "hang";
let calls = 0;
globalThis.fetch = (url, init) => {
  calls += 1;
  if (mode === "ok") return Promise.resolve({ ok: true, status: 202 });
  return new Promise((_, reject) => {
    if (!init.signal) return; // no signal = would hang forever
    init.signal.addEventListener("abort", () => {
      const err = new Error("aborted"); err.name = "AbortError"; reject(err);
    });
  });
};
require(path);
const rec = new window.ExamRecorder.Recorder({ attemptId: "a1", fetchTimeoutMs: 50 });
rec.queue = [{ type: "focus", client_ts: "t1" }, { type: "blur", client_ts: "t2" }];
rec.flush(false);
const during = { sending: rec._sending, inflight: rec._inflight.length, queued: rec.queue.length };
setTimeout(() => {
  const after = {
    sending: rec._sending, queued: rec.queue.length, failures: rec._failures,
    order: rec.queue.map((e) => e.client_ts), backoff: rec._retryAt > Date.now(),
    snapshot: store["exam:telemetry:a1"] ? JSON.parse(store["exam:telemetry:a1"]).length : 0,
  };
  mode = "ok";
  rec._retryAt = 0;
  rec.flush(false);
  setTimeout(() => {
    const healed = { sending: rec._sending, queued: rec.queue.length, failures: rec._failures };
    process.stdout.write(JSON.stringify({ during, after, healed, calls }));
  }, 50);
}, 200);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hung_fetch_is_aborted_requeued_and_retried(tmp_path: Path) -> None:
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(harness), str(RECORDER)],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    result = json.loads(out)
    # While the request hangs, one chunk is in flight and the queue is empty.
    assert result["during"] == {"sending": True, "inflight": 2, "queued": 0}
    after = result["after"]
    assert after["sending"] is False  # the sender is free again
    assert after["queued"] == 2 and after["order"] == ["t1", "t2"]  # re-queued, in order
    assert after["failures"] == 1 and after["backoff"] is True  # treated as a failure
    assert after["snapshot"] == 2  # persisted like any failed send
    # Next send (healthy network) drains the queue and resets the failure count.
    assert result["healed"] == {"sending": False, "queued": 0, "failures": 0}
    assert result["calls"] == 2
