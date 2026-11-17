"""Results finalize computes the integrity profile + advisory verdict ONCE and stores it.

Gate: finalize persists `AttemptReport.integrity` + `.verdict` next to the grade;
the read API (`ResultsService.get_verdict` / `get_integrity`,
`AttemptRepository.get_verdict`) returns them without recomputing; the analyst
receives the writing tasks + submitted texts + level + the opaque objective % but
never moves a score (golden rule #2); a failing analyst (or a crashing analysis
layer) still yields a COMPLETE grade report — with a deterministic verdict + `error`
when only the analyst failed, with NO verdict when the layer itself crashed; a
regrade recomputes it; the default (no key) is the rule-engine verdict alone.

The rule engine is the verdict of record: an analyst only adds a second opinion on
writing and never changes the suspicion. `ANALYSIS_LLM=off` makes zero analyst
calls at finalize (even with a key); `writing` + a key asks for the opinion;
`writing` without a key behaves like `off`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.analysis import AnalysisContext, AnalysisService, VerdictDraft
from app.feedback import FeedbackService, MockFeedbackAnalyst
from app.grading.llm import MockLLMGrader
from app.grading.service import GradingService
from app.integrity import RULES_VERSION
from app.persistence.repository import AttemptRepository, ContentRepository, EventRepository
from app.results import ResultsService
from contracts import IntegrityEvent, WritingAssessment
from tests.test_results import _seed

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _ev(t: str, secs: float, item: str | None, payload: dict) -> IntegrityEvent:
    ts = T0 + timedelta(seconds=secs)
    return IntegrityEvent(
        attempt_id="att1",
        item_id=item,
        type=t,
        client_ts=ts,
        server_ts=ts,
        payload=payload,  # type: ignore[arg-type]
    )


def _add_writing_telemetry(session: Session) -> None:
    repo = EventRepository(session)
    for e in [
        _ev("field_focus", 0, "w1", {"field": "open_writing", "length": 0}),
        _ev("paste_attempt", 2, "w1", {"kind": "paste", "length": 500}),
        _ev("answer_change", 30, "w1", {"value": "hi i am here today"}),
    ]:
        repo.add_event(e)
    session.commit()


def _service(session: Session, **kw) -> ResultsService:
    content = ContentRepository(session)
    attempts = AttemptRepository(session)
    return ResultsService(
        content=content,
        attempts=attempts,
        grading=GradingService(content, attempts, llm_grader=MockLLMGrader()),
        feedback=FeedbackService(llm=MockFeedbackAnalyst()),
        **kw,
    )


class _SpyAnalyst:
    """Records what it was given; returns a fixed advisory draft."""

    model_id = "spy-analyst"

    def __init__(self) -> None:
        self.contexts: list[AnalysisContext | None] = []

    def analyze(self, profile, segments, context=None):  # noqa: ANN001
        self.contexts.append(context)
        return VerdictDraft(
            suspicion_score=0.4,
            confidence=0.7,
            flags=("large_paste_attempt",),
            summary="Попытка вставки.",
            model_id=self.model_id,
            writing=(
                WritingAssessment(
                    item_id="w1", ai_assist_likelihood=0.5, signals=["large_paste_attempt"]
                ),
            ),
        )


class _BoomAnalyst:
    def analyze(self, profile, segments, context=None):  # noqa: ANN001
        raise RuntimeError("gemini 503")


def test_finalize_persists_deterministic_verdict_by_default(session: Session) -> None:
    # conftest stubs the settings analyst to None -> the rule-engine verdict alone.
    attempt_id = _seed(session)
    _add_writing_telemetry(session)
    report = _service(session).finalize(attempt_id)

    assert report.status == "complete"
    assert report.verdict is not None and report.integrity is not None
    assert report.verdict.model_id is None
    assert [w.item_id for w in report.integrity.writing] == ["w1"]
    assert report.integrity.writing[0].flags == ["large_paste_attempt"]
    assert report.verdict.writing[0].ai_assist_likelihood == 0.3  # one medium rule
    # The rule engine is the verdict of record: the hit, its numbers, its evidence.
    assert report.verdict.rules_version == RULES_VERSION
    [hit] = report.verdict.rules
    assert (hit.code, hit.severity) == ("large_paste_attempt", "medium")
    assert hit.title_ru == "Попытка вставить большой фрагмент текста"
    assert "самая большая — 500 символов" in hit.explanation_ru
    assert hit.evidence.item_ids == ["w1"]
    assert [(e.type, e.item_id) for e in hit.evidence.events] == [("paste_attempt", "w1")]
    assert report.verdict.suspicion_score == 0.3 and report.verdict.flags == [hit.code]

    # Read API for the dashboard: stored, no recompute.
    svc = _service(session)
    assert svc.get_verdict(attempt_id) == report.verdict
    assert svc.get_integrity(attempt_id) == report.integrity
    stored = AttemptRepository(session).get_report(attempt_id)
    assert stored.verdict == report.verdict
    assert AttemptRepository(session).get_verdict(attempt_id) == report.verdict
    assert AttemptRepository(session).get_verdict("nope") is None


def test_analyst_gets_writing_level_and_objective_pct_but_never_moves_score(
    session: Session,
) -> None:
    attempt_id = _seed(session)
    _add_writing_telemetry(session)
    baseline = _service(session, analysis_llm=None).finalize(attempt_id)

    spy = _SpyAnalyst()
    report = _service(session, analysis_llm=spy).finalize(attempt_id)
    ctx = spy.contexts[0]
    assert ctx is not None
    assert ctx.level == "B1_PRELIMINARY"
    assert ctx.objective_pct == 100.0  # r1 gap_fill answered correctly
    assert [(t.item_id, t.text) for t in ctx.writing] == [("w1", "hi i am here today")]
    assert report.verdict.model_id == "spy-analyst"
    # The analyst's 0.4 is NOT the verdict: the overall suspicion, the rules and the
    # summary are identical with and without it — only the writing opinion differs.
    assert report.verdict.suspicion_score == baseline.verdict.suspicion_score == 0.3
    assert report.verdict.rules == baseline.verdict.rules
    assert report.verdict.summary == baseline.verdict.summary != "Попытка вставки."
    assert report.verdict.writing[0].ai_assist_likelihood == 0.5  # the second opinion
    assert baseline.verdict.writing[0].ai_assist_likelihood == 0.3  # the rules' own
    # Golden rule #2: the grade is byte-for-byte the same with or without a verdict.
    assert report.grading.model_dump() == baseline.grading.model_dump()


def test_failing_analyst_still_complete_grade_plus_deterministic_verdict(
    session: Session,
) -> None:
    attempt_id = _seed(session)
    _add_writing_telemetry(session)
    report = _service(session, analysis_llm=_BoomAnalyst()).finalize(attempt_id)

    assert report.status == "complete"  # analysis failure never fails grading
    assert report.error is None
    assert report.grading.score > 0
    assert report.verdict.model_id is None
    assert report.verdict.error == "RuntimeError: gemini 503"
    assert report.verdict.writing[0].signals == ["large_paste_attempt"]
    assert AttemptRepository(session).get_verdict(attempt_id).error == "RuntimeError: gemini 503"


def test_crashing_analysis_layer_still_saves_the_grade(session: Session, monkeypatch) -> None:
    def explode(self, attempt_id, *, context=None):  # noqa: ANN001
        raise ValueError("bad stored event")

    monkeypatch.setattr(AnalysisService, "assess", explode)
    attempt_id = _seed(session)
    report = _service(session).finalize(attempt_id)
    assert report.status == "complete"
    assert report.grading.score > 0
    assert report.integrity is None
    # No made-up "suspicion 0.0" verdict: the dashboard shows "—" (not assessed).
    assert report.verdict is None
    assert AttemptRepository(session).get_verdict(attempt_id) is None


def test_regrade_recomputes_the_verdict(session: Session) -> None:
    attempt_id = _seed(session)
    first = _service(session).finalize(attempt_id)
    # 18 chars, no telemetry yet: below NO_TELEMETRY_MIN_CHARS -> no flag.
    assert first.integrity.writing[0].flags == []
    assert first.integrity.writing[0].paste_attempts == 0
    _add_writing_telemetry(session)  # late telemetry arrives
    second = _service(session).finalize(attempt_id)
    assert second.integrity.writing[0].paste_attempts == 1
    assert AttemptRepository(session).get_verdict(attempt_id) == second.verdict


def test_record_failure_stores_deterministic_verdict(session: Session) -> None:
    attempt_id = _seed(session)
    _add_writing_telemetry(session)
    report = _service(session, analysis_llm=_SpyAnalyst()).record_failure(
        attempt_id, RuntimeError("grader crashed")
    )
    assert report.status == "failed"
    # Crash path: no LLM, the deterministic verdict.
    assert report.verdict is not None and report.verdict.model_id is None
    assert report.integrity.writing[0].paste_attempts == 1


def test_optional_blank_writing_task_is_not_sent_to_the_analyst(session: Session) -> None:
    from contracts import GradingResult, OpenWritingItem, PassageTextStimulus, Section
    from contracts import Test as ExamTest

    test = ExamTest(
        id="t9",
        title="x",
        level="B1_PRELIMINARY",
        status="published",
        duration_minutes=10,
        sections=[
            Section(
                id="s",
                skill="writing",
                stimulus=PassageTextStimulus(text="."),
                items=[
                    OpenWritingItem(id="a", prompt="A", word_min=10, rubric="r", optional=True),
                    OpenWritingItem(id="b", prompt="B", word_min=10, rubric="r", optional=True),
                ],
            )
        ],
    )
    grading = GradingResult(attempt_id="x", items=[], score=0, max_score=0)
    ctx = ResultsService.analysis_context(test, grading, {"b": "my story"})
    assert [t.item_id for t in ctx.writing] == ["b"]
    assert ctx.objective_pct is None


# --------------------------------------------------------------------------- #
# ANALYSIS_LLM: the switch for the optional second opinion.
# --------------------------------------------------------------------------- #
def _settings_analyst(monkeypatch, *, mode: str, key: str):  # noqa: ANN001, ANN202
    """Undo conftest's stub: resolve the analyst from (patched) settings for real."""
    from app.core.config import settings
    from app.results import analyst as analyst_module

    monkeypatch.setattr(settings, "analysis_llm", mode)
    monkeypatch.setattr(settings, "gemini_api_key", key)
    analyst_module._build.cache_clear()
    monkeypatch.setattr(analyst_module, "default_analysis_llm", analyst_module._build)
    return analyst_module


