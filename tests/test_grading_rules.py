"""Grading rules audit: gap_fill typo tolerance + the B1 Writing Part 2 choose-one.

Golden gap_fill cases are taken from the real keys in `deploy/fixtures/*.json`
(A2 Key 2022, B1 Preliminary 2022, Movers): acceptable misspellings are accepted by
the length-aware edit distance in LISTENING sections, while a different real word
("rain"/"train", "boats"/"boots"), a word form (inflection, comparative, agentive,
irregular past, number word) or a first-letter change is never fuzzy-accepted, and
READING / WRITING gaps (open cloze) need exact spelling. The choose-one cases run the
B1 2022 fixture's Writing Part 2 (q2 / q3, both `optional`) through the real
`GradingService` with the deterministic mock grader, plus the teacher's switch of
the counted task (`apply_override`, admin `set_manual_grade`, both reports).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.content import FilesystemStorage
from app.delivery.projection import project_item
from app.grading import GradingError, GradingService, MockLLMGrader, grade_gap_fill
from app.grading.deterministic import (
    allowed_typos,
    is_inflection_of,
    spelling_mode,
    typo_match,
)
from app.grading.vocabulary import (
    BANK_WORDS_FILE,
    COMMON_WORDS_FILE,
    fixture_bank_words,
    is_known_word,
)
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Answer,
    Attempt,
    AttemptReport,
    ClientGapFillItem,
    GapFillItem,
    GradingResult,
    ManualGrade,
    RosterEntry,
)
from contracts import Test as ExamTest
from contracts.content import Skill

FIXTURES = Path(__file__).resolve().parent.parent / "deploy" / "fixtures"
NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _fixture(name: str) -> ExamTest:
    return ExamTest.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _gap_items() -> dict[str, tuple[GapFillItem, Skill | None]]:
    out: dict[str, tuple[GapFillItem, Skill | None]] = {}
    for name in ("a2-2022.json", "b1-2022.json", "movers-vol2.json"):
        for section in _fixture(name).sections:
            for item in section.items:
                if isinstance(item, GapFillItem):
                    out[item.id] = (item, section.skill)
    return out


GAPS = _gap_items()


def _awarded(item_id: str, response: str, *, spelling: str | None = None) -> float:
    """Grade against a real fixture key, in its real section skill.

    `spelling` forces the item's spelling policy (to test the typo rule itself on a
    reading key, whose default is exact)."""
    item, skill = GAPS[item_id]
    if spelling is not None:
        item = item.model_copy(update={"spelling": spelling})
    return grade_gap_fill(item, response, skill=skill).awarded


def _listening(*accepted: str, **kw: object) -> GapFillItem:
    return GapFillItem(id="x", prompt="p", accepted=list(accepted), **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# gap_fill goldens from the real keys.
# --------------------------------------------------------------------------- #
LISTENING_MISSPELLINGS = [
    # A2 Listening Part 2 (one misspelling tolerated on 5-7 letters, 2 on >= 8).
    ("a2-2022-l2-q6", "Fairfort"),  # Fairford (8 letters): 1 edit
    ("a2-2022-l2-q6", "Fareford"),  # Fairford: 2 edits
    ("a2-2022-l2-q7", "Firday"),  # Friday: transposition = 1 edit
    ("a2-2022-l2-q7", "friday"),  # case
    ("a2-2022-l2-q9", "trian"),  # train (5 letters) — old ratio rule: 80 -> fail
    # B1 Listening Part 3.
    ("b1-2022-l3-q14", "raido"),  # radio
    ("b1-2022-l3-q15", "newpaper"),  # newspaper
    ("b1-2022-l3-q16", "jaket"),  # jacket (6 letters) — old ratio rule: 83 -> fail
    ("b1-2022-l3-q16", "jakets"),  # jackets
    ("b1-2022-l3-q17", "poeple"),  # people
    ("b1-2022-l3-q18", "busness"),  # business, unlisted misspelling
    ("b1-2022-l3-q18", "bussines"),  # listed accepted_variant
    ("b1-2022-l3-q19", "produser"),  # producer
    # Movers Listening (multi-word answers: per-word budget).
    ("movers-l2-q2", "elefant"),
    ("movers-l2-q2", "the elefant"),
    ("movers-l2-q2", "elephnts"),  # elephants (9 letters): 1 edit
    ("movers-l2-q3", "parot"),  # parrot
    ("movers-l2-q4", "friut"),  # fruit
    ("movers-l2-q1", "thirty kinds of animls"),
]


@pytest.mark.parametrize(("item_id", "response"), LISTENING_MISSPELLINGS)
def test_listening_misspellings_are_accepted(item_id: str, response: str) -> None:
    assert GAPS[item_id][1] == "listening"
    assert _awarded(item_id, response) == 1.0


# Genuine misspellings of READING keys: the typo rule recognises them (tolerant),
# but reading is exact by default (Cambridge open cloze / R&W need correct spelling).
READING_MISSPELLINGS = [
    ("movers-rw5-q1", "the seeside"),
    ("movers-rw5-q8", "three beutiful dolphins"),
    ("movers-rw5-q8", "dolphns"),
    ("movers-rw5-q4", "plaied"),
    ("b1-2022-r6-q30", "waht"),  # what (4 letters): exact anyway
]


@pytest.mark.parametrize(("item_id", "response"), READING_MISSPELLINGS)
def test_reading_is_exact_spelling(item_id: str, response: str) -> None:
    assert GAPS[item_id][1] == "reading"
    assert _awarded(item_id, response) == 0.0


@pytest.mark.parametrize(("item_id", "response"), READING_MISSPELLINGS[:-1])
def test_reading_misspellings_pass_the_typo_rule_when_tolerant(
    item_id: str, response: str
) -> None:
    assert _awarded(item_id, response, spelling="tolerant") == 1.0


def test_misspelling_goldens_are_not_known_words() -> None:
    tokens = {
        tok
        for _, resp in LISTENING_MISSPELLINGS + READING_MISSPELLINGS
        for tok in resp.casefold().split()
    }
    keys = {
        w
        for item, _ in GAPS.values()
        for a in (*item.accepted, *item.accepted_variants)
        for w in a.casefold().split()
    }
    misspelt = sorted(t for t in tokens - keys if is_known_word(t))
    assert misspelt == [], misspelt


# A different REAL word one edit away from a real fixture key — never accepted, in
# the item's default mode AND with typo tolerance forced on.
DIFFERENT_WORDS_ON_FIXTURE_KEYS = [
    ("a2-2022-l2-q9", "rain"),  # train
    ("a2-2022-l2-q9", "brain"),
    ("a2-2022-l2-q9", "drain"),
    ("a2-2022-l2-q10", "boats"),  # boots
    ("a2-2022-l2-q10", "roots"),
    ("movers-l2-q3", "carrot"),  # parrot
    ("b1-2022-l3-q16", "packet"),  # jacket
    ("movers-rw5-q2", "trained"),  # rained
    ("movers-rw5-q2", "ruined"),
    ("movers-l2-q1", "thirsty"),  # thirty
    ("movers-l2-q1", "thirteen"),
    ("movers-l2-q2", "elegant"),  # elephant
    ("b1-2022-l3-q14", "ratio"),  # radio
    ("movers-l2-q5", "mild"),  # wild
    ("movers-l2-q5", "wind"),
    ("b1-2022-l3-q19", "produce"),  # producer (agentive -r)
]


@pytest.mark.parametrize(("item_id", "response"), DIFFERENT_WORDS_ON_FIXTURE_KEYS)
def test_a_different_real_word_is_never_a_typo(item_id: str, response: str) -> None:
    assert _awarded(item_id, response) == 0.0
    assert _awarded(item_id, response, spelling="tolerant") == 0.0


# The same, on keys outside the fixtures (a listening item: tolerant by default).
DIFFERENT_WORD_PAIRS = [
    ("coat", "goat"),
    ("horse", "house"),
    ("beach", "bench"),
    ("week", "weak"),
    ("left", "lift"),
    ("women", "woman"),
    ("cloudy", "cloud"),
    ("eleventh", "eleven"),
    ("cheapest", "cheaper"),
    ("carefully", "careful"),
    ("friend", "fiend"),
    ("thirteen", "fourteen"),
    ("policemen", "policeman"),
    ("happier", "happily"),
    ("beautiful", "beauty"),
    ("running", "runner"),
]


@pytest.mark.parametrize(("key", "response"), DIFFERENT_WORD_PAIRS)
def test_different_word_pairs_are_rejected(key: str, response: str) -> None:
    assert typo_match(response, key) is None
    assert grade_gap_fill(_listening(key), response, skill="listening").awarded == 0.0


@pytest.mark.parametrize(
    ("item_id", "response"),
    [
        # Short keys (<= 4 letters) are exact: one edit is another word.
        ("a2-2022-r5-q28", "in"),  # on
        ("a2-2022-r5-q30", "do"),  # to
        ("b1-2022-r6-q32", "were"),  # was
        ("a2-2022-r5-q26", "are"),  # Is
        ("movers-l5-q2", "mapp"),  # map
        # Digits are exact.
        ("a2-2022-l2-q8", "7.40"),
        ("a2-2022-l2-q8", "7:30"),
        # A real function word is the student's choice, not a typo.
        ("b1-2022-r6-q27", "must"),  # most
        ("b1-2022-r6-q29", "form"),  # from (transposition into a real word)
        ("b1-2022-r6-q30", "that"),  # what
        ("a2-2022-r5-q29", "the"),  # they
        ("movers-l2-q2", "a elephant"),  # the elephant
        # Inflection (-s/-es/-ed/-ing/-er) = a grammar error, never fuzzy-accepted.
        ("a2-2022-l2-q9", "trains"),
        ("a2-2022-l2-q10", "boot"),
        ("a2-2022-l2-q7", "fridays"),
        ("b1-2022-l3-q17", "peoples"),
        ("b1-2022-l3-q18", "businesses"),
        ("b1-2022-l3-q19", "produced"),
        ("movers-rw5-q2", "rain"),
        ("movers-rw5-q2", "raining"),
        # Irregular verb forms (tense errors).
        ("movers-rw5-q9", "throw a ball"),
        # A first-letter change is never a typo.
        ("b1-2022-l3-q14", "tadio"),
        ("a2-2022-l2-q7", "triday"),
        # Plainly different words.
        ("a2-2022-l2-q9", "plane"),
        ("b1-2022-l3-q14", "radar"),
    ],
)
def test_different_word_forms_are_rejected(item_id: str, response: str) -> None:
    assert _awarded(item_id, response) == 0.0
    assert _awarded(item_id, response, spelling="tolerant") == 0.0


def test_accepted_variants_feed_the_typo_rule_too() -> None:
    item = _listening("colour", accepted_variants=["color"])
    # "colr" is 2 edits from "colour" (6 letters: budget 1) but 1 from the variant
    # "color" -> accepted via the variant only.
    assert typo_match("colr", "colour") is None
    assert grade_gap_fill(item, "colr", skill="listening").awarded == 1.0


def test_rejected_list_overrides_typo_tolerance() -> None:
    item = _listening("going", rejected=["goes", "gone"])
    assert grade_gap_fill(item, "going").awarded == 1.0
    assert grade_gap_fill(item, "goign").awarded == 1.0  # a typo of the key
    assert grade_gap_fill(item, "Goes").awarded == 0.0  # listed as wrong (normalized)
    assert grade_gap_fill(item, "gone").awarded == 0.0
    # As close to a rejected form as to the key -> the rejected form wins the tie.
    tie = _listening("garden", rejected=["golden"])
    assert typo_match("gorden", "garden") == 1
    assert grade_gap_fill(tie, "gorden").awarded == 0.0


def test_rejected_and_spelling_are_authoring_only() -> None:
    assert "rejected" not in ClientGapFillItem.model_fields
    assert "spelling" not in ClientGapFillItem.model_fields
    item = GapFillItem(id="x", prompt="p", accepted=["a"], rejected=["b"], spelling="exact")
    served = project_item(item, None).model_dump()
    assert "spelling" not in served and "rejected" not in served and "accepted" not in served
    assert GapFillItem(id="x", prompt="p", accepted=["a"]).rejected == []
    assert GapFillItem(id="x", prompt="p", accepted=["a"]).spelling is None


def test_typo_rule_is_length_aware_and_pure() -> None:
    assert [allowed_typos(k) for k in ("to", "was", "most", "train", "friend", "producer")] == [
        0,
        0,
        0,
        1,
        1,
        2,
    ]
    assert allowed_typos("7.30") == 0
    assert typo_match("frend", "friend") == 1
    assert typo_match("frend", "friend") == typo_match("frend", "friend")  # deterministic
    assert typo_match("fiend", "friend") is None  # "fiend" is a real word
    assert typo_match("frnd", "friend") is None  # 2 edits on a 6-letter word
    assert typo_match("waht", "what") is None  # 4 letters: exact
    assert is_inflection_of("go", "goes") and is_inflection_of("like", "liked")
    assert is_inflection_of("producer", "produce") and is_inflection_of("cloudy", "cloud")
    assert is_inflection_of("eleventh", "eleven") and is_inflection_of("women", "woman")
    assert is_inflection_of("cheapest", "cheaper") and is_inflection_of("careful", "carefully")
    assert is_inflection_of("thirty", "thirteen")  # two number words
    assert not is_inflection_of("business", "busines")  # a typo, not -s
    assert not is_inflection_of("friend", "frien")  # -d only after e
    assert not is_inflection_of("running", "runing")  # CVC doubles: a typo
    assert not is_inflection_of("story", "stor")  # unknown base: a typo question


def test_blank_and_whitespace_score_zero() -> None:
    assert _awarded("a2-2022-l2-q9", "") == 0.0
    assert _awarded("a2-2022-l2-q9", "   ") == 0.0
    assert grade_gap_fill(GAPS["a2-2022-l2-q9"][0], None).awarded == 0.0


# --------------------------------------------------------------------------- #
# Spelling policy: per item, else derived from the section skill.
# --------------------------------------------------------------------------- #
def test_spelling_mode_is_derived_from_the_section_skill() -> None:
    item = _listening("jacket")
    assert spelling_mode(item, "reading") == "exact"
    assert spelling_mode(item, "writing") == "exact"
    assert spelling_mode(item, "listening") == "tolerant"
    assert spelling_mode(item, None) == "tolerant"  # no skill: historical behaviour
    assert grade_gap_fill(item, "jaket", skill="listening").awarded == 1.0
    assert grade_gap_fill(item, "jaket", skill="reading").awarded == 0.0
    assert grade_gap_fill(item, "jaket", skill="writing").awarded == 0.0
    assert grade_gap_fill(item, "jacket", skill="reading").awarded == 1.0  # exact


def test_item_spelling_overrides_the_skill() -> None:
    # A listening word spelled out letter by letter must be exact.
    spelled = _listening("Fairford", spelling="exact")
    assert spelling_mode(spelled, "listening") == "exact"
    assert grade_gap_fill(spelled, "Fairfort", skill="listening").awarded == 0.0
    assert grade_gap_fill(spelled, "fairford", skill="listening").awarded == 1.0  # case
    tolerant = _listening("jacket", spelling="tolerant")
    assert grade_gap_fill(tolerant, "jaket", skill="reading").awarded == 1.0


def test_fuzzy_false_is_exact_everywhere() -> None:
    item = _listening("jacket", spelling="tolerant")
    assert grade_gap_fill(item, "jaket", fuzzy=False, skill="listening").awarded == 0.0
    assert grade_gap_fill(item, "Jacket", fuzzy=False, skill="listening").awarded == 1.0


def test_service_threads_the_section_skill(session: Session) -> None:
    result = _grade_b1(
        session,
        {"b1-2022-r6-q30": "waht", "b1-2022-l3-q16": "jaket", "b1-2022-l3-q14": "ratio"},
    )
    by_id = {g.item_id: g.awarded for g in result.items}
    assert by_id["b1-2022-r6-q30"] == 0.0  # reading open cloze: exact
    assert by_id["b1-2022-l3-q16"] == 1.0  # listening: recognisable misspelling
    assert by_id["b1-2022-l3-q14"] == 0.0  # a different real word
    strict = _grade_b1(session, {"b1-2022-l3-q16": "jaket"}, fuzzy=False, attempt_id="a2")
    assert {g.item_id: g.awarded for g in strict.items}["b1-2022-l3-q16"] == 0.0


# --------------------------------------------------------------------------- #
# The bundled vocabulary.
# --------------------------------------------------------------------------- #
def test_common_word_list_is_a_few_thousand_lowercase_words() -> None:
    lines = [
        ln for ln in COMMON_WORDS_FILE.read_text(encoding="utf-8").splitlines()
        if ln and not ln.startswith("#")
    ]
    assert len(lines) >= 3000
    assert lines == sorted(set(lines))
    assert all(ln == ln.casefold() and " " not in ln for ln in lines)


def test_bank_word_list_is_up_to_date_with_the_fixtures() -> None:
    stored = {
        ln for ln in BANK_WORDS_FILE.read_text(encoding="utf-8").splitlines()
        if ln and not ln.startswith("#")
    }
    assert stored == fixture_bank_words(), "run `python -m app.grading.vocabulary`"
    # The deliberate misspellings of a key are never "known words".
    variants = {
        v.casefold() for item, _ in GAPS.values() for v in item.accepted_variants
    }
    assert variants and not (variants & stored)
    assert not any(is_known_word(v) for v in variants)


def test_known_words_cover_regular_forms() -> None:
    for word in ("boats", "rained", "trained", "cheaper", "carefully", "cloudy", "stopped",
                 "running", "beginning", "potatoes", "eleventh", "women", "threw"):
        assert is_known_word(word), word
    # Generated forms never include the classic misspellings.
    for typo in ("stoped", "runing", "begining", "potatos", "truely", "nineth", "writeing"):
        assert not is_known_word(typo), typo


# --------------------------------------------------------------------------- #
# B1 Writing Part 2: exactly one of the optional tasks counts.
# --------------------------------------------------------------------------- #
Q1, Q2, Q3 = "b1-2022-w1-q1", "b1-2022-w2-q2", "b1-2022-w2-q3"


def _grade_b1(
    session: Session, answers: dict[str, str], *, attempt_id: str = "a1", **kw: object
) -> GradingResult:
    content = ContentRepository(session)
    if content.get_test("b1-2022") is None:
        content.add_test(_fixture("b1-2022.json"))
    attempts = AttemptRepository(session)
    attempts.add_attempt(
        Attempt(
            id=attempt_id,
            test_id="b1-2022",
            roster_entry_id=f"r-{attempt_id}",
            status="submitted",
            seed=1,
        )
    )
    for item_id, response in answers.items():
        attempts.save_answer(
            Answer(attempt_id=attempt_id, item_id=item_id, response=response, answered_at=NOW)
        )
    session.commit()
    svc = GradingService(ContentRepository(session), AttemptRepository(session), **kw)  # type: ignore[arg-type]
    return svc.grade(attempt_id)


def _writing(result: GradingResult) -> dict[str, object]:
    return {g.item_id: g for g in result.items if g.item_id in (Q1, Q2, Q3)}


def _objective_max(result: GradingResult) -> float:
    return sum(g.max_points for g in result.items if g.item_id not in (Q1, Q2, Q3))


def test_b1_fixture_models_part2_as_an_optional_pair() -> None:
    items = {i.id: i for s in _fixture("b1-2022.json").sections for i in s.items}
    assert items[Q2].optional and items[Q3].optional and not items[Q1].optional


def test_choose_one_only_one_written_grades_that_one(session: Session) -> None:
    result = _grade_b1(session, {Q3: "word " * 200}, llm_grader=MockLLMGrader())
    w = _writing(result)
    assert Q2 not in w  # the unchosen blank task is not graded at all
    assert w[Q3].awarded == 5.0 and w[Q3].max_points == 5.0  # type: ignore[attr-defined]
    # Q1 (required, blank) + exactly one Part 2 task in the maximum.
    assert result.max_score == pytest.approx(_objective_max(result) + 5.0 + 5.0)


def test_choose_one_both_written_counts_the_higher(session: Session) -> None:
    short, full = "word " * 20, "word " * 200  # mock: 20/100 words -> 1.0; full -> 5.0
    result = _grade_b1(session, {Q1: full, Q2: short, Q3: full}, llm_grader=MockLLMGrader())
    w = _writing(result)
    counted, other = w[Q3], w[Q2]
    assert counted.awarded == 5.0 and counted.max_points == 5.0  # type: ignore[attr-defined]
    assert other.awarded == 0.0 and other.max_points == 0.0  # type: ignore[attr-defined]
    assert other.needs_review is True  # type: ignore[attr-defined]
    assert other.feedback.startswith("не засчитано: выбрано одно задание")  # type: ignore[attr-defined]
    assert "1/5" in other.feedback  # type: ignore[attr-defined]  # its own mark is kept
    assert result.needs_review is True
    # Score counts Q1 + the higher Part 2 task only; the max counts ONE Part 2 task.
    writing_score = sum(g.awarded for g in w.values())  # type: ignore[attr-defined]
    assert writing_score == pytest.approx(10.0)
    assert result.max_score == pytest.approx(_objective_max(result) + 10.0)


def test_choose_one_tie_counts_the_first(session: Session) -> None:
    full = "word " * 200
    result = _grade_b1(session, {Q2: full, Q3: full}, llm_grader=MockLLMGrader())
    w = _writing(result)
    assert w[Q2].max_points == 5.0 and w[Q3].max_points == 0.0  # type: ignore[attr-defined]


def test_choose_one_none_written_counts_one_zero(session: Session) -> None:
    result = _grade_b1(session, {Q1: "word " * 200}, llm_grader=MockLLMGrader())
    w = _writing(result)
    assert Q3 not in w
    assert w[Q2].awarded == 0.0 and w[Q2].max_points == 5.0  # type: ignore[attr-defined]
    assert w[Q2].needs_review is False  # type: ignore[attr-defined]
    # The maximum does not shrink: Q1 + one Part 2 task.
    assert result.max_score == pytest.approx(_objective_max(result) + 10.0)


def test_choose_one_blank_whitespace_counts_as_not_written(session: Session) -> None:
    result = _grade_b1(session, {Q2: "   \n", Q3: "word " * 200}, llm_grader=MockLLMGrader())
    w = _writing(result)
    assert Q2 not in w and w[Q3].max_points == 5.0  # type: ignore[attr-defined]


def test_choose_one_without_grader_keeps_one_in_the_max(session: Session) -> None:
    # No LLM grader: both written tasks go to review; still only one counts.
    result = _grade_b1(session, {Q2: "a b c", Q3: "d e f"})
    w = _writing(result)
    assert w[Q2].max_points == 5.0 and w[Q3].max_points == 0.0  # type: ignore[attr-defined]
    assert w[Q2].needs_review and w[Q3].needs_review  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Choose-one: the teacher switches which task counts (manual override).
# --------------------------------------------------------------------------- #
SHORT, FULL = "word " * 20, "word " * 200  # mock grader: 1.0 / 5.0


def _both_written(session: Session) -> tuple[GradingService, GradingResult]:
    result = _grade_b1(session, {Q1: FULL, Q2: SHORT, Q3: FULL}, llm_grader=MockLLMGrader())
    svc = GradingService(ContentRepository(session), AttemptRepository(session))
    return svc, result


def test_marking_the_uncounted_task_makes_it_count(session: Session) -> None:
    svc, auto = _both_written(session)
    assert _writing(auto)[Q2].max_points == 0.0  # type: ignore[attr-defined]
    swapped = svc.apply_override(auto, Q2, awarded=4)
    w = _writing(swapped)
    assert (w[Q2].awarded, w[Q2].max_points) == (4.0, 5.0)  # type: ignore[attr-defined]
    assert w[Q2].method == "open_writing_manual" and not w[Q2].needs_review  # type: ignore[attr-defined]
    assert (w[Q3].awarded, w[Q3].max_points) == (0.0, 0.0)  # type: ignore[attr-defined]
    assert w[Q3].feedback == "не засчитано: выбрано другое задание (оценка 5/5)"  # type: ignore[attr-defined]
    assert not w[Q3].needs_review  # type: ignore[attr-defined]  # the teacher chose
    # The maximum still counts ONE Part 2 task; the score swaps 5 -> 4.
    assert swapped.max_score == auto.max_score
    assert swapped.score == pytest.approx(auto.score - 5.0 + 4.0)
    assert not swapped.needs_review
    # Pure: the input is untouched.
    assert _writing(auto)[Q2].max_points == 0.0  # type: ignore[attr-defined]


def test_marking_the_counted_task_keeps_it_counted(session: Session) -> None:
    svc, auto = _both_written(session)
    marked = svc.apply_override(auto, Q3, awarded=3)
    w = _writing(marked)
    assert (w[Q3].awarded, w[Q3].max_points) == (3.0, 5.0)  # type: ignore[attr-defined]
    assert (w[Q2].awarded, w[Q2].max_points) == (0.0, 0.0)  # type: ignore[attr-defined]
    assert not w[Q2].needs_review  # type: ignore[attr-defined]  # the teacher chose Q3
    assert marked.score == pytest.approx(auto.score - 5.0 + 3.0)


def test_uncounted_task_mark_is_out_of_the_full_maximum(session: Session) -> None:
    svc, auto = _both_written(session)
    assert _writing(svc.apply_override(auto, Q2, awarded=5))[Q2].awarded == 5.0  # type: ignore[attr-defined]
    with pytest.raises(GradingError):
        svc.apply_override(auto, Q2, awarded=6)


def test_stored_marks_the_latest_counts_in_any_order(session: Session) -> None:
    svc, auto = _both_written(session)
    repo = AttemptRepository(session)
    repo.upsert_manual_grade(ManualGrade(attempt_id="a1", item_id=Q2, awarded=4, graded_at=NOW))
    repo.upsert_manual_grade(
        ManualGrade(attempt_id="a1", item_id=Q3, awarded=2, graded_at=NOW + timedelta(minutes=1))
    )
    session.commit()

    def overlay(order: list[tuple[str, float]]) -> GradingResult:
        out = auto
        for item_id, awarded in order:
            out = svc.apply_override(out, item_id, awarded=awarded)
        return out

    a = overlay([(Q2, 4), (Q3, 2)])
    b = overlay([(Q3, 2), (Q2, 4)])
    assert a == b
    w = _writing(a)
    assert (w[Q3].awarded, w[Q3].max_points) == (2.0, 5.0)  # type: ignore[attr-defined]
    assert (w[Q2].awarded, w[Q2].max_points) == (0.0, 0.0)  # type: ignore[attr-defined]
    assert "оценка 4/5" in w[Q2].feedback  # type: ignore[attr-defined]

    # The teacher re-marks Q2 later -> Q2 counts again.
    repo.upsert_manual_grade(
        ManualGrade(attempt_id="a1", item_id=Q2, awarded=4, graded_at=NOW + timedelta(minutes=2))
    )
    session.commit()
    w = _writing(overlay([(Q2, 4), (Q3, 2)]))
    assert (w[Q2].awarded, w[Q2].max_points) == (4.0, 5.0)  # type: ignore[attr-defined]
    assert (w[Q3].awarded, w[Q3].max_points) == (0.0, 0.0)  # type: ignore[attr-defined]


def test_single_written_task_override_is_unchanged(session: Session) -> None:
    result = _grade_b1(session, {Q3: FULL}, llm_grader=MockLLMGrader())
    svc = GradingService(ContentRepository(session), AttemptRepository(session))
    w = _writing(svc.apply_override(result, Q3, awarded=2))
    assert Q2 not in w and (w[Q3].awarded, w[Q3].max_points) == (2.0, 5.0)  # type: ignore[attr-defined]


def _admin_with_report(session: Session, tmp_path) -> tuple[AdminService, GradingResult]:  # noqa: ANN001
    _, auto = _both_written(session)
    repo = AttemptRepository(session)
    repo.add_roster_entry(RosterEntry(id="r-a1", test_id="b1-2022", display_name="Аня"))
    repo.upsert_report(AttemptReport(attempt_id="a1", graded_at=NOW, grading=auto))
    session.commit()
    return build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=None), auto


def test_admin_marks_the_uncounted_task_and_both_reports_follow(session: Session, tmp_path) -> None:  # noqa: ANN001
    admin, auto = _admin_with_report(session, tmp_path)
    attempt = admin.attempts.get_attempt("a1")
    assert attempt is not None
    # The uncounted task is markable out of 5 (not the stored 0).
    assert admin._item_max_points(attempt, Q2) == 5.0
    assert admin._item_max_points(attempt, Q3) == 5.0
    from app.admin.service import AdminValidationError

    with pytest.raises(AdminValidationError):
        admin.set_manual_grade("a1", Q2, 6)

    detail = admin.set_manual_grade("a1", Q2, 4)
    w = _writing(detail.grading)
    assert (w[Q2].awarded, w[Q2].max_points) == (4.0, 5.0)  # type: ignore[attr-defined]
    assert (w[Q3].awarded, w[Q3].max_points) == (0.0, 0.0)  # type: ignore[attr-defined]
    assert detail.grading.score == pytest.approx(auto.score - 5.0 + 4.0)
    rows = {r.item_id: r for r in detail.responses}
    assert rows[Q2].max_points == 5.0 and rows[Q3].max_points == 0.0

    report = admin.student_report("a1")
    tasks = {t.item_id: t for t in report.writing}
    assert (tasks[Q2].awarded, tasks[Q2].max_points, tasks[Q2].manual) == (4.0, 5.0, True)
    assert tasks[Q3].max_points == 0.0  # rendered "не засчитано", never "0 / 0"
    assert report.score == detail.grading.score and report.max_score == auto.max_score

    # Switching back: a later mark on Q3 makes Q3 count again.
    later = admin.set_manual_grade("a1", Q3, 3)
    w = _writing(later.grading)
    assert (w[Q3].awarded, w[Q3].max_points) == (3.0, 5.0)  # type: ignore[attr-defined]
    assert (w[Q2].awarded, w[Q2].max_points) == (0.0, 0.0)  # type: ignore[attr-defined]


def test_admin_pre_finalize_max_matches_the_report_path(session: Session, tmp_path) -> None:  # noqa: ANN001
    _both_written(session)
    session.commit()
    admin = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=None)
    attempt = admin.attempts.get_attempt("a1")
    assert attempt is not None
    assert admin._item_max_points(attempt, Q2) == admin._item_max_points(attempt, Q3) == 5.0


def test_report_pages_never_print_zero_of_zero() -> None:
    web = Path(__file__).resolve().parent.parent / "apps" / "web"
    report = (web / "teacher-report.html").read_text(encoding="utf-8")
    assert "не засчитано (выбрано другое задание)" in report
    assert "w.max_points === 0" in report
    teacher = (web / "teacher.js").read_text(encoding="utf-8")
    assert "засчитать это задание вместо другого" in teacher
    assert "r.max_points === 0" in teacher
