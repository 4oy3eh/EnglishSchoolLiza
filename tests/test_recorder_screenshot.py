"""recorder.js: screenshot keys are captured by NAME only, and timing-critical
events are shipped at once.

Runs `apps/web/recorder.js` under Node (like `test_recorder_split.py`) with a stub
`document` / `window` that records the listeners the recorder registers, then
dispatches key events into the recorder's own handler:

* PrintScreen (keyup only, as on Windows; keydown + keyup of one press; auto-repeat)
  -> exactly one `screenshot_key {chord: "printscreen"}` per press;
* macOS Cmd+Shift+3/4/5 and Windows Win+Shift+S -> their chord names;
* EVERY other key (letters, digits, Shift+S without Meta, Meta+S without Shift …)
  records nothing — the recorder never reports which ordinary key was pressed;
* the payload is exactly `{chord}` and the server's typed model accepts it, while
  any other payload key (a smuggled key name) or an unknown chord is a 422;
* `window_blur` / `window_focus` / a tab return are sent immediately, and so is an
  answer made right after a return (the integrity rules measure those gaps in
  server time), while an ordinary answer waits for the periodic flush.

Skipped when `node` is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.telemetry.schema import ClientEvent

RECORDER = Path(__file__).resolve().parents[1] / "apps" / "web" / "recorder.js"

_HARNESS = r"""
const path = process.argv[2];
globalThis.window = globalThis;
// Pin the platform (Node >= 21 has its own navigator.platform, which would make the
// Win+Shift+S expectation depend on the machine running the test).
const setPlatform = (platform) => Object.defineProperty(globalThis, "navigator", {
  value: { platform }, configurable: true, writable: true,
});
setPlatform("Win32");
const store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
console.warn = () => {};
const docHandlers = {};
const winHandlers = {};
globalThis.document = {
  visibilityState: "visible",
  addEventListener: (type, fn) => { (docHandlers[type] = docHandlers[type] || []).push(fn); },
  removeEventListener: (type, fn) => {
    docHandlers[type] = (docHandlers[type] || []).filter((f) => f !== fn);
  },
};
globalThis.addEventListener = (type, fn) => {
  (winHandlers[type] = winHandlers[type] || []).push(fn);
};
globalThis.removeEventListener = (type, fn) => {
  winHandlers[type] = (winHandlers[type] || []).filter((f) => f !== fn);
};
const sent = [];  // every event the "server" received, in order, with its request no.
let requests = 0;
globalThis.fetch = (url, init) => {
  requests += 1;
  for (const e of JSON.parse(init.body).events) {
    sent.push({ req: requests, type: e.type, payload: e.payload });
  }
  return Promise.resolve({ ok: true, status: 202 });
};
require(path);
const tick = () => new Promise((r) => setTimeout(r, 20));
const fire = (map, type, e) => (map[type] || []).forEach((fn) => fn(e || {}));
const key = (type, e) => fire(docHandlers, type, e);

