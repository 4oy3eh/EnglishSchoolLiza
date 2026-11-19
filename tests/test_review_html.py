"""`scripts/review_html.py` — the teacher's offline review page for a draft Test JSON.

The page is built from untrusted ingest output (an LLM / agent wrote the JSON and
NOTES.md), so every value must be inert: escaped in markup and `\\u003c`-escaped
inside the inline script (a `</script>` in the test id must not close it).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

import scripts.review_html as review
from scripts.review_html import js_str, render


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))


def _tags(page: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Every start tag the browser's tokenizer would see (attribute values decoded)."""
    parser = _TagCollector()
    parser.feed(page)
    return parser.tags


HOSTILE = "</script><script>alert(1)</script><img src=x onerror=alert(2)>"


def _hostile_test() -> dict:
    return {
        "id": f"t-{HOSTILE}",
        "title": f"Title {HOSTILE}",
        "level": "A2_KEY",
        "status": "draft",
        "duration_minutes": 30,
        "sections": [
            {
                "id": f"s1-{HOSTILE}",
                "title": f"Part {HOSTILE}",
                "skill": "reading",
                "stimulus": {"kind": "passage_text", "text": f"Passage {HOSTILE}"},
                "items": [
                    {
                        "id": f"q1-{HOSTILE}",
                        "item_type": "single_choice",
                        "prompt": f"Prompt {HOSTILE}",
                        "options": [
                            {"key": "A", "text": f"wrong {HOSTILE}"},
                            {"key": "B", "text": "the right one"},
                        ],
                        "correct": "B",
                    },
                    {
                        "id": "q2",
                        "item_type": "gap_fill",
                        "prompt": "Fill",
                        "accepted": [f"cat{HOSTILE}"],
                        "rejected": [HOSTILE],
                    },
                ],
            },
            {
                "id": "s2",
                "title": "Listening",
                "skill": "listening",
                "stimulus": {"kind": "audio_asset", "asset_id": "missing.mp3", "plays": 2},
                "items": [
                    {
                        "id": "q3",
                        "item_type": "single_choice",
                        "prompt": "Which?",
                        "options": [
                            {"key": "A", "kind": "image", "asset_id": "here.png", "alt": HOSTILE},
                            {"key": "B", "kind": "image", "asset_id": "gone.png"},
                        ],
                        "correct": "A",
                    }
                ],
            },
        ],
    }


NOTES = f"# Notes {HOSTILE}\n\n- bullet **{HOSTILE}**\n- `code {HOSTILE}`\n\n| a | {HOSTILE} |\n"


def test_js_str_is_inert_in_a_script_and_round_trips() -> None:
    lit = js_str(HOSTILE + "&\u2028")
    assert "<" not in lit and ">" not in lit and "&" not in lit
    assert "\u2028" not in lit
    assert json.loads(lit) == HOSTILE + "&\u2028"  # the same string for JS/JSON
    assert js_str(None) == '""'


def test_hostile_strings_never_reach_the_page_raw() -> None:
    page = render(_hostile_test(), NOTES)
    # Exactly the template's own script element — nothing from the data opens or
    # closes one.
    assert page.count("<script") == 1
    assert page.count("</script") == 1
    script = page[page.index("<script>") : page.index("</script>")]
    markup = page.replace(script, "")
    assert "<img src=x" not in markup  # escaped: &lt;img src=x ...
    tags = _tags(page)
    assert [t for t, _ in tags if t == "script"] == ["script"]
    assert not [a for _, attrs in tags for a, _ in attrs if a.startswith("on")]  # no on*
    assert ("img", [("src", "x")]) not in [(t, a[:1]) for t, a in tags]
    assert "<" not in script[len("<script>") :]  # nothing in the script can open a tag
    assert "\\u003c/script\\u003e" in script  # the id, escaped for the script
    # Visible text is escaped, not dropped.
    assert "&lt;/script&gt;&lt;script&gt;alert(1)" in page


def test_page_highlights_the_key_and_lists_notes() -> None:
    page = render(_hostile_test(), NOTES)
    assert re.search(r"<li class='correct'><b>B</b> the right one ✓</li>", page)
    assert "class='key'>Принимается: <b>cat&lt;/script" in page
    assert "Заметки агента (NOTES.md)" in page
    assert "проигрываний: 2" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_script_still_parses(tmp_path: Path) -> None:
    page = render(_hostile_test(), NOTES)
    script = page[page.index("<script>") + len("<script>") : page.index("</script>")]
    js = tmp_path / "review.js"
    js.write_text(script, encoding="utf-8")
    subprocess.run(["node", "--check", str(js)], check=True)


def test_main_writes_the_page_and_warns_about_missing_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    src = tmp_path / "test.json"
    src.write_text(json.dumps(_hostile_test()), encoding="utf-8")
    (tmp_path / "NOTES.md").write_text(NOTES, encoding="utf-8")
    (tmp_path / "here.png").write_bytes(b"\x89PNG")
    # main() configures the shared console logger; keep pytest's capture handler.
    monkeypatch.setattr(review, "configure_logging", lambda *a, **k: None)
    with caplog.at_level(logging.INFO, logger="scripts.review_html"):
        assert review.main(["--file", str(src)]) == 0
    out = tmp_path / "REVIEW.html"
    page = out.read_text(encoding="utf-8")
    assert "Заметки агента" in page  # NOTES.md next to the JSON is picked up
    warn = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn) == 1
    msg = warn[0].getMessage()
    assert "2 missing file(s)" in msg and "missing.mp3" in msg and "gone.png" in msg
    assert "here.png" not in msg
    assert any("review page written" in r.getMessage() for r in caplog.records)


