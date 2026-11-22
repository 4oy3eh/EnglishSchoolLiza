"""exam-iq.js pure helpers under Node: the drawing payload stays inside the grader's
caps, keeps the contract shape, and the timed-mode detection never fires for a
test with an untimed section (so every other test keeps the plain runner).

Runs the REAL functions cut out of `apps/web/exam-iq.js` (like
`test_runner_answer_aids.py` does for exam.js); the serialized payload is then
parsed by the real server-side `parse_drawing`. Skipped when `node` is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.delivery import DeliveryService
from app.grading.drawing import MAX_POINTS, MAX_RESPONSE_CHARS, MAX_STROKES, parse_drawing
from contracts import DrawingItem
from contracts.content import DRAWING_MAX_RESPONSE_CHARS

ROOT = Path(__file__).resolve().parents[1]
IQ_JS = ROOT / "apps" / "web" / "exam-iq.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[process.argv.length - 1], "utf8").replace(/\r\n/g, "\n");
const grabFn = (name) => {
  const i = src.indexOf(`\nfunction ${name}(`);
  if (i < 0) throw new Error("missing function " + name);
  return src.slice(i, src.indexOf("\n}\n", i) + 3);
};
const grabConst = (name) => {
  const m = new RegExp(`^const ${name} = .*?;`, "m").exec(src);
  if (!m) throw new Error("missing const " + name);
  return m[0];
};
const body =
  ["DRAW_MAX_POINTS", "DRAW_MAX_CHARS", "DRAW_MAX_STROKES"].map(grabConst).join("\n") + "\n" +
  ["drawDist", "drawSegDist", "drawRdp", "drawSerialize", "drawParse", "iqTimedCandidate",
   "iqClockText", "iqLimitText", "iqPerQuestionText", "iqPracticeNorm",
   "iqPracticeCorrect"].map(grabFn).join("\n") +
  "\nreturn { drawSerialize, drawParse, iqTimedCandidate, iqClockText, DRAW_MAX_STROKES," +
  " DRAW_MAX_CHARS, iqPerQuestionText, iqPracticeCorrect };";
const api = new Function(body)();

// A frantic scribble: 200 strokes x 300 jittery points = 60 000 points.
let seed = 7;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const big = [];
for (let s = 0; s < 200; s++) {
  const st = [];
  for (let k = 0; k < 300; k++) st.push([400 * rnd(), 300 * rnd()]);
  big.push(st);
}
// Worst case the runner lets through: its stroke cap, long jittery strokes, on a
// big picture (5-digit coordinates with decimals = the longest numbers).
const huge = [];
for (let s = 0; s < api.DRAW_MAX_STROKES; s++) {
  const st = [];
  for (let k = 0; k < 400; k++) st.push([1000 + 9000 * rnd() + 0.123, 1000 + 9000 * rnd() + 0.456]);
  huge.push(st);
}
const line = [[[10, 10], [20.04, 10.06], [30, 10]]];
const timed = { sections: [{ time_limit_seconds: 60 }, { time_limit_seconds: 30 }] };
const partly = { sections: [{ time_limit_seconds: 60 }, { time_limit_seconds: null }] };
const plain = { sections: [{}, {}] };
console.log(JSON.stringify({
  big: api.drawSerialize(400, 300, big),
  huge: api.drawSerialize(12000, 12000, huge),
  line: api.drawSerialize(400, 300, line),
  empty: api.drawSerialize(400, 300, []),
  parsedBack: api.drawParse(api.drawSerialize(400, 300, line)),
  bad: [api.drawParse(""), api.drawParse("nope"), api.drawParse('{"w":0,"h":1,"strokes":[]}')],
  timed: [api.iqTimedCandidate(timed), api.iqTimedCandidate(partly), api.iqTimedCandidate(plain),
          api.iqTimedCandidate({ sections: [] })],
  clock: [api.iqClockText(61000), api.iqClockText(500), api.iqClockText(-5)],
  perQ: [[34, 6], [90, 28], [5, 4], [600, 5], [120, 20], [60, 0]].map(
    ([lim, n]) => api.iqPerQuestionText(lim, n)),
  practice: [
    api.iqPracticeCorrect({ kind: "text", solution: "Tea" }, "  tea "),
    api.iqPracticeCorrect({ kind: "text", solution: "8" }, "7"),
    api.iqPracticeCorrect({ kind: "text", solution: "8" }, ""),
    api.iqPracticeCorrect({ kind: "choice", solution: "c" }, "c"),
    api.iqPracticeCorrect({ kind: "mark", solution: "0" }, "0"),
    api.iqPracticeCorrect({ kind: "mark", solution: "1" }, "0"),
    api.iqPracticeCorrect({ kind: "drawing", solution: "" }, "x"),
  ],
  maxStrokes: api.DRAW_MAX_STROKES,
  maxChars: api.DRAW_MAX_CHARS,
}));
"""


