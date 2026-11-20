"""Matching-pool pictures + the single_choice option-shuffle opt-out.

A) `PoolOption.image` (a picture per word, YLE word boxes) and the pool-level
   context `MatchingPoolStimulus.images`: served to the student (not answer data),
   counted as referenced assets (missing file -> refused; an uploaded picture is not
   "unreferenced"), checked as safe asset ids, structural on a locked test, shown in
   the review page, and rendered by the runner (word-bank chip / letter button with
   the picture, alt = the word; the pool's own pictures above the passage).
B) `SingleChoiceItem.shuffle_options=False` keeps the authored order for every
   student; the displayed index still maps to the canonical key, so grading is right;
   default True still shuffles; the flag is authoring-only and structural.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.content import ContentService, FilesystemStorage, shuffle_options
from app.content.editor import (
    ContentValidationError,
    is_cosmetic_change,
    referenced_asset_ids,
    semantic_errors,
)
from app.delivery import DeliveryService
from app.delivery.projection import project_test
from app.grading.service import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    ClientTest,
    MatchingItem,
    MatchingPoolStimulus,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest
from contracts.export_jsonschema import schema_for

ROOT = Path(__file__).resolve().parents[1]
EXAM_JS = ROOT / "apps" / "web" / "exam.js"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)


def _pool_test(**update: object) -> ExamTest:
    words = [("A", "cat"), ("B", "dog"), ("C", "fish"), ("D", "bird")]
    test = ExamTest(
        id="yp",
        title="Starters — words",
        level="PRE_A1_STARTERS",
        status="draft",
        duration_minutes=20,
        sections=[
            Section(
                id="yp-s1",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    text="Look and read.",
                    images=["yp-story.png"],
                    options=[PoolOption(key=k, text=w, image=f"yp-{w}.png") for k, w in words],
                ),
                items=[
                    MatchingItem(id=f"yp-q{i}", prompt=f"Picture {i}", correct=k)
                    for i, (k, _) in enumerate(words[:3], start=1)
                ],
            ),
            Section(
                id="yp-s2",
                skill="listening",
                stimulus=PassageTextStimulus(text=""),
                items=[
                    SingleChoiceItem(
                        id="yp-yn",
                        prompt="Is it a cat?",
                        options=[TextOption(key="Y", text="yes"), TextOption(key="N", text="no")],
                        correct="N",
                        shuffle_options=False,
                    ),
                    SingleChoiceItem(
                        id="yp-sh",
                        prompt="Pick",
                        options=[TextOption(key=k, text=k.lower()) for k in "ABCDE"],
                        correct="C",
                    ),
                ],
            ),
        ],
    )
    return ExamTest.model_validate({**test.model_dump(), **update})


# --------------------------------------------------------------------------- #
# A) pool pictures.
# --------------------------------------------------------------------------- #
def test_pool_pictures_are_served_without_answer_data() -> None:
    from app.content import build_attempt_layout
    from app.delivery.service import _default_bank

    test = _pool_test()
    client = project_test(test, build_attempt_layout(_default_bank(test), 7))
    st = client.sections[0].stimulus
    assert st.images == ["yp-story.png"]
    assert [o.image for o in st.options] == [
        "yp-cat.png",
        "yp-dog.png",
        "yp-fish.png",
        "yp-bird.png",
    ]
    dump = json.dumps(client.model_dump(mode="json"))
    assert "correct" not in dump and "shuffle_options" not in dump
    props = json.dumps(schema_for(ClientTest))
    assert '"image"' in props  # served field exists on the client schema


def test_pool_pictures_count_as_referenced_and_are_checked() -> None:
    test = _pool_test()
    refs = referenced_asset_ids(test)
    assert {"yp-story.png", "yp-cat.png", "yp-bird.png"} <= refs
    bad = _pool_test().model_dump()
    bad["sections"][0]["stimulus"]["options"][0]["image"] = "../evil.png"
    bad["sections"][0]["stimulus"]["images"] = ["../story.png"]
    errors = semantic_errors(ExamTest.model_validate(bad))
    assert any("картинка варианта A" in e for e in errors)
    assert any("картинки" in e for e in errors)


def test_missing_and_unreferenced_pool_pictures(session: Session, tmp_path: Path) -> None:
    content = ContentService(ContentRepository(session), FilesystemStorage(tmp_path / "a"))
    raw = json.dumps(_pool_test().model_dump(mode="json"))
    with pytest.raises(ContentValidationError, match="yp-cat.png"):
        content.import_test(raw, {"yp-story.png": PNG})
    assets = {f"yp-{w}.png": PNG for w in ("story", "cat", "dog", "fish", "bird")}
    assets["stray.png"] = PNG
    outcome = content.import_test(raw, assets)
    assert "yp-cat.png" in outcome.stored_assets and outcome.ignored_assets == ["stray.png"]


def test_pool_pictures_are_structural_on_a_locked_test() -> None:
    old = _pool_test()
    moved = old.model_dump()
    moved["sections"][0]["stimulus"]["options"][0]["image"] = "yp-dog.png"
    assert not is_cosmetic_change(old, ExamTest.model_validate(moved))
    ctx = old.model_dump()
    ctx["sections"][0]["stimulus"]["images"] = []
    assert not is_cosmetic_change(old, ExamTest.model_validate(ctx))


def test_review_page_shows_pool_pictures(tmp_path: Path) -> None:
    from scripts.review_html import render

    page = render(_pool_test().model_dump(mode="json"), None)
    assert "yp-story.png" in page and "yp-cat.png" in page
    assert "не перемешиваются" in page


_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8").replace(/\r\n/g, "\n");
const grab = (name) => {
  const i = src.indexOf(`\nfunction ${name}(`);
  if (i < 0) throw new Error("missing function " + name);
  return src.slice(i, src.indexOf("\n}\n", i) + 3);
};
const body =
  "const t = (k, ...a) => k + '(' + a.join('|') + ')';\n" +
  ["esc", "poolPicHTML", "poolOf", "finePointer", "wordBankHTML", "contextImagesHTML", "picAlt"]
    .map(grab).join("\n") +
  "\nreturn { poolPicHTML, wordBankHTML, contextImagesHTML };";
const api = new Function("window", body)({});
const section = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify({
  bank: api.wordBankHTML(section),
  pic: api.poolPicHTML(section.stimulus.options[0], "ol-pic"),
  none: api.poolPicHTML({ key: "Z", text: "zebra" }, "ol-pic"),
  ctx: api.contextImagesHTML({ ...section.stimulus, __title: "Part 1" }),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_runner_renders_pool_pictures(tmp_path: Path) -> None:
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    data = tmp_path / "section.json"
    data.write_text(
        json.dumps(_pool_test().model_dump(mode="json")["sections"][0]), encoding="utf-8"
    )
    proc = subprocess.run(
        ["node", str(harness), str(EXAM_JS), str(data)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # Word-bank chip: picture above the word, alt = the word.
    assert out["bank"].count('class="wb-pic"') == 4
    assert 'src="/assets/yp-cat.png" alt="cat"' in out["bank"]
    assert 'class="wb-chip has-pic"' in out["bank"]
    assert out["pic"].startswith('<img class="ol-pic" src="/assets/yp-cat.png" alt="cat"')
    assert out["none"] == ""
    assert "/assets/yp-story.png" in out["ctx"]
    src = EXAM_JS.read_text(encoding="utf-8")
    # The pool's own pictures come first, above the passage; letter buttons show it.
    assert "contextImagesHTML(st) +\n      (st.text ? passageHTML" in src.replace("\r\n", "\n")
    assert 'poolPicHTML(p, "ol-pic")' in src


# --------------------------------------------------------------------------- #
# B) shuffle opt-out.
# --------------------------------------------------------------------------- #
def test_shuffle_off_keeps_the_authored_order_for_every_seed() -> None:
    test = _pool_test()
    yes_no = test.sections[1].items[0]
    shuffled = test.sections[1].items[1]
    assert all(shuffle_options(yes_no, seed).order == ("Y", "N") for seed in range(200))
    orders = {shuffle_options(shuffled, seed).order for seed in range(50)}
    assert len(orders) > 1  # the default still shuffles
    assert (
        SingleChoiceItem.model_validate(
            {"id": "x", "prompt": "p", "options": yes_no.model_dump()["options"], "correct": "Y"}
        ).shuffle_options
        is True
    )


def test_shuffle_off_served_in_order_and_graded_by_index(session: Session) -> None:
    ContentRepository(session).add_test(_pool_test(status="published"))
    repo = AttemptRepository(session)
    for i in range(6):
        repo.add_roster_entry(RosterEntry(id=f"e{i}", test_id="yp", display_name=f"S{i}"))
    svc = DeliveryService(ContentRepository(session), repo)
    grading = GradingService(ContentRepository(session), repo)
    for i in range(6):
        a = svc.start(f"e{i}")
        yn = next(it for s in svc.client_test(a.id).sections for it in s.items if it.id == "yp-yn")
        assert [o.text for o in yn.options] == ["yes", "no"]
        svc.save_answer(a.id, "yp-yn", 1)  # displayed index 1 = "no" = the key
        assert svc.get_saved_answers(a.id)["yp-yn"] == 1
        grade = next(g for g in grading.grade(a.id).items if g.item_id == "yp-yn")
        assert grade.awarded == 1


def test_shuffle_flag_is_structural_and_not_served() -> None:
    old = _pool_test()
    data = old.model_dump()
    data["sections"][1]["items"][0]["shuffle_options"] = True
    assert not is_cosmetic_change(old, ExamTest.model_validate(data))
    assert "shuffle_options" not in json.dumps(schema_for(ClientTest))