def _b2c2_test(x: str = "") -> dict:
    """A C1-style test with every B2-C2 field; `x` is appended to each new string field."""
    return {
        "id": "c1-demo",
        "title": "C1 demo",
        "level": "C1_ADVANCED",
        "status": "draft",
        "duration_minutes": 90,
        "sections": [
            {
                "id": "uoe3",
                "title": "Word formation",
                "skill": "reading",
                "reported_skill": f"use_of_english{x}",
                "stimulus": {"kind": "gapped_text", "text": "the (17) ... (COME)"},
                "items": [
                    {
                        "id": "q17",
                        "item_type": "gap_fill",
                        "prompt": "Gap 17",
                        "accepted": ["overcome"],
                        "stem_word": f"COME{x}",
                        "max_words": 1,
                    },
                    {
                        "id": "q25",
                        "item_type": "key_word_transformation",
                        "prompt": "My brother now earns far less.",
                        "key_word": f"NEARLY{x}",
                        "gapped_sentence": f"My brother ____ much now.{x}",
                        "answers": [
                            {
                                "part_a": ["does not earn", f"doesn't earn{x}"],
                                "part_b": ["nearly as"],
                            },
                            {"part_a": ["is not earning"], "part_b": [f"nearly so{x}"]},
                        ],
                        "points": 2,
                    },
                ],
            },
            {
                "id": "r7",
                "title": "Gapped text",
                "skill": "reading",
                "stimulus": {
                    "kind": "matching_pool",
                    "text": "(41) ...",
                    "options": [{"key": "A", "text": "para A"}, {"key": "B", "text": "para B"}],
                    "each_option_once": True,
                },
                "items": [
                    {
                        "id": "q41",
                        "item_type": "matching",
                        "prompt": "Gap 41",
                        "correct": "A",
                        "points": 2,
                    }
                ],
            },
            {
                "id": "l4",
                "title": "Listening 4",
                "skill": "listening",
                "stimulus": {
                    "kind": "matching_pool",
                    "options": [{"key": "A", "text": "x"}, {"key": "B", "text": "y"}],
                    "each_option_once": False,
                    "audio": {"kind": "audio_asset", "asset_id": f"p4{x}.mp3", "plays": 1},
                },
                "items": [
                    {"id": "q21", "item_type": "matching", "prompt": "Speaker 1", "correct": "B"}
                ],
            },
            {
                "id": "w2",
                "title": "Writing 2",
                "skill": "writing",
                "stimulus": {"kind": "passage_text", "text": "Choose one"},
                "items": [
                    {
                        "id": f"w{n}",
                        "item_type": "open_writing",
                        "prompt": f"Q{n}",
                        "word_min": 220,
                        "rubric": "r",
                        "optional": True,
                        "points": 20,
                    }
                    for n in (2, 3, 4)
                ],
            },
        ],
    }


def test_b2_c2_fields_are_shown() -> None:
    page = render(_b2c2_test(), None)
    assert "трансформация с ключевым словом" in page
    assert "Ключевое слово: <b>NEARLY</b>" in page
    assert "Второе предложение: My brother ____ much now." in page
    # every A | B pattern, alternatives joined with " / "
    assert "<li><b>does not earn / doesn&#x27;t earn</b> | <b>nearly as</b></li>" in page
    assert "<li><b>is not earning</b> | <b>nearly so</b></li>" in page
    assert "Исходное слово: <b>COME</b>" in page
    assert "Не больше слов: 1" in page
    assert "Баллы засчитываются как: <b>Use of English</b>" in page
    assert page.count("Баллы: 2<") == 2  # the KWT and the 2-mark gapped-text question
    assert page.count("Баллы: 20<") == 3
    assert "Каждый вариант — не больше одного раза" in page
    assert "Варианты можно выбирать повторно" in page
    assert "<audio controls preload='none' src='p4.mp3'>" in page  # the pool's own recording
    assert page.count("на выбор (одно из 3)") == 3
    assert "одно из двух" not in page


def test_choose_one_label_counts_the_group() -> None:
    t = _b2c2_test()
    t["sections"][3]["items"] = t["sections"][3]["items"][:2]
    assert render(t, None).count("на выбор (одно из 2)") == 2


def test_b2_c2_fields_are_escaped() -> None:
    page = render(_b2c2_test(HOSTILE), None)
    assert page.count("<script") == 1 and page.count("</script") == 1
    tags = _tags(page)
    assert [t for t, _ in tags if t == "script"] == ["script"]
    assert not [a for _, attrs in tags for a, _ in attrs if a.startswith("on")]
    assert ("img", [("src", "x")]) not in [(t, a[:1]) for t, a in tags]
    for shown in (
        "NEARLY",
        "COME",
        "much now.",
        "doesn&#x27;t earn",
        "nearly so",
        "use_of_english",
    ):
        assert f"{shown}&lt;/script&gt;&lt;script&gt;alert(1)" in page, shown
    # the hostile pool-audio id stays inside a quoted, escaped attribute
    audio_srcs = [dict(a).get("src") for t, a in tags if t == "audio"]
    assert f"p4{HOSTILE}.mp3" in audio_srcs
