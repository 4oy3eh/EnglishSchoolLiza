"""Phase 5: grading engine — deterministic + writing + override/review.

Gate (docs/PROMPTS.md Prompt 5): golden grading cases incl. acceptable-misspellings;
grading never reads integrity data; the LLM grader is mocked deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.grading import GradingError, GradingService, MockLLMGrader, normalize
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Answer,
    Attempt,
    GapFillItem,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    PassageTextStimulus,
    PoolOption,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _test() -> ExamTest:
    return ExamTest(
        id="t1",
        title="A2 Key — Mock",
        level="A2_KEY",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id="sec-read",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read this."),
                items=[
                    SingleChoiceItem(
                        id="q-sc",
                        prompt="Pick one",
                        options=[
                            TextOption(key="A", text="alpha"),
                            TextOption(key="B", text="bravo"),
                            TextOption(key="C", text="charlie"),
                        ],
                        correct="C",
                    ),
                    GapFillItem(
                        id="q-gap",
                        prompt="Fill",
                        accepted=["House"],
                        accepted_variants=["haus"],
                    ),
                    GapFillItem(
                        id="q-fuzzy",
                        prompt="Spell",
                        accepted=["beautiful"],
                    ),
                    GapFillItem(
                        id="q-short",
                        prompt="Short",
                        accepted=["to"],
                    ),
                ],
            ),
            Section(
                id="sec-match",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key="A", text="a"), PoolOption(key="B", text="b")]
                ),
                items=[MatchingItem(id="q-match", prompt="Match", correct="B")],
            ),
            Section(
                id="sec-write",
                skill="writing",
                stimulus=PassageTextStimulus(text="Write."),
                items=[
                    OpenWritingItem(
                        id="q-write-llm",
                        prompt="Write 25 words",
                        word_min=25,
                        rubric="secret rubric",
                        grade_mode="llm",
                    ),
                    OpenWritingItem(
                        id="q-write-manual",
                        prompt="Write 25 words",
                        word_min=25,
                        rubric="secret rubric",
                        grade_mode="manual",
                    ),
                ],
            ),
        ],
    )


def _seed(session: Session, answers: dict[str, str]) -> str:
    """Persist the test, an attempt, and the given canonical answers; return attempt id."""
    ContentRepository(session).add_test(_test())
    attempts = AttemptRepository(session)
    attempt = Attempt(
        id="a1", test_id="t1", roster_entry_id="r1", status="submitted", seed=1
    )
    attempts.add_attempt(attempt)
    for item_id, response in answers.items():
        attempts.save_answer(
            Answer(attempt_id="a1", item_id=item_id, response=response, answered_at=NOW)
        )
    session.commit()
    return attempt.id


def _grades(session: Session, answers: dict[str, str], **kwargs: object):
    attempt_id = _seed(session, answers)
    svc = GradingService(ContentRepository(session), AttemptRepository(session), **kwargs)  # type: ignore[arg-type]
    result = svc.grade(attempt_id)
    return svc, result, {g.item_id: g for g in result.items}


# --------------------------------------------------------------------------- #
# Deterministic objective grading.
# --------------------------------------------------------------------------- #
def test_single_choice_and_matching_compare_canonical_keys(session: Session) -> None:
    _, _, g = _grades(
        session,
        {"q-sc": "C", "q-match": "A"},  # sc correct, match wrong
        llm_grader=MockLLMGrader(),
    )
    assert g["q-sc"].awarded == 1.0 and g["q-sc"].method == "single_choice"
    assert g["q-match"].awarded == 0.0 and g["q-match"].method == "matching"


def test_unanswered_item_scores_zero_without_review(session: Session) -> None:
    _, _, g = _grades(session, {}, llm_grader=MockLLMGrader())
    assert g["q-sc"].awarded == 0.0
    assert g["q-sc"].needs_review is False


# --------------------------------------------------------------------------- #
# gap_fill: normalize + accepted + variants + fuzzy.
# --------------------------------------------------------------------------- #
def test_gap_fill_exact_is_case_and_space_insensitive(session: Session) -> None:
    _, _, g = _grades(session, {"q-gap": "  house "}, llm_grader=MockLLMGrader())
    assert g["q-gap"].awarded == 1.0


def test_gap_fill_accepts_authored_variant(session: Session) -> None:
    _, _, g = _grades(session, {"q-gap": "HAUS"}, llm_grader=MockLLMGrader())
    assert g["q-gap"].awarded == 1.0


def test_gap_fill_accepts_unlisted_misspelling_via_fuzzy(session: Session) -> None:
    # "beutiful" is not in accepted/variants but is an acceptable misspelling.
    _, _, g = _grades(session, {"q-fuzzy": "beutiful"}, llm_grader=MockLLMGrader())
    assert g["q-fuzzy"].awarded == 1.0


def test_gap_fill_rejects_wrong_word(session: Session) -> None:
    _, _, g = _grades(session, {"q-fuzzy": "ugly"}, llm_grader=MockLLMGrader())
    assert g["q-fuzzy"].awarded == 0.0


def test_gap_fill_short_answer_skips_fuzzy(session: Session) -> None:
    # "do" is one edit from "to" but too short to fuzzy-accept.
    _, _, g = _grades(session, {"q-short": "do"}, llm_grader=MockLLMGrader())
    assert g["q-short"].awarded == 0.0


def test_fuzzy_threshold_is_configurable(session: Session) -> None:
    # A strict 100 threshold rejects the misspelling that the default accepts.
    _, _, g = _grades(
        session, {"q-fuzzy": "beutiful"}, llm_grader=MockLLMGrader(), fuzzy_threshold=100.0
    )
    assert g["q-fuzzy"].awarded == 0.0


def test_normalize_helper() -> None:
    assert normalize("  Foo   Bar ") == "foo bar"


# --------------------------------------------------------------------------- #
# Writing: LLM (mocked) + manual routing.
# --------------------------------------------------------------------------- #
def test_writing_llm_uses_injected_grader(session: Session) -> None:
    long_answer = " ".join(["word"] * 30)  # >= word_min 25 -> full marks, no review
    _, _, g = _grades(
        session, {"q-write-llm": long_answer}, llm_grader=MockLLMGrader(max_points=5.0)
    )
    assert g["q-write-llm"].method == "open_writing_llm"
    assert g["q-write-llm"].awarded == 5.0
    assert g["q-write-llm"].needs_review is False


def test_writing_llm_flags_short_answer_for_review(session: Session) -> None:
    short = " ".join(["word"] * 10)  # < word_min -> partial + review
    _, _, g = _grades(session, {"q-write-llm": short}, llm_grader=MockLLMGrader())
    assert g["q-write-llm"].needs_review is True
    assert 0.0 < g["q-write-llm"].awarded < 5.0


def test_writing_manual_always_needs_review(session: Session) -> None:
    _, _, g = _grades(
        session, {"q-write-manual": "anything"}, llm_grader=MockLLMGrader()
    )
    assert g["q-write-manual"].method == "open_writing_manual"
    assert g["q-write-manual"].awarded == 0.0
    assert g["q-write-manual"].needs_review is True


def test_writing_llm_without_grader_falls_back_to_review(session: Session) -> None:
    _, _, g = _grades(session, {"q-write-llm": "x"})  # no llm_grader injected
    assert g["q-write-llm"].method == "open_writing_llm"
    assert g["q-write-llm"].needs_review is True
    assert g["q-write-llm"].awarded == 0.0


# --------------------------------------------------------------------------- #
# Score assembly + review queue + manual override.
# --------------------------------------------------------------------------- #
def test_score_assembly_and_review_queue(session: Session) -> None:
    svc, result, g = _grades(
        session,
        {"q-sc": "C", "q-gap": "house", "q-match": "B", "q-write-llm": "word " * 30},
        llm_grader=MockLLMGrader(),
    )
    # 3 objective correct (1 each) + writing 5.0; q-fuzzy/q-short/manual unanswered.
    assert result.score == pytest.approx(8.0)
    # max: 5 objective items * 1 + 2 writing * 5 = 15
    assert result.max_score == pytest.approx(15.0)
    # The manual writing item is always queued.
    queue = {gr.item_id for gr in svc.review_queue(result)}
    assert "q-write-manual" in queue


def test_manual_override_updates_score_and_clears_review(session: Session) -> None:
    svc, result, _ = _grades(session, {}, llm_grader=MockLLMGrader())
    before = result.score
    updated = svc.apply_override(result, "q-write-manual", awarded=4.0)
    new = {g.item_id: g for g in updated.items}
    assert new["q-write-manual"].awarded == 4.0
    assert new["q-write-manual"].method == "open_writing_manual"
    assert new["q-write-manual"].needs_review is False
    assert updated.score == pytest.approx(before + 4.0)
    # Original result is untouched.
    assert result.items[0].item_id == updated.items[0].item_id
    assert "q-write-manual" in {g.item_id for g in svc.review_queue(result)}


def test_override_preserves_objective_method(session: Session) -> None:
    # Overriding a single_choice grade must not relabel it as open_writing_manual.
    svc, result, _ = _grades(session, {"q-sc": "A"}, llm_grader=MockLLMGrader())
    updated = svc.apply_override(result, "q-sc", awarded=1.0)
    new = {g.item_id: g for g in updated.items}["q-sc"]
    assert new.method == "single_choice"
    assert new.awarded == 1.0
    assert new.needs_review is False


def test_override_out_of_range_rejected(session: Session) -> None:
    svc, result, _ = _grades(session, {}, llm_grader=MockLLMGrader())
    with pytest.raises(GradingError):
        svc.apply_override(result, "q-write-manual", awarded=99.0)


def test_override_unknown_item_rejected(session: Session) -> None:
    svc, result, _ = _grades(session, {}, llm_grader=MockLLMGrader())
    with pytest.raises(GradingError):
        svc.apply_override(result, "nope", awarded=1.0)


def test_grade_unknown_attempt_rejected(session: Session) -> None:
    svc = GradingService(ContentRepository(session), AttemptRepository(session))
    with pytest.raises(GradingError):
        svc.grade("missing")


# --------------------------------------------------------------------------- #
# Invariant #2: grading never reaches into integrity / telemetry.
# --------------------------------------------------------------------------- #
def test_grading_engine_does_not_touch_integrity_or_telemetry() -> None:
    grading_dir = Path(__file__).resolve().parent.parent / "app" / "grading"
    for path in grading_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "app.telemetry" not in source, path.name
        assert "app.integrity" not in source, path.name