(async () => {
  // A long flush interval: anything that arrives did so through an immediate flush.
  const rec = new window.ExamRecorder.Recorder({ attemptId: "a1", flushIntervalMs: 3600000 });
  rec.start();
  await tick();
  const count = (type) => (docHandlers[type] || []).length;
  const listeners = { keydown: count("keydown"), keyup: count("keyup") };
  const shots = () => sent.filter((e) => e.type === "screenshot_key").map((e) => e.payload);

  // 1) PrintScreen as Windows delivers it: keyup only.
  key("keyup", { key: "PrintScreen", code: "PrintScreen" });
  await tick();
  const keyupOnly = shots().length;
  // 2) One press seen as keydown + keydown (auto-repeat) + keyup -> still ONE more.
  rec._lastShot = { chord: null, at: 0 };
  key("keydown", { key: "PrintScreen", code: "PrintScreen" });
  key("keydown", { key: "PrintScreen", code: "PrintScreen" });
  key("keyup", { key: "PrintScreen", code: "PrintScreen" });
  await tick();
  const onePress = shots().length - keyupOnly;
  // 3) The chords.
  key("keydown", { key: "#", code: "Digit3", metaKey: true, shiftKey: true });
  key("keydown", { key: "$", code: "Digit4", metaKey: true, shiftKey: true });
  key("keydown", { key: "%", code: "Digit5", metaKey: true, shiftKey: true });
  key("keydown", { key: "S", code: "KeyS", metaKey: true, shiftKey: true });
  await tick();
  const afterChords = shots().slice();
  // 4) Everything else: nothing recorded, whatever the modifiers.
  const before = sent.length + rec.queue.length;
  for (const e of [
    { key: "a", code: "KeyA" }, { key: "S", code: "KeyS", shiftKey: true },
    { key: "s", code: "KeyS", metaKey: true }, { key: "3", code: "Digit3" },
    { key: "#", code: "Digit3", shiftKey: true }, { key: "4", code: "Digit4", metaKey: true },
    { key: "Enter", code: "Enter" }, { key: "Shift", code: "ShiftLeft", shiftKey: true },
    { key: "Meta", code: "MetaLeft", metaKey: true },
    { key: "6", code: "Digit6", metaKey: true, shiftKey: true },
    { key: "c", code: "KeyC", ctrlKey: true }, {},
  ]) { key("keydown", e); key("keyup", e); }
  await tick();
  const otherKeys = sent.length + rec.queue.length - before;
  // 4b) On a Mac, Cmd+Shift+S is "Save As" — not a screenshot.
  setPlatform("MacIntel");
  const beforeMac = shots().length;
  key("keydown", { key: "S", code: "KeyS", metaKey: true, shiftKey: true });
  key("keydown", { key: "#", code: "Digit3", metaKey: true, shiftKey: true });
  await tick();
  const macShots = shots().slice(beforeMac);
  setPlatform("Win32");

  // 5) Timing: blur / focus / a tab return leave at once.
  const reqBefore = requests;
  fire(winHandlers, "blur");
  await tick();
  const blurSent = sent.some((e) => e.type === "window_blur");
  fire(winHandlers, "focus");
  await tick();
  const focusSent = sent.some((e) => e.type === "window_focus");
  // An ordinary answer (no recent return) waits in the queue.
  rec.answerChange("q1", "A");
  await tick();
  const coldQueued = rec.queue.filter((e) => e.type === "answer_change").length;
  // Leave + return, then answer: the return and the answer are both shipped at once.
  document.visibilityState = "hidden";
  fire(docHandlers, "visibilitychange");
  await tick();
  document.visibilityState = "visible";
  fire(docHandlers, "visibilitychange");
  await tick();
  const visibleSent = sent.some((e) => e.type === "visibility_visible");
  rec.answerChange("q2", "B");
  await tick();
  const answers = sent.filter((e) => e.type === "answer_change").length;

  rec.stop();
  const removed = { keydown: count("keydown"), keyup: count("keyup") };
  process.stdout.write(JSON.stringify({
    listeners, removed, keyupOnly, onePress, afterChords, otherKeys, macShots,
    blurSent, focusSent, coldQueued, visibleSent, answers, immediate: requests - reqBefore,
  }));
})();
"""

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(tmp_path: Path) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(harness), str(RECORDER)],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    return json.loads(out)


@needs_node
def test_screenshot_chords_are_recorded_by_name_and_nothing_else(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result["listeners"] == {"keydown": 1, "keyup": 1}
    assert result["removed"] == {"keydown": 0, "keyup": 0}  # stop() unhooks them
    assert result["keyupOnly"] == 1  # PrintScreen has no keydown on Windows
    assert result["onePress"] == 1  # keydown + auto-repeat + keyup = one press
    # The payload is the chord's NAME and nothing else.
    assert result["afterChords"] == [
        {"chord": "printscreen"},
        {"chord": "printscreen"},
        {"chord": "mac_shift_3"},
        {"chord": "mac_shift_4"},
        {"chord": "mac_shift_5"},
        {"chord": "win_shift_s"},
    ]
    # No other key is ever identified or reported.
    assert result["otherKeys"] == 0
    # On a Mac, Cmd+Shift+S ("Save As") is not reported; Cmd+Shift+3 still is.
    assert result["macShots"] == [{"chord": "mac_shift_3"}]
    # ... and the server accepts exactly these payloads.
    for payload in result["afterChords"]:
        ClientEvent(
            type="screenshot_key", client_ts=datetime(2026, 6, 21, tzinfo=UTC), payload=payload
        )


@needs_node
def test_timing_critical_events_are_shipped_at_once(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result["blurSent"] and result["focusSent"] and result["visibleSent"]
    assert result["coldQueued"] == 1  # an ordinary answer waits for the periodic flush
    # After the return both answers have left (the queued one rides along).
    assert result["answers"] == 2
    assert result["immediate"] >= 4  # blur, focus, hidden, visible(+answers)


def _event(payload: dict) -> ClientEvent:
    return ClientEvent(
        type="screenshot_key", client_ts=datetime(2026, 6, 21, tzinfo=UTC), payload=payload
    )


def test_screenshot_key_payload_is_typed_and_cannot_carry_a_key_log() -> None:
    assert _event({"chord": "printscreen"}).payload == {"chord": "printscreen"}
    for bad in (
        {},  # the chord is required
        {"chord": "ctrl_c"},  # not a screenshot chord
        {"chord": "printscreen", "key": "a"},  # a smuggled key identity
        {"chord": "printscreen", "keys": ["h", "i"]},
        {"chord": "printscreen", "text": "secret"},
        {"key": "PrintScreen"},
    ):
        with pytest.raises(ValidationError):
            _event(bad)
