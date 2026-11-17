"""The teacher dashboard reads the STORED advisory verdict (integration step).

The integrity profile + verdict are computed ONCE at finalize and frozen on the
`AttemptReport`; the dashboard only reads them back:

* the results list, the attempt detail, the CSV and the student report never call
  an analyst (a spy that counts calls) — and with a stored profile never even
  re-run the deterministic extractor;
* an attempt without a stored verdict shows no suspicion in the list ("—") and a
  live DETERMINISTIC fallback in the detail (the rule engine, `model_id=None`), with
  the writing features present because the answers + writing ids reach integrity;
  when that live computation fails (malformed stored events) the detail still
  renders and shows "—" with a note instead of answering 500;
* the list row carries the top rule titles behind the suspicion and marks a verdict
  stored before the rule engine as legacy;
* "suspicious first" ranks by the stored score and never touches a grade (rule #2);
* a regrade is the only path that recomputes (and re-stores) the verdict;
* the human timeline renders every writing-telemetry event as a Russian sentence,
  `writing_stats` folded, the raw events complete next to it (rule #6);
* the printable student report carries no integrity data;
* `teacher.js` has a Russian label for every flag AND every rule code the
  integrity/analysis code emits, and every rule has a Russian title.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.admin.models import StudentReport
from app.analysis import AnalysisService, VerdictDraft
from app.content import FilesystemStorage
from app.core.db import get_session
from app.feedback import MockFeedbackAnalyst
from app.grading.llm import MockLLMGrader
from app.grading.service import GradingService
from app.integrity import RULES_VERSION, IntegrityService
from app.integrity.rules import RULE_TITLES, WRITING_RULE_SEVERITY, combine
from app.persistence.repository import AttemptRepository, EventRepository
from apps.api import admin as admin_api
from apps.api.main import app
from contracts import (
    AnalysisVerdict,
    IntegrityEvent,
    IntegrityProfile,
    RuleHit,
    WritingAssessment,
    WritingFeatures,
)
from tests.test_admin_dashboard import T0, _report, _seed, _test

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "verdict-teacher-pw"
ESSAY = "Hi Pat, let's meet at the bus stop at three. Bring some food please. See you soon!"


class _SpyAnalyst:
    """Counts every call. A dashboard READ must leave `calls` at 0.

    (`AnalysisService` swallows an analyst exception and degrades to the formula,
    so raising here would be invisible — the tests assert on the counter.)
    """

    model_id = "spy-analyst"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, profile, segments, context=None):  # noqa: ANN001, ANN201
        self.calls += 1
        return VerdictDraft(
            suspicion_score=0.42,
            confidence=0.8,
            flags=("spy_flag",),
            summary="Вердикт шпиона.",
            model_id=self.model_id,
        )


def _svc(session: Session, tmp_path, spy: _SpyAnalyst) -> AdminService:
    return build_admin_service(
        session,
        FilesystemStorage(tmp_path),
        analysis_llm=spy,
        llm_grader=MockLLMGrader(),
        feedback_llm=MockFeedbackAnalyst(),
    )


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _SpyAnalyst:
    """The only analyst anywhere: injected AND what the settings would resolve to."""
    analyst = _SpyAnalyst()
    monkeypatch.setattr("app.results.analyst.default_analysis_llm", lambda: analyst)
    return analyst


def _verdict(
    attempt_id: str,
    suspicion: float,
    *,
    model_id: str | None = "gemini-test-model",
    ai: float | None = 0.8,
) -> AnalysisVerdict:
    return AnalysisVerdict(
        attempt_id=attempt_id,
        suspicion_score=suspicion,
        confidence=0.7,
        flags=["large_paste_attempt", "long_hidden"],
        summary="СЕКРЕТНЫЙ-ВЕРДИКТ: попытка вставки большого текста.",
        model_id=model_id,
        writing=(
            []
            if ai is None
            else [
                WritingAssessment(
                    item_id="t1-w1",
                    ai_assist_likelihood=ai,
                    signals=["large_paste_attempt"],
                    rationale="ОБОСНОВАНИЕ: текст появился без набора.",
                )
            ]
        ),
    )


def _profile(attempt_id: str) -> IntegrityProfile:
    return IntegrityProfile(
        attempt_id=attempt_id,
        total_hidden_ms=45_000,
        paste_attempt_count=1,
        writing=[
            WritingFeatures(
                item_id="t1-w1",
                has_keystroke_telemetry=True,
                final_length=len(ESSAY),
                typed_chars=20,
                non_keyboard_chars=62,
                paste_attempts=1,
                max_paste_attempt_length=300,
                flags=["large_paste_attempt"],
            )
        ],
    )


def _store(
    session: Session,
    attempt_id: str,
    verdict: AnalysisVerdict | None,
    *,
    integrity: IntegrityProfile | None = None,
) -> None:
    """A frozen report (as finalize writes it) carrying `verdict` / `integrity`."""
    _report(session, attempt_id, q1=1, q2=0, w1=3)
    repo = AttemptRepository(session)
    report = repo.get_report(attempt_id)
    assert report is not None
    repo.upsert_report(report.model_copy(update={"verdict": verdict, "integrity": integrity}))
    session.commit()


def _event(attempt_id: str, secs: float, kind: str, item: str | None, payload: dict | None = None):
    ts = T0 + timedelta(seconds=secs)
    return IntegrityEvent(
        attempt_id=attempt_id,
        item_id=item,
        type=kind,  # type: ignore[arg-type]
        client_ts=ts,
        server_ts=ts,
        payload=payload or {},
    )


def _boom(what: str):
    def _raise(*_a, **_k):
        raise AssertionError(what)

    return _raise


# --------------------------------------------------------------------------- #
# Stored verdict: read back, never recomputed, never an analyst call.
# --------------------------------------------------------------------------- #
def test_list_and_detail_read_the_stored_verdict_without_an_analyst_call(
    session: Session, tmp_path, spy: _SpyAnalyst, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Соня", answers={"t1-q1": "A", "t1-q2": "B", "t1-w1": ESSAY})
    stored, profile = _verdict(aid, 0.77), _profile(aid)
    _store(session, aid, stored, integrity=profile)

    # A stored verdict + profile means NOTHING is recomputed on a read: no analysis,
    # no integrity extraction, no grading pass.
    monkeypatch.setattr(AnalysisService, "assess", _boom("read recomputed the verdict"))
    monkeypatch.setattr(IntegrityService, "profile", _boom("read re-extracted the profile"))
    monkeypatch.setattr(GradingService, "grade", _boom("read graded the attempt"))

    [row] = svc.results_for_test("t1")
    assert (row.suspicion_score, row.confidence) == (0.77, 0.7)
    assert row.ai_assist_max == 0.8 and row.verdict_model_id == "gemini-test-model"

    detail = svc.attempt_result(aid)
    assert detail.verdict == stored and detail.profile == profile
    assert detail.verdict_source == "stored"
    # Rule #6: the raw replay + the human timeline travel next to the verdict.
    assert detail.event_count == len(detail.events) == 1 and len(detail.timeline) == 1
    # Rule #2: the grade is the frozen report's, whatever the verdict says.
    assert (detail.grading.score, detail.grading.max_score) == (4, 7)

    svc.results_csv("t1")
    svc.student_report(aid)
    assert spy.calls == 0


def test_read_path_analysis_service_can_never_carry_an_analyst(
    session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    svc = _svc(session, tmp_path, spy)
    # The injected analyst went to the regrade `ResultsService` only.
    assert svc.analysis.llm is None
    events = EventRepository(session)
    integrity = IntegrityService(events)
    with pytest.raises(ValueError, match="no LLM analyst"):
        AdminService(
            content=svc.content,
            roster=svc.roster,
            grading=svc.grading,
            integrity=integrity,
            analysis=AnalysisService(events, integrity, llm=spy),
            results=svc.results,
            attempts=svc.attempts,
            events=events,
        )


def test_regrade_is_the_only_path_that_recomputes_the_verdict(
    session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Ann", answers={"t1-q1": "A", "t1-w1": ESSAY})

    detail = svc.regrade(aid)  # the explicit teacher action: finalize + analyst, once
    assert spy.calls == 1
    assert detail.verdict_source == "stored" and detail.verdict.model_id == "spy-analyst"
    # The analyst is a second opinion on writing: its 0.42 / summary are NOT the
    # verdict — the stored suspicion is the rule engine's (nothing fired here).
    stored = detail.verdict
    assert stored.rules_version == RULES_VERSION
    assert stored.suspicion_score == combine(h.severity for h in stored.rules) != 0.42
    assert stored.summary != "Вердикт шпиона."
    score = detail.grading.score

    for _ in range(3):  # a live-polling dashboard
        [row] = svc.results_for_test("t1")
        assert (row.suspicion_score, row.verdict_model_id) == (
            stored.suspicion_score,
            "spy-analyst",
        )
        assert not row.verdict_legacy
        assert svc.attempt_result(aid).verdict == stored
    assert spy.calls == 1

    assert svc.regrade(aid).grading.score == score  # recomputed; the grade is unmoved
    assert spy.calls == 2


# --------------------------------------------------------------------------- #
# No stored verdict: "—" in the list, the deterministic formula in the detail.
# --------------------------------------------------------------------------- #
def test_no_stored_verdict_list_is_empty_and_detail_falls_back_to_the_formula(
    session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    answers = {"t1-q1": "A", "t1-q2": "A", "t1-w1": ESSAY}
    bare = _seed(session, name="Bare", answers=answers, events=0)  # never finalized
    old = _seed(session, name="Old", answers=answers, events=0)  # pre-migration report
    _store(session, old, None)
    repo = EventRepository(session)
    for aid in (bare, old):
        repo.add_event(_event(aid, 5, "field_focus", "t1-w1", {"field": "open_writing"}))
        repo.add_event(_event(aid, 9, "paste_attempt", "t1-w1", {"kind": "paste", "length": 300}))
    session.commit()

    rows = {o.attempt_id: o for o in svc.results_for_test("t1")}
    for aid in (bare, old):
        row = rows[aid]
        assert row.suspicion_score is None and row.confidence is None
        assert row.ai_assist_max is None and row.verdict_model_id is None

    for aid in (bare, old):
        detail = svc.attempt_result(aid)
        assert detail.verdict_source == "live_fallback"
        v = detail.verdict
        assert v.model_id is None and v.error is None  # the rules, not a failed LLM
        assert v.rules_version == RULES_VERSION
        # A blocked 300-char paste, and a submitted text no input event explains.
        signals = ["large_paste_attempt", "no_input_telemetry"]
        assert v.flags == signals == [h.code for h in v.rules]
        assert [h.severity for h in v.rules] == ["medium", "weak"]
        assert all(h.evidence.item_ids == ["t1-w1"] for h in v.rules)
        assert "самая большая — 300 символов" in v.rules[0].explanation_ru
        [assessment] = v.writing
        assert assessment.item_id == "t1-w1" and assessment.signals == signals
        expected = combine(WRITING_RULE_SEVERITY[s] for s in signals)
        assert expected == pytest.approx(0.37)  # 1 - (1 - 0.30)(1 - 0.10)
        assert assessment.ai_assist_likelihood == expected
        assert v.suspicion_score == expected  # no attempt-level rule fired
        assert re.search("[а-яё]", v.summary) and re.search("[а-яё]", assessment.rationale)
        # The answers + writing ids reached integrity: the features describe the
        # SUBMITTED text although no answer_change snapshot exists in the stream.
        [features] = detail.profile.writing
        assert features.item_id == "t1-w1" and features.final_length == len(ESSAY)
        assert (features.paste_attempts, features.max_paste_attempt_length) == (1, 300)
        assert features.word_count > 10
        # Rule #2: a cheating signal never moves the grade (both objective items right).
        assert detail.grading.score >= 2

    # A read computes for display only — nothing is persisted, nobody calls an LLM.
    assert svc.attempts.get_report(bare) is None
    assert svc.attempts.get_verdict(old) is None
    assert spy.calls == 0


def test_crash_path_report_without_profile_gets_writing_features_for_display(
    session: Session, tmp_path, spy: _SpyAnalyst, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Crash", answers={"t1-q1": "A", "t1-w1": ESSAY}, events=0)
    stored = _verdict(aid, 0.0, model_id=None, ai=None)
    _store(session, aid, stored, integrity=None)  # verdict stored, its profile lost

    monkeypatch.setattr(AnalysisService, "assess", _boom("stored verdict was recomputed"))
    detail = svc.attempt_result(aid)
    assert detail.verdict == stored and detail.verdict_source == "stored"
    [features] = detail.profile.writing  # extracted with answers= / writing_items=
    assert features.item_id == "t1-w1" and features.final_length == len(ESSAY)
    assert spy.calls == 0


# --------------------------------------------------------------------------- #
# Ranking + CSV use the stored suspicion; neither touches a score (rule #2).
# --------------------------------------------------------------------------- #
def _ranking_fixture(session: Session, svc: AdminService) -> dict[str, str]:
    svc.content.create_test(_test())
    session.commit()
    ids = {name: _seed(session, name=name, events=0) for name in ("None", "Low", "High", "Tie")}
    # "Low" LOOKS worst live (two minutes off the tab) — a recomputing list would
    # rank it first. The stored numbers decide instead.
    repo = EventRepository(session)
    repo.add_event(_event(ids["Low"], 10, "visibility_hidden", None))
    repo.add_event(_event(ids["Low"], 130, "visibility_visible", None))
    _store(session, ids["Low"], _verdict(ids["Low"], 0.2, model_id=None, ai=0.1))
    _store(session, ids["High"], _verdict(ids["High"], 0.9))
    _store(session, ids["Tie"], _verdict(ids["Tie"], 0.9, ai=None))
    return ids


def test_ranking_uses_the_stored_suspicion_and_never_moves_a_score(
    session: Session, tmp_path, spy: _SpyAnalyst, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(session, tmp_path, spy)
    ids = _ranking_fixture(session, svc)
    monkeypatch.setattr(AnalysisService, "assess", _boom("the list recomputed a verdict"))
    monkeypatch.setattr(IntegrityService, "profile", _boom("the list extracted a profile"))

    ranked = svc.results_for_test("t1")
    # Highest stored suspicion first; ties keep the roster order; no verdict last.
    assert [o.display_name for o in ranked] == ["High", "Tie", "Low", "None"]
    assert [o.suspicion_score for o in ranked] == [0.9, 0.9, 0.2, None]
    assert [o.ai_assist_max for o in ranked] == [0.8, None, 0.1, None]
    assert [o.verdict_model_id for o in ranked] == ["gemini-test-model"] * 2 + [None, None]
    # Same answers + same frozen grade => same score, whatever the suspicion.
    by_name = {o.display_name: o for o in ranked}
    assert {by_name[n].score for n in ("High", "Tie", "Low")} == {4.0}
    assert spy.calls == 0 and set(ids) == set(by_name)


def test_csv_suspicion_column_is_the_stored_value(
    session: Session, tmp_path, spy: _SpyAnalyst, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(session, tmp_path, spy)
    _ranking_fixture(session, svc)
    monkeypatch.setattr(AnalysisService, "assess", _boom("the CSV recomputed a verdict"))
    monkeypatch.setattr(IntegrityService, "profile", _boom("the CSV extracted a profile"))
    monkeypatch.setattr(GradingService, "grade", _boom("the CSV graded an attempt"))

    lines = [line.split(",") for line in svc.results_csv("t1").split("\r\n") if line]
    column = lines[0].index("suspicion_score")
    by_name = {line[0]: line for line in lines[1:]}
    assert by_name["High"][column] == "0.90" and by_name["Tie"][column] == "0.90"
    assert by_name["Low"][column] == "0.20"  # the stored 0.2, not the live "2 min away"
    assert by_name["None"][column] == ""  # no stored verdict -> an empty cell
    # The score columns are the frozen grade — identical across suspicion levels.
    score = lines[0].index("score")
    assert {by_name[n][score] for n in ("High", "Tie", "Low")} == {"4"}
    assert spy.calls == 0


# --------------------------------------------------------------------------- #
# HTTP surface: the JSON the dashboard renders.
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(
    session: Session, tmp_path, spy: _SpyAnalyst, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    # The token secret stays on its dev default here, which the admin API tolerates
    # only in an explicit dev/test environment for a request from this machine.
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()
    app.dependency_overrides[get_session] = lambda: session

    def _dep() -> Iterator[AdminService]:
        yield _svc(session, tmp_path, spy)

    app.dependency_overrides[admin_api.get_admin_service] = _dep
    try:
        yield TestClient(app, client=("127.0.0.1", 50000))  # a loopback teacher
    finally:
        app.dependency_overrides.clear()
        admin_api.login_limiter.reset()


def _headers(client: TestClient) -> dict[str, str]:
    token = client.post("/admin/login", json={"password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_results_endpoints_expose_the_stored_verdict(
    client: TestClient, session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    answers = {"t1-q1": "A", "t1-w1": ESSAY}
    stored = _seed(session, name="Stored", answers=answers)
    _store(session, stored, _verdict(stored, 0.66), integrity=_profile(stored))
    bare = _seed(session, name="Bare", answers=answers)

    rows = client.get("/admin/tests/t1/results", headers=headers).json()
    assert [r["display_name"] for r in rows] == ["Stored", "Bare"]
    assert rows[0]["suspicion_score"] == 0.66 and rows[0]["ai_assist_max"] == 0.8
    assert rows[0]["verdict_model_id"] == "gemini-test-model"
    assert rows[1]["suspicion_score"] is None and rows[1]["confidence"] is None
    assert rows[1]["ai_assist_max"] is None

    d = client.get(f"/admin/results/{stored}", headers=headers).json()
    assert d["verdict_source"] == "stored"
    assert d["verdict"]["summary"].startswith("СЕКРЕТНЫЙ-ВЕРДИКТ")
    assert d["verdict"]["writing"][0]["signals"] == ["large_paste_attempt"]
    assert d["profile"]["writing"][0]["max_paste_attempt_length"] == 300
    assert d["events"] and d["timeline"]  # rule #6: the replay next to the verdict

    d = client.get(f"/admin/results/{bare}", headers=headers).json()
    assert d["verdict_source"] == "live_fallback" and d["verdict"]["model_id"] is None
    assert spy.calls == 0


def test_student_report_carries_no_integrity_fields_even_with_a_stored_verdict(
    client: TestClient, session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Соня", answers={"t1-q1": "A", "t1-q2": "B", "t1-w1": ESSAY})
    _store(session, aid, _verdict(aid, 0.91), integrity=_profile(aid))

    resp = client.get(f"/admin/results/{aid}/report", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == set(StudentReport.model_fields)
    assert (body["score"], body["max_score"]) == (4, 7)  # the grade, untouched
    blob = json.dumps(body, ensure_ascii=False).lower()
    for word in (
        "suspicion", "verdict", "integrity", "confidence", "ai_assist", "signals",
        "flags", "paste", "typed_chars", "profile", "events", "timeline", "device",
        "секретный-вердикт", "обоснование", "gemini-test-model", "0.91",
    ):  # fmt: skip
        assert word not in blob, word
    # The print page itself renders nothing from the integrity side either.
    page = TestClient(app).get("/web/teacher-report.html").text
    for word in ("suspicion", "verdict", "ai_assist", "profile", "timeline", "flags"):
        assert word not in page, word
    assert spy.calls == 0


# --------------------------------------------------------------------------- #
# Timeline: a Russian sentence for every writing-telemetry event.
# --------------------------------------------------------------------------- #
def test_timeline_sentences_for_the_writing_telemetry_events(
    session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Writer", answers={"t1-q1": "A", "t1-w1": ESSAY}, events=0)
    w, ow = "t1-w1", {"field": "open_writing"}
    stream = [
        (10, "field_focus", w, {**ow, "length": 0}),
        # Two stats flushes + a swiped word of ONE focus session -> one folded line.
        (20, "writing_stats", w, {
            **ow, "added": {"insertText": 30}, "deleted_chars": 2, "keystrokes": 34,
            "active_ms": 12_000,
        }),
        (25, "input_burst", w, {**ow, "input_type": "insertText", "length": 5}),
        (30, "writing_stats", w, {
            **ow, "added": {"insertText": 11, "insertFromPaste": 40}, "deleted_chars": 1,
            "keystrokes": 12, "untrusted_inputs": 1, "active_ms": 3_000,
        }),
        (40, "field_blur", w, {**ow, "length": 81}),
        (50, "paste_attempt", w, {"kind": "paste", "length": 300, **ow}),
        (55, "paste_attempt", "t1-q1", {"kind": "drop", "length": 10}),
        (60, "fullscreen_exit", w, {}),
        (70, "input_burst", w, {**ow, "input_type": "insertFromPaste", "length": 120}),
        (75, "input_burst", w, {**ow, "input_type": "insertText", "length": 6, "trusted": False}),
        (80, "input_burst", w, {**ow, "input_type": "historyUndo", "length": 22}),
        (85, "input_burst", w, {**ow, "input_type": "insertText", "length": 40}),
        (90, "input_burst", w, {**ow, "input_type": "", "length": 15}),
        (95, "writing_stats", w, {
            **ow, "added": {"insertText": 3}, "keystrokes": 3, "active_ms": 500,
        }),
        (100, "fullscreen_exit", None, {}),
    ]  # fmt: skip
    repo = EventRepository(session)
    for secs, kind, item, payload in stream:
        repo.add_event(_event(aid, secs, kind, item, payload))
    session.commit()

    detail = svc.attempt_result(aid)
    got = [(e.type, e.label, e.integrity) for e in detail.timeline]
    assert got == [
        ("field_focus", "№3: перешёл в поле ответа (поле пустое)", False),
        (
            "writing_stats",
            "№3: набор текста — +41 символ с клавиатуры, удалено 3, 46 нажатий, "
            "≈164 симв./мин; ⚠︎ без набора +40 символов (вставка из буфера); "
            "⚠︎ сгенерировано скриптом: 1 · сводка 2 замеров · коротких групповых "
            "вводов (≤12 симв., автозамена/свайп): 1",
            True,
        ),
        ("field_blur", "№3: вышел из поля ответа (в поле 81 символ)", False),
        (
            "paste_attempt",
            "⚠︎ Попытка вставить текст (300 символов) в задание 3 — заблокирована",
            True,
        ),
        (
            "paste_attempt",
            "Попытка перетащить текст (10 символов) в задание 1 — заблокирована",
            True,
        ),
        ("fullscreen_exit", "⚠︎ Вышел из полноэкранного режима (задание 3)", True),
        ("input_burst", "⚠︎ №3: вставка без набора: +120 символов (вставка из буфера)", True),
        ("input_burst", "⚠︎ №3: ввод сгенерирован скриптом: +6 символов", True),
        ("input_burst", "№3: отмена/повтор правки: +22 символа", False),
        (
            "input_burst",
            "⚠︎ №3: крупный ввод одним событием: +40 символов (ввод текста) — "
            "не по одной букве",
            True,
        ),
        (
            "input_burst",
            "⚠︎ №3: вставка без набора: +15 символов (без типа — программный ввод)",
            True,
        ),
        (
            "writing_stats",
            "№3: набор текста — +3 символа с клавиатуры, удалено 0, 3 нажатия",
            False,
        ),
        ("fullscreen_exit", "⚠︎ Вышел из полноэкранного режима", True),
    ]
    # No event type leaks through as a raw code, and the timeline is chronological.
    assert all(e.label != e.type for e in detail.timeline)
    assert [e.at for e in detail.timeline] == sorted(e.at for e in detail.timeline)
    # Folding is presentation only: the raw replay next to it is complete (rule #6).
    assert detail.event_count == len(detail.events) == len(stream)
    assert sum(e.type == "writing_stats" for e in detail.events) == 3
    assert spy.calls == 0


def test_timeline_fold_is_per_focus_session(session: Session, tmp_path, spy: _SpyAnalyst) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Two", answers={"t1-q1": "A", "t1-w1": ESSAY}, events=0)
    w, ow = "t1-w1", {"field": "open_writing"}
    repo = EventRepository(session)
    stats = {**ow, "added": {"insertText": 5}, "keystrokes": 5}
    for secs, kind, payload in [
        (1, "field_focus", {**ow, "length": 0}),
        (2, "writing_stats", stats),
        (3, "field_blur", {**ow, "length": 5}),
        (4, "field_focus", {**ow, "length": 5}),
        (5, "input_burst", {**ow, "input_type": "insertCompositionText", "length": 7}),
        (6, "field_blur", {**ow, "length": 12}),
    ]:
        repo.add_event(_event(aid, secs, kind, w, payload))
    session.commit()

    labels = [e.label for e in svc.attempt_result(aid).timeline]
    assert labels == [
        "№3: перешёл в поле ответа (поле пустое)",
        "№3: набор текста — +5 символов с клавиатуры, удалено 0, 5 нажатий",
        "№3: вышел из поля ответа (в поле 5 символов)",
        "№3: перешёл в поле ответа (в поле уже 5 символов)",
        # A lone swiped/IME word with no stats flush yet still reads as typing.
        "№3: набор текста · коротких групповых вводов (≤12 симв., автозамена/свайп): 1",
        "№3: вышел из поля ответа (в поле 12 символов)",
    ]


# --------------------------------------------------------------------------- #
# The dashboard's Russian flag table covers everything the code can emit.
# --------------------------------------------------------------------------- #
def _js_flag_labels() -> dict[str, str]:
    js = (REPO_ROOT / "apps" / "web" / "teacher.js").read_text(encoding="utf-8")
    block = re.search(r"const FLAG_LABELS = \{(.*?)\n\};", js, re.S)
    assert block, "FLAG_LABELS table not found in teacher.js"
    return dict(re.findall(r'^\s*([a-z_]+):\s*"([^"]+)",?\s*$', block.group(1), re.M))


def test_dashboard_translates_every_flag_the_backend_can_emit() -> None:
    def source(*parts: str) -> str:
        return (REPO_ROOT.joinpath("app", *parts)).read_text(encoding="utf-8")

    emitted = set(RULE_TITLES)  # every code the rule engine can put on a card
    writing_flags = set(
        re.findall(r'flags\.append\("([a-z_]+)"\)', source("integrity", "writing.py"))
    )
    emitted |= writing_flags
    emitted |= set(re.findall(r'kind="([a-z_]+)"', source("analysis", "segments.py")))
    # Every rule code used in rules.py is declared in RULE_TITLES (no code can be
    # emitted without a title): `_hit("<code>", ...)` calls + the writing flags.
    rules_src = source("integrity", "rules.py")
    used = set(re.findall(r'_hit\(\s*"([a-z_]+)"', rules_src)) | set(WRITING_RULE_SEVERITY)
    assert used == set(RULE_TITLES), sorted(used ^ set(RULE_TITLES))
    # Every writing flag the extractor can raise is a rule (it has a severity).
    assert writing_flags == set(WRITING_RULE_SEVERITY)
    for code, title in RULE_TITLES.items():
        assert re.search("[а-яё]", title), f"{code} title is not Russian"
    # The second opinion's own signals.
    gemini_src = source("analysis", "llm_gemini.py")
    emitted |= set(re.findall(r'^[A-Z_]+_SIGNAL = "([a-z_]+)"$', gemini_src, re.M))
    # Sanity: the scan really found every family.
    assert {
        "large_paste_attempt",
        "long_hidden",
        "writing_flagged",
        "device_changed",
        "systematic_tab_switching",
        "long_time_away",
        "frequent_fullscreen_exits",
        "possible_screenshot",
        "fast_answer_after_return",
        "level_mismatch",
        "instruction_injection_attempt",
    } <= emitted

    labels = _js_flag_labels()
    assert emitted <= set(labels), f"no Russian label for: {sorted(emitted - set(labels))}"
    for code in emitted:
        assert re.search("[а-яё]", labels[code]), f"{code} label is not Russian"
    # Codes the analysis / integrity engines are about to emit are pre-labelled.
    for code in ("instruction_injection_attempt", "telemetry_gap"):
        assert re.search("[а-яё]", labels.get(code, "")), f"{code} has no Russian label"
    # Unknown codes (Gemini's own snake_case extras) fall back to the raw code —
    # own keys only, so a model-supplied `constructor` is not a prototype lookup.
    js = (REPO_ROOT / "apps" / "web" / "teacher.js").read_text(encoding="utf-8")
    assert (
        "const flagLabel = (f) => (Object.hasOwn(FLAG_LABELS, f) ? FLAG_LABELS[f] : f);"
        in js
    )


# --------------------------------------------------------------------------- #
# The live fallback never answers 500 (malformed stored events).
# --------------------------------------------------------------------------- #
def test_malformed_device_event_no_longer_breaks_the_live_fallback(
    client: TestClient, session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    """`device_info` payloads are untyped on ingest: a string where an object is
    expected used to crash the extractor (and the device list) -> HTTP 500."""
    headers = _headers(client)
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Broken", answers={"t1-q1": "A", "t1-w1": ESSAY}, events=0)
    repo = EventRepository(session)
    malformed = {"screen": "1920x1080", "user_agent": ["x"]}
    repo.add_event(_event(aid, 1, "device_info", None, malformed))
    repo.add_event(_event(aid, 2, "device_info", None, {"screen": {"width": 1, "height": 2}}))
    session.commit()

    resp = client.get(f"/admin/results/{aid}", headers=headers)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["verdict_source"] == "live_fallback" and d["verdict"] is not None
    assert d["verdict_note"] is None
    assert [dev["screen"] for dev in d["devices"]] == [None, "1x2"]
    assert d["devices"][0]["user_agent"] == "['x']"  # shown as text, never a crash
    assert spy.calls == 0


def test_failing_live_fallback_shows_a_note_instead_of_a_500(
    client: TestClient,
    session: Session,
    tmp_path,
    spy: _SpyAnalyst,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Bad", answers={"t1-q1": "A", "t1-w1": ESSAY})
    monkeypatch.setattr(AnalysisService, "assess", _boom("malformed stored event"))

    with caplog.at_level("WARNING", logger="app.admin.service"):
        resp = client.get(f"/admin/results/{aid}", headers=headers)
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["verdict"] is None and d["profile"] is None
    assert d["verdict_source"] == "unavailable"
    assert re.search("[а-яё]", d["verdict_note"]) and "не удалось рассчитать" in d["verdict_note"]
    # Everything else on the page is intact: the grade, the answers, the replay.
    assert d["grading"]["score"] >= 1 and d["responses"] and d["events"] and d["timeline"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("live integrity fallback failed" in r.getMessage() for r in warnings)
    # The list still works and simply has no suspicion for the row.
    [row] = client.get("/admin/tests/t1/results", headers=headers).json()
    assert row["suspicion_score"] is None
    assert spy.calls == 0


def test_stored_verdict_survives_a_failing_profile_re_extraction(
    session: Session, tmp_path, spy: _SpyAnalyst, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Crash", answers={"t1-q1": "A", "t1-w1": ESSAY}, events=0)
    stored = _verdict(aid, 0.3, model_id=None, ai=None)
    _store(session, aid, stored, integrity=None)
    monkeypatch.setattr(IntegrityService, "profile", _boom("malformed stored event"))

    detail = svc.attempt_result(aid)
    assert detail.verdict == stored and detail.verdict_source == "stored"
    assert detail.profile is None and detail.verdict_note is None


# --------------------------------------------------------------------------- #
# The list row explains the suspicion: top rule titles, legacy marker.
# --------------------------------------------------------------------------- #
def test_list_row_carries_the_top_rule_titles_and_the_legacy_marker(
    session: Session, tmp_path, spy: _SpyAnalyst
) -> None:
    svc = _svc(session, tmp_path, spy)
    svc.content.create_test(_test())
    session.commit()
    new = _seed(session, name="New", events=0)
    old = _seed(session, name="Old", events=0)

    def hit(code: str, severity: str) -> RuleHit:
        return RuleHit(
            code=code, title_ru=RULE_TITLES[code], severity=severity, explanation_ru="x"
        )

    rules = [
        hit("systematic_tab_switching", "strong"),
        hit("text_appeared_without_typing", "strong"),
        hit("text_appeared_without_typing", "strong"),  # a second writing item
        hit("long_time_away", "weak"),
    ]
    _store(
        session,
        new,
        _verdict(new, 0.9, model_id=None, ai=None).model_copy(
            update={"rules": rules, "rules_version": RULES_VERSION}
        ),
    )
    _store(session, old, _verdict(old, 0.5))  # stored before the rule engine

    rows = {o.display_name: o for o in svc.results_for_test("t1")}
    assert rows["New"].top_rules == [
        RULE_TITLES["systematic_tab_switching"],
        RULE_TITLES["text_appeared_without_typing"],
    ]
    assert rows["New"].verdict_legacy is False
    assert rows["Old"].top_rules == [] and rows["Old"].verdict_legacy is True
    assert spy.calls == 0
