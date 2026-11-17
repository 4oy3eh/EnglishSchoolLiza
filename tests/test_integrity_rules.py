"""Golden tests for the deterministic integrity rule engine (`app/integrity/rules.py`).

The rule engine is the advisory verdict OF RECORD: explainable hits + a bounded
suspicion, a pure function of the event stream (golden rule #6).

* an honest stream fires nothing;
* leaving the tab before (almost) every answer and answering at once fires the
  strong `systematic_tab_switching` rule with the exact Russian sentence and the
  raw moments behind it; fewer such answers -> medium / the weak isolated rule;
* time away from the tab: weak / medium / strong by absolute time and by share of
  the working time;
* writing: text that appeared without typing (strong), a blocked large paste
  (medium), and the `telemetry_gap` downgrade to weak;
* device change, frequent fullscreen exits;
* a possible screenshot — a key chord, or the overlay-blur pattern derived from
  blur / focus / visibility events — is WEAK only and, like any number of weak
  signals, can never push the suspicion into the "высокая" band on its own;
* same events -> same report (shuffled, replayed, repeated); hits are ordered
  strongest first; evidence points at real events and is bounded.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.integrity import evaluate_rules, extract_profile
from app.integrity import rules as R
from app.integrity.features import chronological, overlay_blurs
from contracts import AnalysisVerdict, IntegrityEvent, RuleHit
from tests.test_writing_integrity import cheat_stream, honest_stream, lost_stats_stream

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
# The dashboard's bands (apps/web/teacher.js): < 0.3 низкая, < 0.6 средняя, else высокая.
MEDIUM_BAND, HIGH_BAND = 0.3, 0.6


def ev(
    event_type: str, secs: float, item_id: str | None = None, payload: dict | None = None
) -> IntegrityEvent:
    ts = T0 + timedelta(seconds=secs)
    return IntegrityEvent(
        attempt_id="att-r",
        item_id=item_id,
        type=event_type,  # type: ignore[arg-type]
        client_ts=ts,
        server_ts=ts,
        payload=payload or {},
    )


def report_for(events: list[IntegrityEvent]) -> R.RuleReport:
    return evaluate_rules(extract_profile("att-r", events), events)


def hit_of(report: R.RuleReport, code: str) -> RuleHit:
    [hit] = [h for h in report.hits if h.code == code]
    return hit


def codes(report: R.RuleReport) -> list[tuple[str, str]]:
    return [(h.code, h.severity) for h in report.hits]


# --------------------------------------------------------------------------- #
# Streams.
# --------------------------------------------------------------------------- #
def honest_questions(n: int = 10) -> list[IntegrityEvent]:
    """`n` questions read and answered with human pauses; the tab is never left."""
    out: list[IntegrityEvent] = []
    for i in range(1, n + 1):
        base = 100 + 30 * i
        out.append(ev("interaction", base, f"q{i}"))
        out.append(ev("answer_change", base + 8 + i % 5, f"q{i}", {"value": "A"}))
    return out


def tab_switch_stream(questions: int, fast: int, *, leave_s: float = 2) -> list[IntegrityEvent]:
    """q1 answered cold; before EVERY later answer the student leaves the tab for
    `leave_s`; the first `fast` of those are answered 1 s after the return, the
    rest 6 s after it."""
    out = [ev("interaction", 0, "q1"), ev("answer_change", 5, "q1", {"value": "A"})]
    for i in range(2, questions + 1):
        base = 20 * i
        gap = 1 if i - 2 < fast else 6
        out += [
            ev("interaction", base, f"q{i}"),
            ev("visibility_hidden", base + 1, f"q{i}"),
            ev("visibility_visible", base + 1 + leave_s, f"q{i}"),
            ev("answer_change", base + 1 + leave_s + gap, f"q{i}", {"value": "B"}),
        ]
    return out


def away_stream(leaves: list[float], span: float) -> list[IntegrityEvent]:
    """One question open for `span` seconds with the given leaves (seconds) inside."""
    out = [ev("interaction", 0, "q1")]
    at = 10.0
    for seconds in leaves:
        out += [ev("visibility_hidden", at), ev("visibility_visible", at + seconds)]
        at += seconds + 5
    assert at + 5 < span, "the leaves do not fit in the span"
    out.append(ev("answer_change", span, "q1", {"value": "A"}))
    return out


# --------------------------------------------------------------------------- #
# Honest work fires nothing.
# --------------------------------------------------------------------------- #
def test_honest_stream_fires_no_rule() -> None:
    events = honest_questions() + honest_stream()  # 10 questions + a typed essay
    report = report_for(events)
    assert report.hits == ()
    assert report.suspicion_score == 0.0
    assert report.confidence == R.CONFIDENCE_FULL
    assert report.codes() == [] and report.top_titles() == []
    assert report.summary_ru() == "Правила не нашли заметных признаков нарушений."


def test_a_brief_glance_away_is_not_a_signal() -> None:
    # Two 5 s leaves far from any answer: below every threshold.
    report = report_for(away_stream([5, 5], span=600))
    assert report.hits == () and report.suspicion_score == 0.0


# --------------------------------------------------------------------------- #
# Systematic tab switching — the owner's headline case.
# --------------------------------------------------------------------------- #
def test_tab_switch_before_every_question_is_the_strong_systematic_rule() -> None:
    events = tab_switch_stream(questions=10, fast=7)
    report = report_for(events)

    assert codes(report) == [("systematic_tab_switching", "strong")]
    hit = report.hits[0]
    assert hit.title_ru == "Систематические уходы со вкладки перед ответами"
    # The numbers of THIS attempt: 9 leaves, 7 of the 10 answers within 2 s.
    assert hit.explanation_ru == (
        "Ушёл со вкладки 9 раз; 7 из 10 ответов дал в течение 2 с после возврата (70 %)."
    )
    # Evidence: the seven questions, and for each the leave, the return, the answer.
    assert hit.evidence.item_ids == [f"q{i}" for i in range(2, 9)]
    refs = [(e.type, e.item_id, e.at) for e in hit.evidence.events]
    assert len(refs) == 21
    assert refs[:3] == [
        ("visibility_hidden", "q2", T0 + timedelta(seconds=41)),
        ("visibility_visible", "q2", T0 + timedelta(seconds=43)),
        ("answer_change", "q2", T0 + timedelta(seconds=44)),
    ]
    by_ts = {(e.type, e.server_ts) for e in events}
    assert all((e.type, e.at) in by_ts for e in hit.evidence.events)  # real events only

    # One strong rule alone puts the attempt in the "высокая" band.
    assert report.suspicion_score == 0.65 >= HIGH_BAND
    assert report.confidence == R.CONFIDENCE_FULL
    assert report.summary_ru() == (
        "Сработало правил: 1 (сильных — 1, средних — 0, слабых — 0). "
        "Главное: Систематические уходы со вкладки перед ответами. "
        "Это сигналы для проверки, а не доказательство."
    )


@pytest.mark.parametrize(
    ("questions", "fast", "expected", "sentence"),
    [
        # exactly half of the answers -> strong
        (10, 5, ("systematic_tab_switching", "strong"), "5 из 10 ответов"),
        # 3 of 10 (30 %) -> medium; 3 of 12 (25 %) is still medium
        (10, 3, ("systematic_tab_switching", "medium"), "3 из 10 ответов"),
        (12, 3, ("systematic_tab_switching", "medium"), "3 из 12 ответов"),
        # 3 of 13 (23 %): below a quarter -> only the weak, isolated rule
        (13, 3, ("fast_answer_after_return", "weak"), "3 из 13 ответов"),
        # fewer than 3 such answers is not a system
        (10, 2, ("fast_answer_after_return", "weak"), "2 из 10 ответов"),
        # fewer than 5 answered questions cannot show a system, even 3 of 4
        (4, 3, ("fast_answer_after_return", "weak"), "3 из 4 ответов"),
    ],
)
def test_systematic_thresholds(
    questions: int, fast: int, expected: tuple[str, str], sentence: str
) -> None:
    report = report_for(tab_switch_stream(questions, fast))
    assert codes(report) == [expected]
    assert sentence in report.hits[0].explanation_ru
    leaves = questions - 1  # the student left before every question but the first
    text = report.hits[0].explanation_ru
    if expected[0] == "systematic_tab_switching":
        assert text.startswith(f"Ушёл со вкладки {leaves} раз")
    else:
        assert f"(уходов со вкладки: {leaves})" in text
    assert len(report.hits[0].evidence.item_ids) == fast


def test_leaving_without_instant_answers_is_not_the_systematic_rule() -> None:
    # Left before every question but always answered 6 s after returning.
    report = report_for(tab_switch_stream(questions=10, fast=0))
    assert report.hits == ()


def test_systematic_thresholds_are_the_documented_constants() -> None:
    assert (R.SYSTEMATIC_MIN_ANSWERED, R.SYSTEMATIC_MIN_FAST) == (5, 3)
    assert (R.SYSTEMATIC_STRONG_SHARE, R.SYSTEMATIC_MEDIUM_SHARE) == (0.5, 0.25)
    assert R.POST_RETURN_FAST_MS == 2000


# --------------------------------------------------------------------------- #
# Time away from the tab: absolute and as a share of the working time.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("leaves", "span", "severity"),
    [
        ([12], 600, "weak"),  # one leave >= 10 s
        ([20, 15], 3600, "weak"),  # 35 s in total (>= 30 s), 1 % of the time
        ([40], 300, "medium"),  # 13 % of the working time (and >= 30 s)
        ([130], 3600, "medium"),  # >= 2 min
        ([100], 150, "medium"),  # 67 % of the time, but < 2 min: not strong
        ([150], 400, "strong"),  # 38 % of the time and >= 2 min
        ([400, 300], 7200, "strong"),  # >= 10 min in total
    ],
)
def test_time_away_severity(leaves: list[float], span: float, severity: str) -> None:
    report = report_for(away_stream(leaves, span))
    assert codes(report) == [("long_time_away", severity)]
    assert len(report.hits[0].evidence.events) == 2 * len(leaves)  # each hide + return


def test_time_away_explanation_carries_the_numbers() -> None:
    report = report_for(away_stream([150, 40], span=600))
    hit = hit_of(report, "long_time_away")
    assert hit.severity == "strong"  # 190 s = 32 % of the 10 minutes
    assert hit.explanation_ru == (
        "Был вне вкладки теста 3 мин 10 с за 2 ухода (самый долгий — 2 мин 30 с); "
        "это 32 % времени работы над тестом (10 мин)."
    )
    assert [e.type for e in hit.evidence.events] == [
        "visibility_hidden",
        "visibility_visible",
        "visibility_hidden",
        "visibility_visible",
    ]


# --------------------------------------------------------------------------- #
# Writing rules (one hit per flag of a writing item).
# --------------------------------------------------------------------------- #
def test_text_that_appeared_without_typing_is_a_strong_rule() -> None:
    events = cheat_stream()  # a 400-char single insert, a blocked 420-char paste, 60 s away
    report = evaluate_rules(extract_profile("att-w", events), events)

    assert codes(report) == [
        ("text_appeared_without_typing", "strong"),
        ("long_time_away", "medium"),
        ("large_paste_attempt", "medium"),
        ("away_while_writing", "medium"),
        ("fullscreen_exit_while_writing", "weak"),
    ]
    appeared = hit_of(report, "text_appeared_without_typing")
    assert appeared.title_ru == "Текст появился без набора с клавиатуры"
    assert appeared.explanation_ru == (
        "Текст появился без набора: вставлено не с клавиатуры: 400 символов. "
        "Итоговый текст — 400 символов, с клавиатуры набрано 10."
    )
    assert appeared.evidence.item_ids == ["w1"]
    assert [e.type for e in appeared.evidence.events] == ["input_burst"]

    paste = hit_of(report, "large_paste_attempt")
    assert paste.explanation_ru.startswith(
        "Попыток вставить текст: 1, самая большая — 420 символов."
    )
    assert [e.type for e in paste.evidence.events] == ["paste_attempt"]
    away = hit_of(report, "away_while_writing")
    assert away.explanation_ru == "Во время письма был вне страницы 1 мин (уходов: 1)."

    assert report.suspicion_score == pytest.approx(0.892)  # 1 - 0.35 * 0.7^3 * 0.9
    assert report.item_likelihood("w1") == pytest.approx(0.8457, abs=1e-4)
    assert report.item_likelihood("other") == 0.0
    assert report.top_titles() == [
        "Текст появился без набора с клавиатуры",
        "Долгое время вне вкладки теста",
    ]


def test_a_blocked_large_paste_alone_is_a_medium_rule() -> None:
    events = [
        *honest_stream(),
        ev("paste_attempt", 12, "w1", {"kind": "paste", "length": 300, "field": "open_writing"}),
        ev("paste_attempt", 13, "w1", {"kind": "paste", "length": 20, "field": "open_writing"}),
    ]
    report = evaluate_rules(extract_profile("att-w", events), events)
    assert codes(report) == [("large_paste_attempt", "medium")]
    hit = report.hits[0]
    assert hit.explanation_ru == (
        "Попыток вставить текст: 2, самая большая — 300 символов. Вставка "
        "заблокирована, но готовый текст был в буфере обмена."
    )
    # Only the LARGE attempt is pointed at (the 20-char one is not evidence).
    assert [(e.type, e.at) for e in hit.evidence.events] == [
        ("paste_attempt", T0 + timedelta(seconds=12))
    ]
    assert report.suspicion_score == 0.3  # one medium rule = exactly "средняя"


def test_telemetry_gap_downgrades_unexplained_text_to_a_weak_rule() -> None:
    # 141 of 201 chars have no keystroke telemetry — but the snapshots show them
    # arriving at typing speed (dropped batches): NOT "text appeared without typing".
    events = lost_stats_stream()
    report = evaluate_rules(extract_profile("att-w", events), events)
    assert codes(report) == [("telemetry_gap", "weak")]
    hit = report.hits[0]
    assert hit.explanation_ru.startswith("Не подтверждено записью набора: 141 символ текста")
    assert "понижен до слабого" in hit.explanation_ru
    assert report.count("strong") == 0
    assert report.suspicion_score == 0.1 < MEDIUM_BAND
    assert report.confidence == R.CONFIDENCE_PARTIAL  # the telemetry is incomplete


def test_every_writing_flag_is_a_rule_with_a_severity() -> None:
    assert set(R.WRITING_RULE_SEVERITY) <= set(R.RULE_TITLES)
    strong = {c for c, s in R.WRITING_RULE_SEVERITY.items() if s == "strong"}
    assert strong == {"text_appeared_without_typing", "scripted_input"}
    assert R.WRITING_RULE_SEVERITY["telemetry_gap"] == "weak"


# --------------------------------------------------------------------------- #
# Device change, fullscreen exits.
# --------------------------------------------------------------------------- #
def test_device_switch_mid_attempt_is_a_medium_rule() -> None:
    events = [
        ev("device_info", 0, None, {"user_agent": "Chrome/Win", "platform": "Win32"}),
        *honest_questions(),
        ev("device_info", 500, None, {"user_agent": "Safari/iPhone", "platform": "iPhone"}),
    ]
    report = report_for(events)
    assert codes(report) == [("device_changed", "medium")]
    assert report.hits[0].explanation_ru == (
        "Тест продолжен на другом устройстве: замечено разных устройств — 2."
    )
    assert [e.type for e in report.hits[0].evidence.events] == ["device_info", "device_info"]
    # A reload on the SAME device is not a device change.
    same = [events[0], *honest_questions(), ev("device_info", 500, None, events[0].payload)]
    assert report_for(same).hits == ()


@pytest.mark.parametrize(
    ("exits", "expected"), [(2, None), (3, "weak"), (5, "weak"), (6, "medium")]
)
def test_frequent_fullscreen_exits(exits: int, expected: str | None) -> None:
    events = honest_questions() + [ev("fullscreen_exit", 50 + i) for i in range(exits)]
    report = report_for(events)
    if expected is None:
        assert report.hits == ()
        return
    assert codes(report) == [("frequent_fullscreen_exits", expected)]
    times = "раза" if exits == 3 else "раз"
    assert report.hits[0].explanation_ru == (
        f"Вышел из полноэкранного режима {exits} {times} за время теста."
    )
    assert len(report.hits[0].evidence.events) == exits


# --------------------------------------------------------------------------- #
# Possible screenshot: key chords + the derived overlay-blur pattern. Weak only.
# --------------------------------------------------------------------------- #
def blur(start: float, seconds: float) -> list[IntegrityEvent]:
    return [ev("window_blur", start), ev("window_focus", start + seconds)]


@pytest.mark.parametrize(
    ("events", "count"),
    [
        (blur(10, 2), 1),  # focus back after 2 s, tab visible all along
        (blur(10, 10), 1),  # exactly the 10 s limit
        (blur(10, 15), 0),  # too long: the student worked elsewhere
        (blur(10, 0.1), 0),  # a flicker
        # blur, then the tab is hidden within a second: an ordinary tab switch
        (
            [
                ev("window_blur", 10),
                ev("visibility_hidden", 10.5),
                ev("visibility_visible", 30),
                ev("window_focus", 30.2),
            ],
            0,
        ),
        # the hide came just BEFORE the blur (the other event order of a tab switch)
        (
            [
                ev("visibility_hidden", 9.9),
                ev("window_blur", 10),
                ev("visibility_visible", 12),
                ev("window_focus", 12.1),
            ],
            0,
        ),
        # an unclosed hide before a reload must not mute later blurs
        (
            [ev("visibility_hidden", 1), ev("device_info", 5), *blur(10, 2), *blur(20, 3)],
            2,
        ),
        (blur(10, 2) + blur(20, 1) + blur(40, 5), 3),
    ],
)
def test_overlay_blur_derivation(events: list[IntegrityEvent], count: int) -> None:
    assert len(overlay_blurs(chronological(events))) == count
    assert extract_profile("att-r", events).overlay_blur_count == count


def test_screenshot_chord_and_overlay_blurs_are_a_weak_rule_only() -> None:
    events = [
        *honest_questions(),
        ev("screenshot_key", 30, "q1", {"chord": "printscreen"}),
        ev("screenshot_key", 60, "q2", {"chord": "printscreen"}),
        ev("screenshot_key", 70, "q2", {"chord": "mac_shift_4"}),
        *blur(31, 3),
        *blur(61, 4),
    ]
    profile = extract_profile("att-r", events)
    assert (profile.screenshot_key_count, profile.overlay_blur_count) == (3, 2)
    report = evaluate_rules(profile, events)

    assert codes(report) == [("possible_screenshot", "weak")]
    hit = report.hits[0]
    assert hit.title_ru == "Возможный снимок экрана"
    assert hit.explanation_ru == (
        "Клавиша снимка экрана нажата 3 раза (PrintScreen, Cmd+Shift+4); окно 2 раза "
        "ненадолго теряло фокус, хотя вкладка оставалась открытой — так выглядит "
        "инструмент снимка экрана (или короткое переключение на другое окно). "
        "Подтвердить снимок экрана невозможно, а фотографию экрана на телефон "
        "обнаружить нельзя — это только слабый сигнал."
    )
    assert [e.type for e in hit.evidence.events] == [
        "screenshot_key",
        "window_blur",
        "window_focus",
        "screenshot_key",
        "window_blur",
        "window_focus",
        "screenshot_key",
    ]
    assert report.suspicion_score == 0.1 < MEDIUM_BAND  # "низкая"


def test_one_short_blur_is_not_mentioned_but_one_key_press_is() -> None:
    assert report_for(honest_questions() + blur(31, 3)).hits == ()
    two = report_for(honest_questions() + blur(31, 3) + blur(61, 3))
    assert codes(two) == [("possible_screenshot", "weak")]
    assert "Клавиша" not in two.hits[0].explanation_ru  # no key was seen
    one_key = report_for(
        honest_questions() + [ev("screenshot_key", 30, "q1", {"chord": "win_shift_s"})]
    )
    assert codes(one_key) == [("possible_screenshot", "weak")]
    assert "Win+Shift+S" in one_key.hits[0].explanation_ru


def test_screenshots_alone_never_exceed_the_medium_band_however_many() -> None:
    events = honest_questions()
    events += [ev("screenshot_key", 20 + i, "q1", {"chord": "printscreen"}) for i in range(60)]
    for i in range(20):
        events += blur(400 + 20 * i, 3)
    report = report_for(events)
    assert codes(report) == [("possible_screenshot", "weak")]
    assert report.suspicion_score == 0.1  # the count does not raise the severity
    assert len(report.hits[0].evidence.events) == R.MAX_EVIDENCE_EVENTS  # bounded


def test_weak_signals_together_cannot_reach_the_high_band() -> None:
    # Every attempt-level weak rule at once: screenshot keys, 4 fullscreen exits,
    # one 12 s leave, one answer right after the return.
    events = [
        ev("interaction", 0, "q1"),
        ev("visibility_hidden", 5, "q1"),
        ev("visibility_visible", 17, "q1"),
        ev("answer_change", 18, "q1", {"value": "A"}),
        *[ev("screenshot_key", 30 + i, "q1", {"chord": "printscreen"}) for i in range(5)],
        *[ev("fullscreen_exit", 50 + i, "q1") for i in range(4)],
        *honest_questions(),
    ]
    report = report_for(events)
    assert {h.severity for h in report.hits} == {"weak"}
    assert sorted(h.code for h in report.hits) == [
        "fast_answer_after_return",
        "frequent_fullscreen_exits",
        "long_time_away",
        "possible_screenshot",
    ]
    assert report.suspicion_score == pytest.approx(1 - 0.9**4)  # 0.3439: "средняя"
    assert MEDIUM_BAND <= report.suspicion_score < HIGH_BAND


# --------------------------------------------------------------------------- #
# The combination.
# --------------------------------------------------------------------------- #
def test_combination_is_bounded_and_strong_rules_dominate() -> None:
    assert R.combine([]) == 0.0
    assert R.combine(["weak"]) == 0.1
    assert R.combine(["medium"]) == 0.3
    assert R.combine(["strong"]) == 0.65
    # Weak signals alone are capped below "высокая", however many there are.
    assert R.combine(["weak"] * 6) == R.WEAK_TIER_CAP == 0.45
    assert R.combine(["weak"] * 500) == 0.45 < HIGH_BAND
    # Mediums add up: two stay "средняя", three reach "высокая".
    assert R.combine(["medium"] * 2) == pytest.approx(0.51)
    assert R.combine(["medium"] * 3) == pytest.approx(0.657)
    # One strong outweighs any pile of weak ones, and never asserts certainty.
    assert R.combine(["strong"]) > R.combine(["weak"] * 500)
    assert R.combine(["strong", "weak"]) == pytest.approx(0.685)
    assert R.combine(["strong"] * 20 + ["medium"] * 20) == R.MAX_SUSPICION == 0.95
    # Monotone: one more hit never lowers the suspicion.
    hits: list = []
    last = 0.0
    for severity in ["weak", "medium", "weak", "strong", "medium", "weak", "strong"]:
        hits.append(severity)
        assert R.combine(hits) >= last
        last = R.combine(hits)


def test_hits_are_ordered_strongest_first_and_each_has_a_russian_title() -> None:
    events = [
        *tab_switch_stream(10, 7),
        *[ev("fullscreen_exit", 300 + i) for i in range(3)],
        ev("device_info", 0, None, {"user_agent": "A"}),
        ev("device_info", 290, None, {"user_agent": "B"}),
        ev("screenshot_key", 295, None, {"chord": "printscreen"}),
    ]
    report = report_for(events)
    assert codes(report) == [
        ("systematic_tab_switching", "strong"),
        ("device_changed", "medium"),
        ("frequent_fullscreen_exits", "weak"),
        ("possible_screenshot", "weak"),
    ]
    assert report.codes() == [h.code for h in report.hits]
    assert [h.title_ru for h in report.hits] == [R.RULE_TITLES[h.code] for h in report.hits]
    assert (report.count("strong"), report.count("medium"), report.count("weak")) == (1, 1, 2)
    # 1 - (1 - 0.65)(1 - 0.30)(1 - 0.19)
    assert report.suspicion_score == pytest.approx(0.8015, abs=1e-4)


# --------------------------------------------------------------------------- #
# Determinism: same events -> same report.
# --------------------------------------------------------------------------- #
def _everything() -> list[IntegrityEvent]:
    return [
        *tab_switch_stream(10, 7),
        *[ev("fullscreen_exit", 300 + i) for i in range(3)],
        ev("screenshot_key", 310, "q3", {"chord": "printscreen"}),
        *blur(320, 2),
        *blur(330, 2),
    ]


def test_same_events_give_the_same_report() -> None:
    events = _everything()
    expected = report_for(events)
    assert len(expected.hits) == 3
    assert report_for(list(events)) == expected  # repeated
    for seed in range(5):  # any ingest order (distinct timestamps)
        shuffled = events[:]
        random.Random(seed).shuffle(shuffled)
        assert report_for(shuffled) == expected
    # A replayed batch (the recorder is at-least-once) changes nothing.
    assert report_for(events + events[5:25]) == expected


def test_report_round_trips_through_the_stored_verdict() -> None:
    report = report_for(_everything())
    verdict = AnalysisVerdict(
        attempt_id="att-r",
        suspicion_score=report.suspicion_score,
        confidence=report.confidence,
        flags=report.codes(),
        summary=report.summary_ru(),
        rules=list(report.hits),
        rules_version=R.RULES_VERSION,
    )
    restored = AnalysisVerdict.model_validate(verdict.model_dump(mode="json"))
    assert restored == verdict and tuple(restored.rules) == report.hits
    # A verdict stored before the rule engine still validates (no migration).
    legacy = AnalysisVerdict.model_validate(
        {"attempt_id": "a", "suspicion_score": 0.4, "confidence": 0.5, "summary": "old"}
    )
    assert legacy.rules == [] and legacy.rules_version is None


# --------------------------------------------------------------------------- #
# Confidence = completeness of the telemetry, not certainty of guilt.
# --------------------------------------------------------------------------- #
def test_confidence_levels() -> None:
    assert report_for([]).confidence == R.CONFIDENCE_NO_EVIDENCE == 0.1
    assert report_for(honest_questions(3)).confidence == R.CONFIDENCE_PARTIAL == 0.4
    assert report_for(honest_questions(10)).confidence == R.CONFIDENCE_FULL == 0.7
    # A cheater's telemetry is as complete as anyone's: confidence is not suspicion.
    assert report_for(tab_switch_stream(10, 7)).confidence == R.CONFIDENCE_FULL
