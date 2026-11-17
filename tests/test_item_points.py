"""Per-item points (`points` on authoring items): grading, manual marks, totals, editor.

Cambridge YLE Movers Reading & Writing Part 6 sentences are worth 2 marks each, so
an item may carry its own weight (1..20). None keeps the historical defaults
(objective 1, writing / colouring 5) — every existing golden stays unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.admin.service import AdminValidationError
from app.content.editor import ContentValidationError, validate_test_data
from app.delivery.projection import project_item
from app.grading import (
    GradingService,
    MockLLMGrader,
    item_max_points,
    scale_writing_score,
)
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Answer,
    Attempt,
    ColourTaskItem,
    GapFillItem,
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
from tests.test_admin_dashboard import _svc

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def _movers_like(test_id: str = "pts") -> ExamTest:
    """A 3-point single_choice, a 2-point gap_fill, a default matching item, two
    2-point writing sentences (Movers R&W Part 6) and a default colouring task."""
    return ExamTest(
        id=test_id,
        title="Movers-like points",
        level="A1_MOVERS",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id=f"{test_id}-r",
                title="Reading",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[
                    SingleChoiceItem(
                        id=f"{test_id}-sc",
                        prompt="Pick",
                        options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
                        correct="B",
                        points=3,
                    ),
                    GapFillItem(id=f"{test_id}-gap", prompt="Fill", accepted=["cat"], points=2),
                ],
            ),
            Section(
                id=f"{test_id}-m",
                title="Matching",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key="A", text="x"), PoolOption(key="B", text="y")]
                ),
                items=[MatchingItem(id=f"{test_id}-match", prompt="Match", correct="A")],
            ),
            Section(
                id=f"{test_id}-w",
                title="Reading & Writing Part 6",
                skill="writing",
                stimulus=PassageTextStimulus(text="Look at the picture and write."),
                items=[
                    OpenWritingItem(
                        id=f"{test_id}-w{n}",
                        prompt=f"Sentence {n}",
                        word_min=10,
                        rubric="2 = correct sentence, 1 = minor errors, 0 = otherwise",
                        points=2,
                    )
                    for n in (1, 2)
                ],
            ),
            Section(
                id=f"{test_id}-c",
                title="Colour",
                skill="listening",
                stimulus=PassageTextStimulus(text="Listen and colour."),
                items=[ColourTaskItem(id=f"{test_id}-col", prompt="Colour", asset_id="scene.png")],
            ),
        ],
    )


def _seed(session: Session, test: ExamTest, answers: dict[str, str], *, name: str = "S") -> str:
    ContentRepository(session).add_test(test)
    repo = AttemptRepository(session)
    entry = RosterEntry(id=f"{test.id}-{name}", test_id=test.id, display_name=name)
    repo.add_roster_entry(entry)
    attempt_id = f"att-{test.id}-{name}"
    repo.add_attempt(
        Attempt(
            id=attempt_id,
            test_id=test.id,
            roster_entry_id=entry.id,
            status="submitted",
            seed=1,
            started_at=NOW,
            submitted_at=NOW,
            deadline=NOW,
        )
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


def _words(n: int) -> str:
    return " ".join(["word"] * n)


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("awarded", "expected"),
    [(5, 2), (4, 2), (3.5, 1), (3, 1), (2.5, 1), (1.25, 1), (1.2, 0), (1, 0), (0, 0)],
)
def test_scale_writing_score_to_two_marks_rounds_half_up_to_whole(
    awarded: float, expected: float
) -> None:
    assert scale_writing_score(awarded, 5, 2) == expected


def test_scale_writing_score_identity_and_bounds() -> None:
    assert scale_writing_score(3.5, 5, 5) == 3.5  # same scale: the grader's mark verbatim
    assert scale_writing_score(3.5, 5, 10) == 7
    assert scale_writing_score(3.3, 5, 10) == 7  # 6.6 -> 7
    assert scale_writing_score(9, 5, 2) == 2  # clamped
    assert scale_writing_score(-1, 5, 2) == 0
    assert scale_writing_score(3, 0, 2) == 0


def test_item_max_points_defaults_and_overrides() -> None:
    test = _movers_like()
    by_id = {i.id: i for s in test.sections for i in s.items}
    assert item_max_points(by_id["pts-sc"]) == 3
    assert item_max_points(by_id["pts-gap"]) == 2
    assert item_max_points(by_id["pts-match"]) == 1  # None -> objective default
    assert item_max_points(by_id["pts-w1"]) == 2
    assert item_max_points(by_id["pts-col"]) == 5  # None -> colouring default


def test_points_never_reach_the_client() -> None:
    for section in _movers_like().sections:
        for item in section.items:
            assert "points" not in project_item(item, None).model_dump()


def test_points_contract_bounds_have_russian_messages() -> None:
    data = _movers_like().model_dump(mode="json")
    data["sections"][0]["items"][0]["points"] = 0
    with pytest.raises(ContentValidationError, match="баллы: должно быть не меньше 1"):
        validate_test_data(data)
    data["sections"][0]["items"][0]["points"] = 21
    with pytest.raises(ContentValidationError, match="баллы: должно быть не больше 20"):
        validate_test_data(data)
    data["sections"][0]["items"][0]["points"] = 1.5
    with pytest.raises(ContentValidationError, match="баллы: должно быть целым числом"):
        validate_test_data(data)


def test_choose_one_tasks_must_be_worth_the_same() -> None:
    data = _movers_like().model_dump(mode="json")
    w1, w2 = data["sections"][2]["items"]
    w1["optional"] = w2["optional"] = True
    w2["points"] = 3
    with pytest.raises(ContentValidationError, match="одинаковые баллы"):
        validate_test_data(data)
    w2["points"] = 2
    validate_test_data(data)


# --------------------------------------------------------------------------- #
# Grading.
# --------------------------------------------------------------------------- #
def test_grading_uses_item_points(session: Session) -> None:
    test = _movers_like()
    attempt_id = _seed(
        session,
        test,
        {
            "pts-sc": "B",  # right -> 3/3
            "pts-gap": "dog",  # wrong -> 0/2
            "pts-match": "A",  # right, default -> 1/1
            "pts-w1": _words(7),  # mock 3.5/5 -> 1.4 -> 1/2
            "pts-w2": _words(10),  # mock 5/5 -> 2/2
        },
    )
    svc = GradingService(
        ContentRepository(session), AttemptRepository(session), llm_grader=MockLLMGrader()
    )
    result = svc.grade(attempt_id)
    g = {x.item_id: x for x in result.items}
    assert (g["pts-sc"].awarded, g["pts-sc"].max_points) == (3, 3)
    assert (g["pts-gap"].awarded, g["pts-gap"].max_points) == (0, 2)
    assert (g["pts-match"].awarded, g["pts-match"].max_points) == (1, 1)
    assert (g["pts-w1"].awarded, g["pts-w1"].max_points) == (1, 2)
    assert (g["pts-w2"].awarded, g["pts-w2"].max_points) == (2, 2)
    assert g["pts-w1"].needs_review  # the grader's own flag survives the scaling
    assert (g["pts-col"].awarded, g["pts-col"].max_points) == (0, 5)
    assert result.score == 7 and result.max_score == 3 + 2 + 1 + 2 + 2 + 5


def test_gap_fill_right_answer_earns_every_point(session: Session) -> None:
    attempt_id = _seed(session, _movers_like(), {"pts-gap": "Cat"})
    svc = GradingService(ContentRepository(session), AttemptRepository(session))
    g = {x.item_id: x for x in svc.grade(attempt_id).items}
    assert (g["pts-gap"].awarded, g["pts-gap"].max_points) == (2, 2)


def test_manual_writing_and_choose_one_none_written_use_points(session: Session) -> None:
    test = _movers_like()
    for item in test.sections[2].items:
        item.optional = True
    attempt_id = _seed(session, test, {})
    svc = GradingService(ContentRepository(session), AttemptRepository(session))
    g = {x.item_id: x for x in svc.grade(attempt_id).items}
    assert g["pts-w1"].max_points == 2 and g["pts-w1"].awarded == 0  # counts 0 of 2
    assert "pts-w2" not in g

    test2 = _movers_like("pts2")
    attempt2 = _seed(session, test2, {"pts2-w1": _words(10)}, name="T")
    g2 = {x.item_id: x for x in svc.grade(attempt2).items}  # no grader -> manual review
    assert g2["pts2-w1"].max_points == 2 and g2["pts2-w1"].needs_review


# --------------------------------------------------------------------------- #
# Admin: manual mark ranges, CSV + student report totals.
# --------------------------------------------------------------------------- #
def test_manual_mark_range_follows_item_points(session: Session, tmp_path) -> None:
    svc = _svc(session, tmp_path)
    test = _movers_like()
    attempt_id = _seed(session, test, {"pts-sc": "A", "pts-w1": _words(10)})
    attempt = svc.attempts.get_attempt(attempt_id)
    # No report yet: the item definition decides.
    assert svc._item_max_points(attempt, "pts-w1") == 2
    assert svc._item_max_points(attempt, "pts-sc") == 3
    assert svc._item_max_points(attempt, "pts-match") == 1
    assert svc._item_max_points(attempt, "pts-col") == 5
    with pytest.raises(AdminValidationError, match="0–2"):
        svc.set_manual_grade(attempt_id, "pts-w1", 3)
    svc.set_manual_grade(attempt_id, "pts-w1", 2)


def test_csv_and_student_report_totals_use_item_points(session: Session, tmp_path) -> None:
    svc = _svc(session, tmp_path)
    attempt_id = _seed(
        session, _movers_like(), {"pts-sc": "B", "pts-gap": "cat", "pts-w1": _words(10)}
    )
    svc.regrade(attempt_id)
    session.commit()
    report = svc.attempts.get_report(attempt_id)
    assert report is not None and report.grading.max_score == 15
    assert report.grading.score == 5  # 3 + 2, writing waits for the teacher
    # With the report stored, the frozen grade decides the range (writing 0..2).
    assert svc._item_max_points(svc.attempts.get_attempt(attempt_id), "pts-w1") == 2
    svc.set_manual_grade(attempt_id, "pts-w1", 2)
    session.commit()

    student = svc.student_report(attempt_id)
    assert (student.score, student.max_score) == (7, 15)
    writing = {w.item_id: w for w in student.writing}
    assert writing["pts-w1"].max_points == 2 and writing["pts-w1"].awarded == 2

    rows = [line.split(",") for line in svc.results_csv("pts").splitlines()[1:] if line]
    assert rows[0][3:5] == ["7", "15"]


def test_changing_points_on_a_locked_test_is_not_cosmetic() -> None:
    from app.content.editor import ContentConflictError, check_edit_allowed, is_cosmetic_change

    old = _movers_like()
    new = old.model_copy(deep=True)
    new.sections[2].items[0].points = 5  # type: ignore[union-attr]
    assert not is_cosmetic_change(old, new)
    with pytest.raises(ContentConflictError):
        check_edit_allowed(old, new, attempts=0, force=True)


# --------------------------------------------------------------------------- #
# Choose-one marks with mixed points: the uncounted task is out of ITS group.
# --------------------------------------------------------------------------- #
def _mixed_writing(test_id: str = "mix") -> ExamTest:
    """Writing Part 1 worth 10, Part 2 a choose-one pair at the default 5."""
    return ExamTest(
        id=test_id,
        title="Mixed writing points",
        level="B1_PRELIMINARY",
        status="published",
        duration_minutes=45,
        sections=[
            Section(
                id=f"{test_id}-p1",
                title="Writing",  # the same title on purpose: the dashboard must
                skill="writing",  # tell the parts apart by section id
                stimulus=PassageTextStimulus(text="Write an email."),
                items=[
                    OpenWritingItem(
                        id=f"{test_id}-email", prompt="Email", word_min=10, rubric="r", points=10
                    )
                ],
            ),
            Section(
                id=f"{test_id}-p2",
                title="Writing",
                skill="writing",
                stimulus=PassageTextStimulus(text="Choose ONE."),
                items=[
                    OpenWritingItem(
                        id=f"{test_id}-{k}",
                        prompt=f"Task {k}",
                        word_min=10,
                        rubric="r",
                        optional=True,
                    )
                    for k in ("article", "story")
                ],
            ),
        ],
    )


def test_choose_one_uncounted_task_is_marked_out_of_its_own_group(
    session: Session, tmp_path
) -> None:
    svc = _svc(session, tmp_path)
    attempt_id = _seed(
        session,
        _mixed_writing(),
        {"mix-email": _words(30), "mix-article": _words(30), "mix-story": _words(30)},
    )
    svc.regrade(attempt_id)
    session.commit()
    grades = {g.item_id: g for g in svc.attempts.get_report(attempt_id).grading.items}
    assert grades["mix-email"].max_points == 10
    uncounted = next(i for i in ("mix-article", "mix-story") if grades[i].max_points == 0)
    counted = next(i for i in ("mix-article", "mix-story") if i != uncounted)
    assert grades[counted].max_points == 5

    attempt = svc.attempts.get_attempt(attempt_id)
    assert svc._item_max_points(attempt, uncounted) == 5  # its group, not Part 1's 10
    with pytest.raises(AdminValidationError, match="0–5"):
        svc.set_manual_grade(attempt_id, uncounted, 8)
    result = svc.set_manual_grade(attempt_id, uncounted, 4)
    session.commit()
    after = {g.item_id: g for g in result.grading.items}
    # The mark was applied (not silently dropped): the task is now the counted one.
    assert (after[uncounted].awarded, after[uncounted].max_points) == (4, 5)
    assert after[counted].max_points == 0
    assert after["mix-email"].max_points == 10
    # The review rows carry the section id (titles are equal here).
    rows = {r.item_id: r for r in result.responses}
    assert rows["mix-article"].section_id == rows["mix-story"].section_id == "mix-p2"
    assert rows["mix-email"].section_id == "mix-p1"


def test_choose_one_max_without_a_counted_sibling_uses_the_item_definition() -> None:
    from app.admin.service import _choose_one_max
    from contracts import GradingResult, ItemGrade

    test = _mixed_writing()
    test.sections[1].items[0].points = 3  # type: ignore[union-attr]
    grading = GradingResult(
        attempt_id="a",
        items=[
            ItemGrade(item_id="mix-email", awarded=0, max_points=10, method="open_writing_manual"),
            ItemGrade(item_id="mix-article", awarded=0, max_points=0, method="open_writing_llm"),
        ],
        score=0,
        max_score=10,
    )
    groups = [["mix-article", "mix-story"]]
    assert _choose_one_max(grading, "mix-article", groups, test) == 3
    assert _choose_one_max(grading, "mix-email", groups, test) == 10
    assert _choose_one_max(grading, "nope", groups, test) is None


def test_ungraded_review_row_shows_the_item_worth(session: Session, tmp_path) -> None:
    """The unchosen (blank) choose-one task is not graded: its review row shows
    what it is worth from the definition, never a made-up 1."""
    svc = _svc(session, tmp_path)
    test = _mixed_writing("ung")
    for item in test.sections[1].items:
        item.points = 3  # type: ignore[union-attr]
    attempt_id = _seed(session, test, {"ung-email": _words(30), "ung-article": _words(30)})
    svc.regrade(attempt_id)
    session.commit()
    result = svc.attempt_result(attempt_id)
    assert "ung-story" not in {g.item_id for g in result.grading.items}
    rows = {r.item_id: r for r in result.responses}
    assert rows["ung-story"].max_points == 3
    assert rows["ung-article"].max_points == 3


def test_choose_one_equal_points_treats_unset_as_default_five() -> None:
    data = _movers_like().model_dump(mode="json")
    w1, w2 = data["sections"][2]["items"]
    w1["optional"] = w2["optional"] = True
    w1["points"], w2["points"] = None, 5
    validate_test_data(data)  # None == the default 5
    w2["points"] = 4
    with pytest.raises(ContentValidationError, match="одинаковые баллы"):
        validate_test_data(data)