@pytest.fixture(scope="module")
def out() -> dict:
    res = subprocess.run(
        ["node", "-e", _HARNESS, str(IQ_JS)], capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@needs_node
def test_scribble_is_simplified_under_the_grader_caps(out: dict) -> None:
    payload = out["big"]
    assert len(payload) <= MAX_RESPONSE_CHARS
    w, h, strokes = parse_drawing(payload)  # the server's own reader accepts it
    assert (w, h) == (400.0, 300.0)
    assert sum(len(s) for s in strokes) <= MAX_POINTS
    assert len(strokes) <= MAX_STROKES and out["maxStrokes"] < MAX_STROKES


@needs_node
def test_payload_shape_and_rounding(out: dict) -> None:
    assert json.loads(out["line"]) == {
        "w": 400,
        "h": 300,
        "strokes": [[[10, 10], [20, 10.1], [30, 10]]],
    }
    assert json.loads(out["empty"]) == {"w": 400, "h": 300, "strokes": []}
    assert parse_drawing(out["empty"])[2] == []
    assert out["parsedBack"]["strokes"][0][0] == [10, 10]
    assert out["bad"] == [None, None, None]


@needs_node
def test_timed_mode_only_when_every_section_has_a_clock(out: dict) -> None:
    assert out["timed"] == [True, False, False, False]
    assert out["clock"] == ["1:01", "0:01", "0:00"]


@needs_node
def test_worst_case_drawing_stays_under_the_delivery_save_cap(out: dict) -> None:
    """Delivery refuses a drawing over `DRAWING_MAX_RESPONSE_CHARS` (413); the
    runner's own budget is well under it, even for the biggest scribble it allows."""
    item = DrawingItem.model_validate(
        {
            "id": "d",
            "prompt": "p",
            "asset_id": "a.png",
            "mode": "region",
            "target": {
                "mode": "region",
                "zones": [{"kind": "rect", "x": 0, "y": 0, "w": 1, "h": 1}],
            },
        }
    )
    cap = DeliveryService.answer_size_limit(item)
    assert cap == DRAWING_MAX_RESPONSE_CHARS == MAX_RESPONSE_CHARS
    assert out["maxChars"] < cap
    payload = out["huge"]
    assert len(payload) <= out["maxChars"] < cap
    _w, _h, strokes = parse_drawing(payload)
    assert 0 < sum(len(s) for s in strokes) <= MAX_POINTS


@needs_node
def test_ready_screen_per_question_and_practice_check(out: dict) -> None:
    # "about X per question": one decimal < 3 s, whole seconds < 90 s, else minutes.
    assert out["perQ"] == ["6 s", "3 s", "1.3 s", "2 min", "6 s", ""]
    # Practice (public, never scored): text ignores case / spaces; empty = not
    # answered yet (null); a drawing practice is never checked.
    assert out["practice"] == [True, False, None, True, True, False, None]