def _count_gemini_posts(monkeypatch) -> list[str]:  # noqa: ANN001
    """Every HTTP call the analysis Gemini client would make (none reaches a network)."""
    posts: list[str] = []

    def post(url, **kw):  # noqa: ANN001, ANN202
        posts.append(url)
        raise RuntimeError("no network in tests")

    monkeypatch.setattr("app.analysis.llm_gemini.httpx.post", post)
    monkeypatch.setattr("app.analysis.llm_gemini._sleep", lambda s: None)
    return posts


def test_analysis_llm_off_makes_zero_analyst_calls_at_finalize(
    session: Session, monkeypatch
) -> None:  # noqa: ANN001
    module = _settings_analyst(monkeypatch, mode="off", key="AIza-test-key")
    posts = _count_gemini_posts(monkeypatch)
    try:
        assert module.default_analysis_llm() is None  # a key is set, the switch wins
        attempt_id = _seed(session)
        _add_writing_telemetry(session)
        report = _service(session).finalize(attempt_id)  # FROM_SETTINGS analyst
        regraded = _service(session).finalize(attempt_id)
    finally:
        module._build.cache_clear()
    assert posts == []  # not one Gemini analysis request
    for r in (report, regraded):
        assert r.status == "complete"
        assert r.verdict.model_id is None and r.verdict.error is None
        assert r.verdict.suspicion_score == 0.3 and r.verdict.rules  # rules still ran


