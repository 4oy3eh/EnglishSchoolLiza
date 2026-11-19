"""exam.js answering aids: word-bank detection, option clipping, gap-marker linking.

Runs the REAL functions from `apps/web/exam.js` under Node (like
`test_recorder_screenshot.py` does for recorder.js): the harness cuts the pure
helpers out of the runner's source (`buildModel`, `isWordBank`, `clipText`,
`gapLinks`, `passageHTML`, ...) and feeds them the bundled official tests
(`assets/official/*/import*/*.json`) plus synthetic edge cases.

* Every numbered gap ("(19) ......") of every bundled gapped passage is linked to
  exactly one question, in passage order = question order (rule #7), whichever
  numbering the paper uses (A2/B1: the question number; YLE: 1..k per part).
* "(0)" examples, unnumbered passages and a chance overlap with question numbers
  are never linked (the passage then renders exactly as before).
* `passageHTML` never drops, adds or reorders text: removing the slots gives the
  passage minus its gap markers.
* Word banks are exactly the short-word pools (Flyers R&W Part 1, KET Listening
  Part 5); sentence / long-text pools keep the letter + text buttons.

Skipped when `node` is not on PATH.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAM_JS = ROOT / "apps" / "web" / "exam.js"
OFFICIAL = sorted(ROOT.glob("assets/official/*/import*/*.json"))

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

_HARNESS = r"""
const fs = require("fs");
const [examPath, testsPath] = process.argv.slice(2);
const src = fs.readFileSync(examPath, "utf8").replace(/\r\n/g, "\n");
const grabFn = (name) => {
  const i = src.indexOf(`\nfunction ${name}(`);
  if (i < 0) throw new Error("missing function " + name);
  const j = src.indexOf("\n}\n", i);
  return src.slice(i, j + 3);
};
const grabConst = (name) => {
  const m = new RegExp(`^const ${name} = .*?;`, "m").exec(src); // up to its first ";"
  if (!m) throw new Error("missing const " + name);
  return m[0];
};
const consts = ["OPT_FULL_MAX", "OPT_CLIP", "BANK_MAX_WORDS", "BANK_MAX_CHARS", "GAP_RE",
                "GAP_TYPES", "GAP_KINDS", "KWT_GAP_RE", "SLOT_CLIP", "gapCache"].map(grabConst);
const fns = ["esc", "skillLabel", "sectionAudio", "buildModel", "poolOf", "isShortOpt",
             "shortClip", "mirrorText",
             "isWordBank", "isOncePool", "clipText", "splitKwt", "countWords", "wordLimitHint",
             "stemChipHTML", "hasValue", "answerText", "gapLinks", "passageHTML",
             "gapSlotHTML"].map(grabFn);
const body =
  "let units = [], blocks = [], tracks = [], itemNo = {}; let values = {};\n" +
  "const stopAllAudio = () => {}; const t = (k, ...a) => k + '(' + a.join('|') + ')';\n" +
  "const localValue = (id) => values[id];\n" +
  consts.join("\n") + "\n" + fns.join("\n") +
  "\nreturn { buildModel, isWordBank, isOncePool, clipText, gapLinks, passageHTML, GAP_RE," +
  " GAP_KINDS, splitKwt, countWords, wordLimitHint, itemNo: () => itemNo," +
  " model: () => ({ tracks: tracks.map((x) => x.asset_id)," +
  " unitTracks: units.map((u) => u.trackIdx) }), setValues: (v) => { values = v; } };";
const api = new Function(body)();

