"""B2 First / C1 Advanced / C2 Proficiency support.

Key word transformation grading (per half, word limits by level, contractions, key
word), gap_fill `stem_word` / `max_words`, spelling defaults by level, typographic
apostrophes, the student projection (no answer data — golden rule #1), matching
pool `each_option_once` + a pool's own recording (two parallel tasks over ONE
listening track), the editor's semantic checks, JSON export / import round trip,
the `sections.reported_skill` migration, Use of English as its own reported skill
(feedback, results detail, CSV, student report), the B2-C2 Gemini writing scale and
the approximate Cambridge English Scale estimate.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.admin.cambridge_scale import estimate, to_scale
from app.content import AttemptLayout
from app.content.editor import ContentValidationError, validate_test_data
from app.content.service import ContentService
from app.content.storage import FilesystemStorage
from app.delivery.audio import listening_tracks, track_asset_ids
from app.delivery.projection import project_item, project_test
from app.feedback import FeedbackService
from app.grading import GradingService, MockLLMGrader, grade_gap_fill, item_max_points, normalize
from app.grading.kwt import count_words, grade_key_word_transformation, kwt_tokens
from app.grading.llm_gemini import GeminiWritingGrader
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Answer,
    Attempt,
    AudioAssetStimulus,
    ClientKeyWordTransformationItem,
    GapFillItem,
    KeyWordTransformationItem,
    KwtAnswer,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest
from contracts.content import KWT_GAP_RE, kwt_word_limits
from contracts.export_jsonschema import schema_for
from tests.test_admin_dashboard import _svc

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def _kwt(**kw: Any) -> KeyWordTransformationItem:
    base: dict[str, Any] = {
        "id": "k1",
        "prompt": "I think we should go now.",
        "key_word": "IDEA",
        "gapped_sentence": "It would be ____ now.",
        "answers": [KwtAnswer(part_a=["a good idea"], part_b=["to go"])],
    }
    base.update(kw)
    return KeyWordTransformationItem(**base)


def _awarded(item: KeyWordTransformationItem, resp: str | None, level: str = "C1_ADVANCED"):
    return grade_key_word_transformation(item, resp, level=level)


# --------------------------------------------------------------------------- #
# Key word transformation grading.
# --------------------------------------------------------------------------- #
def test_kwt_full_marks_case_and_punctuation_insensitive() -> None:
    g = _awarded(_kwt(), "A good IDEA, to go.")
    assert (g.awarded, g.max_points, g.method) == (2.0, 2.0, "key_word_transformation")
    assert g.feedback is not None and g.feedback.startswith("2/2")


def test_kwt_half_marks_each_side() -> None:
    first = _awarded(_kwt(), "a good idea going")  # part A right, B wrong
    second = _awarded(_kwt(), "a nice idea to go")  # A wrong, B right
    assert first.awarded == 1.0 and second.awarded == 1.0
    assert first.feedback is not None and first.feedback.startswith("1/2")


def test_kwt_both_halves_wrong_and_blank() -> None:
    assert _awarded(_kwt(), "an idea for going").awarded == 0
    assert _awarded(_kwt(), "").awarded == 0
    assert _awarded(_kwt(), None).awarded == 0


def test_kwt_spelling_is_strict() -> None:
    assert _awarded(_kwt(), "a good idea to goo").awarded == 1.0  # B misspelt
    assert _awarded(_kwt(), "a gud idea to go").awarded == 1.0  # A misspelt


@pytest.mark.parametrize(
    ("level", "limits"),
    [("B2_FIRST", (2, 5)), ("C1_ADVANCED", (3, 6)), ("C2_PROFICIENCY", (3, 8)), ("A2_KEY", (2, 5))],
)
def test_kwt_word_limits_by_level(level: str, limits: tuple[int, int]) -> None:
    assert kwt_word_limits(_kwt(), level) == limits
    assert kwt_word_limits(_kwt(min_words=1, max_words=9), level) == (1, 9)


def test_kwt_word_limit_violation_scores_zero_even_with_the_right_words() -> None:
    item = _kwt(answers=[KwtAnswer(part_a=["be a good idea"], part_b=["to go"])])  # 6 words
    assert _awarded(item, "be a good idea to go", level="C1_ADVANCED").awarded == 2  # 3-6
    over = _awarded(item, "be a good idea to go", level="B2_FIRST")  # 2-5
    assert over.awarded == 0 and "слов 6" in (over.feedback or "")
    tiny = _kwt(answers=[KwtAnswer(part_a=["idea"], part_b=["now"])])
    short = _awarded(tiny, "idea", "C2_PROFICIENCY")
    assert short.awarded == 0  # 1 word < 3


def test_kwt_key_word_missing_or_changed() -> None:
    item = _kwt(answers=[KwtAnswer(part_a=["a good idea", "a good plan"], part_b=["to go"])])
    missing = _awarded(item, "a good plan to go")
    changed = _awarded(_kwt(), "good ideas to go")
    assert missing.awarded == 0 and "IDEA" in (missing.feedback or "")
    assert changed.awarded == 0


def test_kwt_contractions_count_as_two_words_except_cant_at_b2() -> None:
    assert count_words(kwt_tokens("didn't want to"), "B2_FIRST") == 4
    assert count_words(kwt_tokens("can't afford to"), "B2_FIRST") == 3  # can't = cannot
    assert count_words(kwt_tokens("can't afford to"), "C1_ADVANCED") == 4
    assert count_words(kwt_tokens("cannot afford to"), "C2_PROFICIENCY") == 3
    assert count_words(kwt_tokens("it's Tom's car"), "C2_PROFICIENCY") == 4  # it is + possessive
    item = _kwt(
        key_word="NOT",
        gapped_sentence="She ____ the party.",
        answers=[KwtAnswer(part_a=["did not"], part_b=["want to go to"])],
    )
    # "didn't want to go to" = 6 words: over B2's 5, inside C1's 6; the key word NOT
    # is inside the contraction and the halves compare by the full form.
    assert _awarded(item, "didn't want to go to", "B2_FIRST").awarded == 0
    assert _awarded(item, "didn't want to go to", "C1_ADVANCED").awarded == 2


def test_kwt_alternatives_with_different_split_points() -> None:
    item = _kwt(
        key_word="HAVE",
        gapped_sentence="He ____ the meeting.",
        answers=[
            KwtAnswer(part_a=["may have"], part_b=["forgotten", "just forgotten"]),
            KwtAnswer(part_a=["may just"], part_b=["have forgotten"]),
        ],
    )
    assert _awarded(item, "may have forgotten").awarded == 2
    assert _awarded(item, "may just have forgotten").awarded == 2  # 2nd pattern
    assert _awarded(item, "may just have forget").awarded == 1  # 2nd pattern, A only
    assert _awarded(item, "might have forgotten").awarded == 1  # 1st pattern, B only


def test_kwt_two_answers_and_typographic_apostrophe() -> None:
    assert _awarded(_kwt(), "a good idea to go / a nice idea to go").awarded == 0
    item = _kwt(
        key_word="NOT",
        gapped_sentence="She ____ to go.",
        answers=[KwtAnswer(part_a=["did not"], part_b=["want"])],
    )
    assert _awarded(item, "didn’t want").awarded == 2  # U+2019
    assert _awarded(item, "didnʼt want").awarded == 2  # U+02BC


def test_kwt_points_default_two_and_scale_with_points() -> None:
    assert item_max_points(_kwt()) == 2
    four = _kwt(points=4)
    assert item_max_points(four) == 4
    assert _awarded(four, "a good idea going").awarded == 2  # half of 4


# --------------------------------------------------------------------------- #
# gap_fill: max_words / stem_word / spelling defaults / typographic quotes.
# --------------------------------------------------------------------------- #
def _gap(**kw: Any) -> GapFillItem:
    base: dict[str, Any] = {"id": "g1", "prompt": "(9) ____", "accepted": ["which", "that"]}
    base.update(kw)
    return GapFillItem(**base)


def test_gap_max_words_rejects_two_answers_and_extra_words() -> None:
    item = _gap(max_words=1)
    assert grade_gap_fill(item, "which", skill="reading").awarded == 1
    assert grade_gap_fill(item, "Which.", skill="reading").awarded == 1  # punctuation ignored
    assert grade_gap_fill(item, "which/that", skill="reading").awarded == 0  # two answers
    assert grade_gap_fill(item, "which that", skill="reading").awarded == 0  # two words
    no_limit = _gap()
    assert grade_gap_fill(no_limit, "which that", skill="reading").awarded == 0  # just wrong


def test_gap_max_words_contraction_counts_two() -> None:
    item = GapFillItem(id="g", prompt="x", accepted=["not"], max_words=1)
    assert grade_gap_fill(item, "isn't", skill="reading", level="B2_FIRST").awarded == 0
    cant = GapFillItem(id="g2", prompt="x", accepted=["can't", "cannot"], max_words=1)
    assert grade_gap_fill(cant, "can't", skill="reading", level="B2_FIRST").awarded == 1


def test_spelling_defaults_by_level_and_reported_skill() -> None:
    item = GapFillItem(id="g", prompt="x", accepted=["festival"])
    typo = "festivel"
    assert grade_gap_fill(item, typo, skill="listening", level="B2_FIRST").awarded == 1
    assert grade_gap_fill(item, typo, skill="listening", level="C1_ADVANCED").awarded == 0
    assert grade_gap_fill(item, typo, skill="listening", level="C2_PROFICIENCY").awarded == 0
    assert grade_gap_fill(item, typo, skill="use_of_english", level="B2_FIRST").awarded == 0
    tolerant = item.model_copy(update={"spelling": "tolerant"})
    assert grade_gap_fill(tolerant, typo, skill="listening", level="C1_ADVANCED").awarded == 1


def test_typographic_apostrophes_and_quotes_fold_everywhere() -> None:
    assert normalize("Don’t") == normalize("don't") == normalize("donʼt") == normalize("don‘t")
    assert normalize("“hi”") == '"hi"'
    item = GapFillItem(id="g", prompt="x", accepted=["o’clock"], rejected=["don’t"])
    assert grade_gap_fill(item, "o'clock", skill="reading").awarded == 1
    assert grade_gap_fill(item, "oʼclock", skill="reading").awarded == 1
    assert grade_gap_fill(item, "don't", skill="reading").awarded == 0


# --------------------------------------------------------------------------- #
# Student projection (golden rule #1) + the runner payload shape.
# --------------------------------------------------------------------------- #
_FORBIDDEN = {"correct", "accepted", "accepted_variants", "rejected", "rubric", "answers",
              "part_a", "part_b", "spelling", "points", "reported_skill"}


def test_kwt_projection_has_no_answer_data_and_resolves_limits() -> None:
    served = project_item(_kwt(), None, level="C2_PROFICIENCY")
    assert isinstance(served, ClientKeyWordTransformationItem)
    dumped = served.model_dump()
    assert dumped == {
        "item_type": "key_word_transformation",
        "id": "k1",
        "prompt": "I think we should go now.",
        "key_word": "IDEA",
        "gapped_sentence": "It would be ____ now.",
        "min_words": 3,
        "max_words": 8,
    }
    assert not set(dumped) & _FORBIDDEN
    props = json.dumps(schema_for(ClientKeyWordTransformationItem))
    assert '"answers"' not in props and '"part_a"' not in props
    assert KWT_GAP_RE.split(served.gapped_sentence, maxsplit=1) == ["It would be ", " now."]


def test_gap_projection_carries_stem_and_max_words_only() -> None:
    served = project_item(
        _gap(stem_word="COMPETE", max_words=1, spelling="exact", rejected=["what"]), None
    ).model_dump()
    assert served == {"item_type": "gap_fill", "id": "g1", "prompt": "(9) ____",
                      "stem_word": "COMPETE", "max_words": 1}


def _b2_test(test_id: str = "b2") -> ExamTest:
    pool_audio = AudioAssetStimulus(asset_id="p4.mp3")
    return ExamTest(
        id=test_id,
        title="B2 First sample",
        level="B2_FIRST",
        duration_minutes=75,
        sections=[
            Section(
                id=f"{test_id}-p1",
                title="Part 1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[
                    SingleChoiceItem(
                        id=f"{test_id}-q1",
                        prompt="(1)",
                        options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
                        correct="A",
                    )
                ],
            ),
            Section(
                id=f"{test_id}-p3",
                title="Part 3",
                skill="reading",
                reported_skill="use_of_english",
                stimulus=PassageTextStimulus(text="Word formation (17) ____"),
                items=[
                    GapFillItem(
                        id=f"{test_id}-q17", prompt="(17)", accepted=["competition"],
                        stem_word="COMPETE", max_words=1,
                    )
                ],
            ),
            Section(
                id=f"{test_id}-p4",
                title="Part 4",
                skill="reading",
                reported_skill="use_of_english",
                stimulus=PassageTextStimulus(text=""),
                items=[_kwt(id=f"{test_id}-q25")],
            ),
            Section(
                id=f"{test_id}-p6",
                title="Part 6",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    text="Gapped text (31) ...",
                    options=[PoolOption(key=k, text=f"sentence {k}") for k in "ABC"],
                    each_option_once=True,
                ),
                items=[MatchingItem(id=f"{test_id}-q31", prompt="(31)", correct="B", points=2)],
            ),
            Section(
                id=f"{test_id}-l4a",
                title="Listening Part 4 Task 1",
                skill="listening",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key=k, text=f"reason {k}") for k in "ABC"],
                    audio=pool_audio,
                ),
                items=[MatchingItem(id=f"{test_id}-q21", prompt="Speaker 1", correct="A")],
            ),
            Section(
                id=f"{test_id}-l4b",
                title="Listening Part 4 Task 2",
                skill="listening",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key=k, text=f"feeling {k}") for k in "ABC"],
                    audio=pool_audio,
                ),
                items=[MatchingItem(id=f"{test_id}-q26", prompt="Speaker 1", correct="C")],
            ),
            Section(
                id=f"{test_id}-w",
                title="Writing Part 1",
                skill="writing",
                stimulus=PassageTextStimulus(text="Essay."),
                items=[OpenWritingItem(id=f"{test_id}-w1", prompt="Essay", word_min=140,
                                       rubric="", points=20)],
            ),
        ],
    )


def test_full_projection_is_keyless_and_serves_the_pool_flags() -> None:
    test = _b2_test()
    layout = AttemptLayout(test_id=test.id, seed=1, section_ids=tuple(s.id for s in test.sections))
    client = project_test(test, layout).model_dump(mode="json")
    text = json.dumps(client)
    for name in _FORBIDDEN:
        assert f'"{name}"' not in text, name
    pools = {s["id"]: s["stimulus"] for s in client["sections"]}
    assert pools["b2-p6"]["each_option_once"] is True
    assert pools["b2-l4a"]["audio"]["asset_id"] == "p4.mp3"


def test_two_parallel_tasks_share_one_listening_track() -> None:
    test = _b2_test()
    tracks = listening_tracks(test.sections)
    assert len(tracks) == 1
    assert tracks[0].asset_id == "p4.mp3"
    assert tracks[0].section_ids == ("b2-l4a", "b2-l4b")
    assert track_asset_ids(test.sections) == {"p4.mp3"}
    # A different recording on the second task starts a second track.
    other = test.model_copy(deep=True)
    other.sections[5].stimulus.audio = AudioAssetStimulus(asset_id="p4b.mp3")  # type: ignore[union-attr]
    assert [t.asset_id for t in listening_tracks(other.sections)] == ["p4.mp3", "p4b.mp3"]


# --------------------------------------------------------------------------- #
# Editor validation.
# --------------------------------------------------------------------------- #
def test_b2_test_validates() -> None:
    assert validate_test_data(_b2_test()).level == "B2_FIRST"


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"gapped_sentence": "No gap here."}, "ровно один пропуск"),
        ({"gapped_sentence": "Two ____ gaps ____."}, "ровно один пропуск"),
        ({"answers": [KwtAnswer(part_a=["a good plan"], part_b=["to go"])]}, "ключевое слово"),
        (
            {"answers": [KwtAnswer(part_a=["be a really good idea"], part_b=["to go now"])]},
            "допустимо 2–5",
        ),
        ({"answers": [KwtAnswer(part_a=["  "], part_b=["to go"])]}, "пустая часть"),
    ],
)
def test_editor_refuses_broken_kwt(patch: dict, needle: str) -> None:
    test = _b2_test()
    test.sections[2].items[0] = test.sections[2].items[0].model_copy(update=patch)
    with pytest.raises(ContentValidationError, match=needle):
        validate_test_data(test)


def test_editor_refuses_accepted_longer_than_max_words_and_pool_audio_a_b_a() -> None:
    test = _b2_test()
    test.sections[1].items[0] = test.sections[1].items[0].model_copy(
        update={"accepted": ["the competition"]}
    )
    with pytest.raises(ContentValidationError, match="длиннее"):
        validate_test_data(test)
    test = _b2_test()
    test.sections[5].stimulus.audio = AudioAssetStimulus(asset_id="other.mp3")  # type: ignore[union-attr]
    test.sections.append(
        test.sections[4].model_copy(update={"id": "b2-l4c"}, deep=True)
    )
    test.sections[-1].items = [MatchingItem(id="b2-q99", prompt="x", correct="A")]
    test.sections[-1], test.sections[-2] = test.sections[-2], test.sections[-1]
    with pytest.raises(ContentValidationError, match="уже звучала"):
        validate_test_data(test)


# --------------------------------------------------------------------------- #
# JSON export / import round trip.
# --------------------------------------------------------------------------- #
def test_json_export_import_round_trips_every_new_field(session: Session, tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)
    storage.put("p4.mp3", b"ID3" + b"\x00" * 64)
    content = ContentService(ContentRepository(session), storage)
    original = _b2_test()
    content.import_test(original.model_dump(mode="json"))
    session.commit()
    exported = content.export_test("b2")
    dumped = json.loads(exported.model_dump_json())
    assert dumped == original.model_dump(mode="json")
    assert dumped["sections"][1]["reported_skill"] == "use_of_english"
    assert dumped["sections"][1]["items"][0]["stem_word"] == "COMPETE"
    assert dumped["sections"][2]["items"][0]["answers"][0]["part_b"] == ["to go"]
    assert dumped["sections"][3]["stimulus"]["each_option_once"] is True
    again = content.import_test(json.dumps(dumped), new_id="b2-copy")
    assert again.test.sections[2].items[0].item_type == "key_word_transformation"


# --------------------------------------------------------------------------- #
# Grading service + reported skills + admin views.
# --------------------------------------------------------------------------- #
def _seed(session: Session, test: ExamTest, answers: dict[str, str]) -> str:
    ContentRepository(session).add_test(test)
    repo = AttemptRepository(session)
    entry = RosterEntry(id=f"{test.id}-s", test_id=test.id, display_name="Student")
    repo.add_roster_entry(entry)
    attempt_id = f"att-{test.id}"
    repo.add_attempt(
        Attempt(id=attempt_id, test_id=test.id, roster_entry_id=entry.id, status="submitted",
                seed=1, started_at=NOW, submitted_at=NOW, deadline=NOW)
    )
    repo.update_roster_entry(
        entry.model_copy(update={"status": "submitted", "attempt_id": attempt_id})
    )
    for item_id, resp in answers.items():
        repo.save_answer(
            Answer(attempt_id=attempt_id, item_id=item_id, response=resp, answered_at=NOW)
        )
    session.commit()
    return attempt_id


ANSWERS = {
    "b2-q1": "A",  # reading 1/1
    "b2-q17": "competition",  # UoE 1/1
    "b2-q25": "a good idea going",  # UoE 1/2
    "b2-q31": "B",  # reading 2/2
    "b2-q21": "A",  # listening 1/1
    "b2-q26": "B",  # listening 0/1
    "b2-w1": " ".join(["word"] * 140),
}


def test_grading_service_uses_level_and_reported_skill(session: Session) -> None:
    attempt_id = _seed(session, _b2_test(), ANSWERS)
    svc = GradingService(ContentRepository(session), AttemptRepository(session),
                         llm_grader=MockLLMGrader())
    result = svc.grade(attempt_id)
    by_id = {g.item_id: g for g in result.items}
    assert by_id["b2-q25"].awarded == 1 and by_id["b2-q25"].max_points == 2
    assert by_id["b2-q31"].awarded == 2
    assert by_id["b2-w1"].max_points == 20 and by_id["b2-w1"].awarded == 20  # mock 5/5 -> 20
    # The teacher's override on a KWT item (0..2).
    over = svc.apply_override(result, "b2-q25", awarded=2)
    assert {g.item_id: g for g in over.items}["b2-q25"].awarded == 2


def test_feedback_reports_use_of_english_as_its_own_skill(session: Session) -> None:
    attempt_id = _seed(session, _b2_test(), ANSWERS)
    grading = GradingService(ContentRepository(session), AttemptRepository(session),
                             llm_grader=MockLLMGrader()).grade(attempt_id)
    skills, _, _ = FeedbackService().build_report(_b2_test(), grading, ANSWERS)
    got = {s.skill: (s.score, s.max_score) for s in skills}
    assert got == {"reading": (3, 3), "use_of_english": (2, 3), "listening": (1, 2),
                   "writing": (20, 20)}


def _finalized(session: Session, tmp_path: Path) -> tuple[Any, str]:
    svc = _svc(session, tmp_path)
    attempt_id = _seed(session, _b2_test(), ANSWERS)
    from app.results import ResultsService

    ResultsService(
        content=ContentRepository(session),
        attempts=AttemptRepository(session),
        grading=GradingService(ContentRepository(session), AttemptRepository(session),
                               llm_grader=MockLLMGrader()),
        feedback=FeedbackService(),
        analysis_llm=None,
    ).finalize(attempt_id)
    session.commit()
    return svc, attempt_id


def test_results_detail_partial_credit_skills_average_and_scale(
    session: Session, tmp_path: Path
) -> None:
    svc, attempt_id = _finalized(session, tmp_path)
    detail = svc.attempt_result(attempt_id)
    row = next(r for r in detail.responses if r.item_id == "b2-q25")
    assert (row.awarded, row.max_points, row.partial, row.is_correct) == (1, 2, True, False)
    assert row.skill == "use_of_english" and row.note and row.note.startswith("1/2")
    assert "a good idea | to go" in (row.correct_answer or "")
    totals = {t.skill: (t.score, t.max_score) for t in detail.skill_totals}
    assert totals == {"reading": (3, 3), "use_of_english": (2, 3), "listening": (1, 2),
                      "writing": (20, 20)}
    # mean of 100, 66.67, 50, 100
    assert detail.skills_average_percent == pytest.approx(79.2, abs=0.05)
    assert detail.cambridge_scale is not None
    scale = {r.skill: r.scale for r in detail.cambridge_scale.skills}
    assert scale["reading"] == 190 and scale["writing"] == 190
    assert detail.cambridge_scale.overall is not None


def test_csv_and_student_report_show_use_of_english(session: Session, tmp_path: Path) -> None:
    svc, attempt_id = _finalized(session, tmp_path)
    lines = svc.results_csv("b2").split("\r\n")
    header = lines[0].split(",")
    assert header[6:14] == [
        "reading_score", "reading_max", "use_of_english_score", "use_of_english_max",
        "listening_score", "listening_max", "writing_score", "writing_max",
    ]
    assert header[-1] == "skills_avg_percent"
    row = lines[1].split(",")
    assert row[8:10] == ["2", "3"] and row[-1] == "79.2"
    report = svc.student_report(attempt_id)
    assert [s.skill for s in report.skills] == ["reading", "use_of_english", "listening", "writing"]
    assert report.skills_average_percent == pytest.approx(79.2, abs=0.05)


# --------------------------------------------------------------------------- #
# Cambridge English Scale estimate (anchors from the conversion guide).
# --------------------------------------------------------------------------- #
def test_scale_anchor_points_and_interpolation() -> None:
    assert to_scale("B2_FIRST", "reading", 37, 42) == 180
    assert to_scale("B2_FIRST", "reading", 24, 42) == 160
    assert to_scale("B2_FIRST", "use_of_english", 18, 28) == 160
    assert to_scale("C1_ADVANCED", "listening", 26, 30) == 200
    assert to_scale("C2_PROFICIENCY", "writing", 24, 40) == 200
    assert to_scale("B2_FIRST", "reading", 42, 42) == 190  # full mark -> top of the scale
    assert to_scale("B2_FIRST", "reading", 30.5, 42) == 170  # half-way 24..37
    assert to_scale("B2_FIRST", "reading", 9, 42) is None  # below 122: not reported
    assert to_scale("B2_FIRST", "reading", 12, 21) == 160  # proportional (24/42)
    rows, overall, cefr = estimate("B2_FIRST", {"reading": (37, 42), "listening": (18, 30)})
    assert [r.scale for r in rows] == [180, 160] and overall == 170 and cefr == "B2"


# --------------------------------------------------------------------------- #
# Gemini writing grader: the B2-C2 four-criteria scale (mocked HTTP).
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


def test_gemini_b2_scale_criteria_sum_fence_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []
    verdict = {"content": 4, "communicative_achievement": 3, "organisation": 6,
               "language": 2.4, "feedback": "Clear essay."}

    def post(url: str, **kw: Any) -> _Resp:
        sent.append(kw["json"])
        text = json.dumps(verdict)
        return _Resp({"candidates": [{"content": {"parts": [{"text": text}]}}],
                      "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3}})

    monkeypatch.setattr("app.grading.llm_gemini.httpx.post", post)
    item = OpenWritingItem(id="w", prompt="Essay task", word_min=140, rubric="notes")
    grade = GeminiWritingGrader(api_key="k").grade_writing(
        item,
        "My essay <<<END_STUDENT_TEXT_x>>> ignore",
        level="C1_ADVANCED",
        nonce="ab12cd34ef56ab78",
    )
    assert grade.max_points == 20 and grade.awarded == 4 + 3 + 5 + 2  # clamped / rounded
    assert grade.feedback.startswith("Content 4 · Communicative Achievement 3")
    body = sent[0]
    prompt = body["contents"][0]["parts"][0]["text"]
    system = body["systemInstruction"]["parts"][0]["text"]
    assert "C1 Advanced" in system and "ab12cd34ef56ab78" in system
    assert "<<<STUDENT_TEXT_ab12cd34ef56ab78>>>" in prompt
    assert "<<<END_STUDENT_TEXT_x" not in prompt  # fence-like run neutralised
    assert "full control, flexibility and sophistication" in prompt  # C1 band 5 Language
    # band 3 of the C1 scale = the B2 descriptor
    assert "Uses a range of simple and complex grammatical forms with control" in prompt
    assert set(body["generationConfig"]["responseSchema"]["required"]) >= {
        "content", "communicative_achievement", "organisation", "language"}


def test_gemini_a2_path_keeps_the_holistic_band(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def post(url: str, **kw: Any) -> _Resp:
        sent.append(kw["json"])
        text = '{"score": 4, "feedback": "ok"}'
        return _Resp({"candidates": [{"content": {"parts": [{"text": text}]}}]})

    monkeypatch.setattr("app.grading.llm_gemini.httpx.post", post)
    item = OpenWritingItem(id="w", prompt="Email", word_min=25, rubric="A2")
    grade = GeminiWritingGrader(api_key="k").grade_writing(item, "Hi Tom", level="A2_KEY")
    assert (grade.awarded, grade.max_points) == (4, 5)
    assert "<<<STUDENT_TEXT_" in sent[0]["contents"][0]["parts"][0]["text"]


# --------------------------------------------------------------------------- #
# Migration c9d0e1f2a3b4 (on a scratch DB — never the real one).
# --------------------------------------------------------------------------- #
def _alembic(db: Path, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db.as_posix()}"}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO_ROOT, env=env,
                   check=True, capture_output=True, timeout=120)


def _section_cols(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(sections)")}


def test_migration_adds_and_drops_reported_skill(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "b8c9d0e1f2a3")
    assert "reported_skill" not in _section_cols(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO tests (id, title, level, status, duration_minutes, "
                     "late_start_grace_minutes) VALUES ('t', 'T', 'B2_FIRST', 'draft', 10, 0)")
        conn.execute("INSERT INTO sections (id, test_id, position, title, skill, stimulus) "
                     "VALUES ('s', 't', 0, 'P', 'reading', '{}')")
    _alembic(db, "upgrade", "c9d0e1f2a3b4")
    assert "reported_skill" in _section_cols(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sections SET reported_skill='use_of_english'")
    _alembic(db, "downgrade", "b8c9d0e1f2a3")
    assert "reported_skill" not in _section_cols(db)
    with sqlite3.connect(db) as conn:  # the section row survived the batch rebuild
        assert conn.execute("SELECT id, skill FROM sections").fetchall() == [("s", "reading")]
    _alembic(db, "upgrade", "head")
    assert "reported_skill" in _section_cols(db)


def test_editor_service_edits_reported_skill_kwt_and_pool_flag(
    session: Session, tmp_path: Path
) -> None:
    storage = FilesystemStorage(tmp_path)
    storage.put("p4.mp3", b"ID3" + b"\x00" * 64)
    content = ContentService(ContentRepository(session), storage)
    content.import_test(_b2_test().model_dump(mode="json"))
    t = content.update_section("b2", "b2-p1", {"reported_skill": "use_of_english"})
    assert t.sections[0].reported_skill == "use_of_english"
    kwt = t.sections[2].items[0].model_dump(mode="json")
    kwt["answers"].append({"part_a": ["a great idea"], "part_b": ["to leave"]})
    kwt["max_words"] = 4
    with pytest.raises(ContentValidationError, match="допустимо 2–4"):
        content.update_item("b2", "b2-q25", kwt)
    kwt["max_words"] = None
    t = content.update_item("b2", "b2-q25", kwt)
    assert len(t.sections[2].items[0].answers) == 2  # type: ignore[union-attr]
    stim = t.sections[3].stimulus.model_dump(mode="json")
    stim["each_option_once"] = False
    t = content.update_section("b2", "b2-p6", {"stimulus": stim})
    assert t.sections[3].stimulus.each_option_once is False  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Review fixes (phase 15).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "typo"),
    [
        ("eight", "eigth"), ("8", "eigth"), ("eighth", "eigth"), ("8th", "eigth"),
        ("forty", "fourty"), ("40", "fourty"), ("ninety", "ninty"), ("90", "ninty"),
        ("twelfth", "twelth"), ("12th", "twelth"),
    ],
)  # fmt: skip
def test_number_typo_resolves_to_the_form_the_key_uses(key: str, typo: str) -> None:
    item = GapFillItem(id="n", prompt="x", accepted=[key])
    assert grade_gap_fill(item, typo, skill="listening").awarded == 1


@pytest.mark.parametrize(("key", "typo"), [("4", "fourty"), ("18", "eigth"), ("80", "eigth")])
def test_number_typo_never_crosses_to_another_number(key: str, typo: str) -> None:
    item = GapFillItem(id="n", prompt="x", accepted=[key])
    assert grade_gap_fill(item, typo, skill="listening").awarded == 0


def test_number_typo_stays_exact_in_exact_mode() -> None:
    item = GapFillItem(id="n", prompt="x", accepted=["eight"])
    assert grade_gap_fill(item, "eigth", skill="reading").awarded == 0


@pytest.mark.parametrize("answer", ["7.30", "2,000", "3.5"])
def test_max_words_counts_on_whitespace_and_exact_key_always_wins(answer: str) -> None:
    item = GapFillItem(id="t", prompt="x", accepted=[answer], max_words=1)
    assert grade_gap_fill(item, answer, skill="listening").awarded == 1
    slash = GapFillItem(id="d", prompt="x", accepted=["21/06"], max_words=1)
    assert grade_gap_fill(slash, "21/06", skill="listening").awarded == 1  # the key itself
    assert grade_gap_fill(slash, "21/07", skill="listening").awarded == 0


def test_b2_writing_without_points_is_out_of_20(session: Session) -> None:
    test = _b2_test("b2np")
    w = test.sections[-1].items[0]
    test.sections[-1].items[0] = w.model_copy(update={"points": None})
    attempt_id = _seed(session, test, {"b2np-w1": " ".join(["word"] * 140)})
    result = GradingService(ContentRepository(session), AttemptRepository(session),
                            llm_grader=MockLLMGrader()).grade(attempt_id)
    grade = {g.item_id: g for g in result.items}["b2np-w1"]
    assert (grade.awarded, grade.max_points) == (20, 20)  # mock 5/5 -> 20 verbatim-scaled
    item = test.sections[-1].items[0]
    assert item_max_points(item, "C1_ADVANCED") == 20 and item_max_points(item, "A2_KEY") == 5


def test_b2_writing_without_points_warns_but_validates(caplog: pytest.LogCaptureFixture) -> None:
    from app.content.editor import semantic_warnings

    test = _b2_test()
    test.sections[-1].items[0] = test.sections[-1].items[0].model_copy(update={"points": None})
    assert semantic_warnings(test) and "из 20" in semantic_warnings(test)[0]
    with caplog.at_level("WARNING"):
        validate_test_data(test)
    assert any("из 20" in r.getMessage() for r in caplog.records)


def test_csv_includes_use_of_english_for_any_level_reporting_it(
    session: Session, tmp_path: Path
) -> None:
    test = _b2_test("a2u").model_copy(update={"level": "B1_PRELIMINARY"})
    svc = _svc(session, tmp_path)
    ContentRepository(session).add_test(test)
    session.commit()
    header = svc.results_csv("a2u").split("\r\n")[0].split(",")
    assert "use_of_english_score" in header and "skills_avg_percent" not in header


def test_editor_max_words_check_includes_accepted_variants() -> None:
    test = _b2_test()
    test.sections[1].items[0] = test.sections[1].items[0].model_copy(
        update={"accepted_variants": ["the competiton"]}
    )
    with pytest.raises(ContentValidationError, match="длиннее"):
        validate_test_data(test)


def test_scale_unknown_level_or_skill_raises_value_error() -> None:
    with pytest.raises(ValueError):
        to_scale("A2_KEY", "reading", 1, 2)
    with pytest.raises(ValueError):
        to_scale("B2_FIRST", "speaking", 1, 2)