def test_analysis_llm_writing_calls_the_analyst_and_keeps_the_rule_suspicion(
    session: Session, monkeypatch
) -> None:  # noqa: ANN001
    module = _settings_analyst(monkeypatch, mode="writing", key="AIza-test-key")
    posts = _count_gemini_posts(monkeypatch)
    try:
        from app.analysis.llm_gemini import GeminiAnalyst

        assert isinstance(module.default_analysis_llm(), GeminiAnalyst)
        attempt_id = _seed(session)
        _add_writing_telemetry(session)
        report = _service(session).finalize(attempt_id)
    finally:
        module._build.cache_clear()
    assert len(posts) == 1  # the writing second opinion was asked for (and "failed")
    assert report.status == "complete"  # ... which never costs the grade
    assert report.verdict.model_id is None and report.verdict.error.startswith("RuntimeError")
    assert report.verdict.suspicion_score == 0.3  # the rule engine, regardless


def test_analysis_llm_writing_without_a_key_is_off(session: Session, monkeypatch) -> None:  # noqa: ANN001
    module = _settings_analyst(monkeypatch, mode="writing", key="")
    posts = _count_gemini_posts(monkeypatch)
    try:
        assert module.default_analysis_llm() is None
        attempt_id = _seed(session)
        _add_writing_telemetry(session)
        report = _service(session).finalize(attempt_id)
    finally:
        module._build.cache_clear()
    assert posts == [] and report.verdict.model_id is None and report.verdict.error is None


def test_analysis_llm_setting_parses_case_and_rejects_unknown_modes() -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings(_env_file=None).analysis_llm in ("writing", "off")  # env may set it
    assert Settings(_env_file=None, analysis_llm=" OFF ").analysis_llm == "off"
    assert Settings(_env_file=None, analysis_llm="Writing").analysis_llm == "writing"
    assert Settings(_env_file=None, analysis_llm="").analysis_llm == "writing"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, analysis_llm="everything")