const out = { tests: [], clip: {} };
for (const test of JSON.parse(fs.readFileSync(testsPath, "utf8"))) {
  api.buildModel(test);
  api.setValues(test.__values || {});
  const nos = api.itemNo();
  const sections = test.sections.map((s) => {
    const st = s.stimulus || {};
    const text = st.text || "";
    const gapped = api.GAP_KINDS.has(st.kind);
    const markers = gapped
      ? [...text.matchAll(api.GAP_RE)].map((m) => Number(m[1])).filter((n) => n > 0)
      : [];
    const links = api.gapLinks(s);
    const passage = gapped && text ? api.passageHTML(s, text) : null;
    return {
      id: s.id, kind: st.kind || null, text, markers,
      items: s.items.map((it) => it.id),
      word_bank: api.isWordBank(s),
      once: api.isOncePool(s),
      links: links ? Object.fromEntries(links.byNo) : null,
      passage,
    };
  });
  out.tests.push({ id: test.id, item_no: nos, sections, ...api.model() });
}
const misc = JSON.parse(fs.readFileSync(testsPath + ".clip", "utf8"));
for (const s of misc.clip || []) out.clip[s] = api.clipText(s);
out.split = (misc.split || []).map((x) => api.splitKwt(x));
out.count = (misc.count || []).map((x) => api.countWords(x));
out.hint = (misc.hint || []).map((x) => api.wordLimitHint(x));
process.stdout.write(JSON.stringify(out));
"""

_MARKER = re.compile(
    r"(?:(?:\.{2,}|_{2,}|…+)(?=\())?\((\d{1,3})\)(?:[ \t\u00a0]*(?:\.{2,}|_{2,}|…+))?"
)


def _strip_first_markers(text: str, links: dict) -> str:
    """The passage minus the FIRST marker of every linked gap (what the slots replace)."""
    seen: set[str] = set()

    def sub(m: re.Match) -> str:
        n = str(int(m.group(1)))
        if n in links and n not in seen:
            seen.add(n)
            return ""
        return m.group(0)

    return _MARKER.sub(sub, text)


_SLOT = re.compile(r'<button type="button" class="gap-slot[^"]*".*?</button>', re.S)


def _run(tmp_path: Path, tests: list[dict], clip: list[str] | None = None, **misc: list) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    data = tmp_path / "tests.json"
    data.write_text(json.dumps(tests), encoding="utf-8")
    misc_data = {"clip": clip or [], **misc}
    (tmp_path / "tests.json.clip").write_text(json.dumps(misc_data), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(EXAM_JS), str(data)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _official() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in OFFICIAL]


def _section(sid: str, kind: str, text: str, items: list[dict], options=None) -> dict:
    st: dict = {"kind": kind, "text": text}
    if options is not None:
        st["options"] = options
    return {"id": sid, "title": sid, "skill": "reading", "stimulus": st, "items": items}


def _gap(iid: str, prompt: str = "Write the word.") -> dict:
    return {"id": iid, "item_type": "gap_fill", "prompt": prompt}


@needs_node
def test_every_official_gap_marker_is_linked_in_order(tmp_path: Path) -> None:
    assert OFFICIAL, "bundled official tests are missing"
    out = _run(tmp_path, _official())
    checked = 0
    for test in out["tests"]:
        for s in test["sections"]:
            if not s["markers"]:
                continue
            checked += 1
            links = s["links"]
            assert links, f"{s['id']}: numbered gaps {s['markers']} not recognised"
            assert sorted(int(n) for n in links) == sorted(set(s["markers"])), s["id"]
            # Distinct questions, and passage order == question order (rule #7).
            ordered = [links[str(n)] for n in sorted(int(k) for k in links)]
            assert len(set(ordered)) == len(ordered), s["id"]
            idx = [s["items"].index(i) for i in ordered]
            assert idx == sorted(idx), s["id"]
    assert checked >= 19  # KET 4/5, PET 4/5/6, the YLE R&W gapped parts of both volumes


@needs_node
def test_paper_numbering_and_yle_numbering(tmp_path: Path) -> None:
    out = {t["id"]: t for t in _run(tmp_path, _official())["tests"]}
    ket = out["a2-key-2020"]
    r5 = next(s for s in ket["sections"] if s["id"] == "a2-key-2020-sec-r5")
    # KET Part 5: "(25)" is question 25 — the displayed number.
    for n, item in r5["links"].items():
        assert ket["item_no"][item] == n
    assert "0" not in r5["links"]  # the "(0)" example stays text
    fly = out["flyers-2018-vol1"]
    rw3 = next(s for s in fly["sections"] if s["id"] == "flyers-2018-vol1-sec-rw3")
    # YLE: gaps 1..5 of the part, while the questions are numbered on from Part 2.
    assert rw3["links"] == {str(i): rw3["items"][i - 1] for i in range(1, 6)}
    assert fly["item_no"][rw3["items"][0]] != "1"


@needs_node
def test_passage_text_is_never_changed_only_markers_become_slots(tmp_path: Path) -> None:
    out = _run(tmp_path, _official())
    for test in out["tests"]:
        for s in test["sections"]:
            if s["passage"] is None:
                continue
            rendered = s["passage"]
            if not s["links"]:
                escaped = html.escape(s["text"], quote=True).replace("&#x27;", "&#39;")
                assert rendered == f'<div class="stimulus">{escaped}</div>'
                continue
            slots = _SLOT.findall(rendered)
            assert len(slots) == len(s["links"]), s["id"]  # one slot per linked gap
            inner = re.sub(r'^<div class="stimulus gapped">|</div>$', "", rendered)
            plain = html.unescape(_SLOT.sub("", inner))
            expected = _strip_first_markers(s["text"], s["links"])
            assert plain == expected, s["id"]


@needs_node
def test_edge_cases_never_link_wrongly(tmp_path: Path) -> None:
    items_4_to_9 = [
        {
            "id": f"pre-{i}",
            "item_type": "single_choice",
            "prompt": "p",
            "options": [
                {"kind": "text", "key": "A", "text": "x"},
                {"kind": "text", "key": "B", "text": "y"},
            ],
        }
        for i in range(3)
    ]
    test = {
        "id": "edge",
        "level": "A2_KEY",
        "sections": [
            _section("pre", "passage_text", "", items_4_to_9),  # questions 1-3
            # markers 1..5 but questions 4..9, no "gap N" prompts: relative 1..k.
            _section(
                "rel",
                "gapped_text",
                "a (1) ... b (2) ... c (3) ... d (4) ... e (5) ... f",
                [_gap(f"rel-{i}") for i in range(6)],
            ),
            # only "(0)" and a 4-digit year — nothing to link
            _section("none", "gapped_text", "In (1999) I hope (0) you ...", [_gap("none-1")]),
            # more markers than questions and not 1..k: no link at all
            _section("odd", "gapped_text", "x (40) ... y (41) ...", [_gap("odd-1")]),
            # "…(2)…" style, prompts name the gaps
            _section(
                "pr",
                "gapped_text",
                "one …(2)… two …(1)… three",
                [_gap("pr-a", "Gap 1"), _gap("pr-b", "Gap 2")],
            ),
        ],
    }
    out = _run(tmp_path, [test])["tests"][0]
    by = {s["id"]: s for s in out["sections"]}
    assert by["rel"]["links"] == {str(i): f"rel-{i - 1}" for i in range(1, 6)}
    assert by["none"]["links"] is None
    assert by["none"]["passage"] == '<div class="stimulus">In (1999) I hope (0) you ...</div>'
    assert by["odd"]["links"] is None
    assert by["pr"]["links"] == {"1": "pr-a", "2": "pr-b"}
    assert "…" not in re.sub(r"<[^>]+>", "", _SLOT.sub("", by["pr"]["passage"]))


@needs_node
def test_word_bank_only_for_short_word_pools(tmp_path: Path) -> None:
    out = _run(tmp_path, _official())
    sections = [s for t in _official() for s in t["sections"]]
    banks = {s["id"] for t in out["tests"] for s in t["sections"] if s["word_bank"]}
    assert {
        "a2-key-2020-sec-l5",
        "flyers-2018-vol1-sec-rw1",
        "flyers-2018-vol2-sec-rw1",
    } <= banks
    # Sentence / long-text pools keep the letter + text buttons.
    assert not banks & {
        "b1-pet-2022-sec-r2",
        "b1-pet-2022-sec-r4",
        "flyers-2018-vol1-sec-rw2",
        "flyers-2018-vol2-sec-rw2",
    }
    for s in sections:
        if s["id"] in banks:  # every bank: >= 2 matching questions, all options short
            opts = [o["text"].strip() for o in s["stimulus"]["options"]]
            assert len(s["items"]) >= 2, s["id"]
            assert all(len(o) <= 24 and len(o.split()) <= 3 for o in opts), s["id"]
    pool = [
        {"key": k, "text": w}
        for k, w in zip("ABC", ["because", "although", "however"], strict=True)
    ]
    one = {
        "id": "one",
        "level": "A2_KEY",
        "sections": [
            _section(
                "one-s",
                "matching_pool",
                "",
                [{"id": "one-q", "item_type": "matching", "prompt": "I went out ____ it rained."}],
                pool,
            ),
        ],
    }
    # One or two questions are not a bank (their letter + text buttons are enough).
    assert _run(tmp_path, [one])["tests"][0]["sections"][0]["word_bank"] is False
    two = json.loads(json.dumps(one))
    sec = two["sections"][0]
    sec["items"] = [dict(sec["items"][0], id=f"two-q{i}") for i in (1, 2)]
    assert _run(tmp_path, [two])["tests"][0]["sections"][0]["word_bank"] is False
    sec["items"].append(dict(sec["items"][0], id="two-q3"))
    assert _run(tmp_path, [two])["tests"][0]["sections"][0]["word_bank"] is True


@needs_node
def test_clip_long_option_texts(tmp_path: Path) -> None:
    short = "Usually when I've finished everything that I've got to do."
    long = (
        "Beckfield Market. This market's world-famous for second-hand camera equipment "
        "and books on photography, and it's open every day."
    )
    clip = _run(tmp_path, [], [short, long, "x" * 120])["clip"]
    assert clip[short] == short  # up to 90 characters: shown in full
    assert clip[long].endswith("…") and len(clip[long]) <= 57
    assert clip[long].startswith("Beckfield Market. This market's")
    assert clip["x" * 120] == "x" * 56 + "…"


@needs_node
def test_passage_text_note_forms_link_only_by_explicit_number(tmp_path: Path) -> None:
    out = _run(tmp_path, _official())["tests"]
    note = next(s for t in out for s in t["sections"] if s["id"] == "ket-2016-additional-sec-w8")
    # KET 2016 Writing Part 8: "(51)" .. "(55)" match the "51 Who to phone ..." prompts.
    assert note["kind"] == "passage_text"
    assert sorted(note["links"]) == ["51", "52", "53", "54", "55"]
    notice = {
        "id": "pt",
        "level": "A2_KEY",
        "sections": [
            # A notice saying "(1)" over question 1 (a choice question): a plain
            # passage only links boxes to write in.
            _section(
                "pre",
                "passage_text",
                "NOTICE (1) Closed on Monday.",
                [
                    {
                        "id": "pre-q",
                        "item_type": "single_choice",
                        "prompt": "When?",
                        "options": [
                            {"kind": "text", "key": "A", "text": "x"},
                            {"kind": "text", "key": "B", "text": "y"},
                        ],
                    }
                ],
            ),
            # "(1)" over question 2 with no numbering in the prompt: never by order.
            _section("pt-1", "passage_text", "Call us (1) any day.", [_gap("pt-q")]),
            # ...while a gapped_text of the same shape does (YLE 1..k).
            _section("gt-1", "gapped_text", "Call us (1) ..... any day.", [_gap("gt-q")]),
            # Word formation in a passage_text: the prompt "(17)" names its gap.
            _section(
                "wf",
                "passage_text",
                "Word formation (17) ____ here.",
                [{**_gap("wf-q", "(17)"), "stem_word": "compete", "max_words": 1}],
            ),
        ],
    }
    by = {s["id"]: s for s in _run(tmp_path, [notice])["tests"][0]["sections"]}
    assert by["pre"]["links"] is None
    assert by["pt-1"]["links"] is None
    assert by["pt-1"]["passage"] == '<div class="stimulus">Call us (1) any day.</div>'
    assert by["gt-1"]["links"] == {"1": "gt-q"}
    assert by["wf"]["links"] == {"17": "wf-q"}
    assert '<span class="stem-chip" lang="en">(COMPETE)</span>' in by["wf"]["passage"]


@needs_node
def test_pool_audio_opens_and_continues_listening_tracks(tmp_path: Path) -> None:
    def pool(sid: str, asset: str | None) -> dict:
        st: dict = {
            "kind": "matching_pool",
            "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
        }
        if asset:
            st["audio"] = {"kind": "audio_asset", "asset_id": asset, "plays": 2}
        return {
            "id": sid,
            "skill": "listening",
            "stimulus": st,
            "items": [{"id": f"{sid}-q", "item_type": "matching", "prompt": "p"}],
        }

    test = {
        "id": "aud",
        "level": "C1_ADVANCED",
        "sections": [
            {
                "id": "l1",
                "skill": "listening",
                "stimulus": {"kind": "audio_asset", "asset_id": "p1.mp3"},
                "items": [_gap("l1-q")],
            },
            pool("l4a", "p4.mp3"),  # Part 4 Task 1: its own recording -> new track
            pool("l4b", "p4.mp3"),  # Task 2: the same recording -> the SAME track
            pool("l4c", None),  # no audio -> continues the current track
            pool("l5", "p5.mp3"),  # a different file -> the next track
        ],
    }
    out = _run(tmp_path, [test])["tests"][0]
    assert out["tracks"] == ["p1.mp3", "p4.mp3", "p5.mp3"]
    assert out["unitTracks"] == [0, 1, 1, 1, 2]


@needs_node
def test_kwt_split_word_count_and_limit_hints(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        [],
        split=["It would be ____ now.", "She ... the party.", "He …… go.", "No gap"],
        count=["", "  had no  idea ", "didn't want to"],
        hint=[None, 1, 3],
    )
    assert out["split"] == [
        ["It would be ", " now."],
        ["She ", " the party."],
        ["He ", " go."],
        ["No gap ", ""],
    ]
    assert out["count"] == [0, 3, 3]  # plain whitespace count (informational only)
    assert out["hint"] == ["", "oneWord()", "maxWords(3)"]


@needs_node
def test_each_option_once_flag_is_read(tmp_path: Path) -> None:
    opts = [{"key": "A", "text": "One sentence here."}, {"key": "B", "text": "Another."}]
    items = [{"id": f"o-{i}", "item_type": "matching", "prompt": "p"} for i in (1, 2)]
    test = {
        "id": "once",
        "level": "B2_FIRST",
        "sections": [
            {**_section("once-a", "matching_pool", "", items, opts), "id": "once-a"},
            _section(
                "once-b", "matching_pool", "", [dict(i, id=i["id"] + "b") for i in items], opts
            ),
        ],
    }
    test["sections"][0]["stimulus"]["each_option_once"] = True
    by = {s["id"]: s for s in _run(tmp_path, [test])["tests"][0]["sections"]}
    assert by["once-a"]["once"] is True and by["once-b"]["once"] is False


@needs_node
def test_prompt_numbers_must_cover_every_gap(tmp_path: Path) -> None:
    pre = [_gap(f"pre-{i}") for i in range(3)]  # questions 1-3 -> the part is 4..8
    test = {
        "id": "partial",
        "level": "A2_KEY",
        "sections": [
            _section("pre", "passage_text", "", pre),
            # "5 people went…" would claim gap 5 alone; all-or-nothing -> by order.
            _section(
                "story",
                "gapped_text",
                "a (1) ... b (2) ... c (3) ... d (4) ... e (5) ... f",
                [_gap("st-0", "5 people went to the party.")]
                + [_gap(f"st-{i}") for i in range(1, 5)],
            ),
        ],
    }
    by = {s["id"]: s for s in _run(tmp_path, [test])["tests"][0]["sections"]}
    assert by["story"]["links"] == {str(i): f"st-{i - 1}" for i in range(1, 6)}


@needs_node
def test_a_repeated_gap_number_gets_one_slot(tmp_path: Path) -> None:
    text = "a (1) ..... b (2) ..... and again (1) ..... end"
    test = {
        "id": "dup",
        "level": "A2_KEY",
        "sections": [_section("dup", "gapped_text", text, [_gap("d-1"), _gap("d-2")])],
    }
    s = _run(tmp_path, [test])["tests"][0]["sections"][0]
    slots = _SLOT.findall(s["passage"])
    assert [re.search(r'data-gap-no="(\d+)"', x).group(1) for x in slots] == ["1", "2"]
    plain = html.unescape(_SLOT.sub("", re.sub(r"^<div[^>]*>|</div>$", "", s["passage"])))
    assert plain == "a  b  and again (1) ..... end"


@needs_node
def test_inline_slot_shows_letter_and_clip_for_long_options(tmp_path: Path) -> None:
    long = "The trip had been planned for months, but nobody expected the storm."
    opts = [{"key": "A", "text": "jam"}, {"key": "C", "text": long}]
    test = {
        "id": "mir",
        "level": "B2_FIRST",
        "__values": {"m-1": "C", "m-2": "A", "g-1": "a very long typed answer indeed ok"},
        "sections": [
            _section(
                "mp",
                "matching_pool",
                "x (1) ..... y (2) .....",
                [
                    {"id": "m-1", "item_type": "matching", "prompt": "Gap 1"},
                    {"id": "m-2", "item_type": "matching", "prompt": "Gap 2"},
                ],
                opts,
            ),
            _section("gf", "gapped_text", "z (3) .....", [_gap("g-1", "Gap 3")]),
        ],
    }
    by = {s["id"]: s for s in _run(tmp_path, [test])["tests"][0]["sections"]}
    vals = re.findall(r'<span class="gap-val">(.*?)</span>', by["mp"]["passage"])
    assert vals == ["C — The trip had been planned…", "jam"]
    labels = re.findall(r'aria-label="([^"]*)"', by["mp"]["passage"])
    assert "The trip had been planned…" in labels[0] and "storm" not in labels[0]
    # Typed text is mirrored as typed.
    assert "a very long typed answer indeed ok" in by["gf"]["passage"]
