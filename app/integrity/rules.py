"""Deterministic integrity rules — the advisory verdict OF RECORD (no LLM).

A rule combines numbers the integrity layer already computed (`IntegrityProfile`,
`WritingFeatures.flags`) with FIXED thresholds and says, in Russian and with the
attempt's own numbers, what was observed ("Ушёл со вкладки 9 раз; 7 из 10 ответов
дал в течение 2 с после возврата"). Each hit points at the raw events behind it,
so the teacher's dashboard can jump from the card to the timeline (golden rule #6:
the replay is always next to the verdict).

Why this lives in `app/integrity` and not in `app/analysis`: golden rule #6 splits
the layers by HOW a conclusion is reached, not by what it is about — integrity is
everything deterministic and reproducible, analysis is LLM judgment. A rule here
is a pure function of the event stream (no clock, no randomness, no I/O, no
model): same events -> same hits -> same suspicion. The owner's decision is that
this reproducible engine, not a model, is what the teacher ranks students by; the
LLM (`app/analysis`) is an optional second opinion on writing only and can never
change a number produced here.

Still advisory and score-free (golden rule #2): nothing here sees or moves a
grade. A hit is a reason to look at the replay, never proof.

Rules, thresholds and severities (keep `app/integrity/CLAUDE.md` in sync):

=============================== ======== =======================================
code                            severity fires when
=============================== ======== =======================================
systematic_tab_switching        strong   >= 5 answered questions, >= 3 of them
                                         answered within 2 s of returning to the
                                         tab, and those are >= 50 % of the answers
                                medium   same, but >= 25 % (and < 50 %)
fast_answer_after_return        weak     >= 1 such answer, not systematic
long_time_away                  strong   away >= 10 min in total, or >= 30 % of
                                         the working time (and >= 2 min)
                                medium   >= 2 min, or >= 10 % (and >= 30 s)
                                weak     >= 30 s in total, or one leave >= 10 s
text_appeared_without_typing    strong   WritingFeatures flag (per writing item)
scripted_input                  strong   WritingFeatures flag
typing_speed_implausible        medium   WritingFeatures flag
large_paste_attempt             medium   WritingFeatures flag
away_while_writing              medium   WritingFeatures flag
telemetry_gap                   weak     WritingFeatures flag: the DOWNGRADE of
                                         text_appeared_without_typing when the
                                         keystroke telemetry was lost
no_input_telemetry              weak     WritingFeatures flag
fullscreen_exit_while_writing   weak     WritingFeatures flag
device_changed                  medium   a second device fingerprint mid-attempt
frequent_fullscreen_exits       medium   >= 6 fullscreen exits
                                weak     >= 3
possible_screenshot             weak     >= 1 screenshot key / chord, or >= 2
                                         short window blurs with the tab visible
                                         (NEVER more than weak: it cannot be
                                         confirmed, and a phone photo of the
                                         screen is undetectable)
=============================== ======== =======================================

Suspicion (bounded, documented):

* every hit weighs by its severity only: strong 0.65, medium 0.30, weak 0.10;
* each tier is a noisy-OR of its hits, ``1 - prod(1 - w)``; the weak tier is
  capped at ``WEAK_TIER_CAP`` (0.45), so weak signals alone — however many — stay
  below the dashboard's "высокая" band (0.6) and never rank a student as a
  probable cheater;
* ``suspicion = min(0.95, 1 - (1 - strong)(1 - medium)(1 - weak))``.

So one strong rule alone gives 0.65 ("высокая"), one medium 0.30 ("средняя"; three
mediums 0.66), one weak 0.10 ("низкая"). The same combination over one writing
item's hits is that item's deterministic outside-help likelihood.

Confidence is NOT "how sure we are of cheating" but how complete the telemetry
was: 0.1 nothing to go on, 0.4 partial (fewer than 5 questions seen, or a writing
answer without full keystroke telemetry), 0.7 full.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.integrity.features import (
    POST_RETURN_FAST_MS,
    chronological,
    overlay_blurs,
)
from app.integrity.writing import (
    KEYBOARD_INPUT_TYPES,
    KEYBOARD_MAX_INSERT,
    LARGE_PASTE_CHARS,
    SNAPSHOT_IMPLAUSIBLE_CPM,
    SNAPSHOT_MIN_GROWTH,
    TYPING_IMPLAUSIBLE_CPM,
)
from contracts import (
    IntegrityEvent,
    IntegrityProfile,
    RuleEventRef,
    RuleEvidence,
    RuleHit,
    RuleSeverity,
    WritingFeatures,
)

# Bump when a rule, threshold or the combination changes in a way that makes
# stored hits / suspicion incomparable with new ones.
RULES_VERSION = 1

# --------------------------------------------------------------------------- #
# Thresholds (documented above and in app/integrity/CLAUDE.md — keep in sync).
# --------------------------------------------------------------------------- #
# systematic_tab_switching — "left, came back, answered at once", repeated.
# `POST_RETURN_FAST_MS` (2 s, features.py) defines one such answer.
SYSTEMATIC_MIN_ANSWERED = 5  # fewer answered questions cannot show a system
SYSTEMATIC_MIN_FAST = 3  # at least this many answers right after a return
SYSTEMATIC_STRONG_SHARE = 0.5  # ... making up at least half of the answers
SYSTEMATIC_MEDIUM_SHARE = 0.25  # ... or at least a quarter

# long_time_away — total bounded time with the tab hidden.
AWAY_SINGLE_MS = 10_000  # one leave this long is worth a (weak) mention
AWAY_WEAK_TOTAL_MS = 30_000
AWAY_MEDIUM_TOTAL_MS = 120_000
AWAY_MEDIUM_SHARE = 0.10  # of the observed working time (needs >= AWAY_WEAK_TOTAL_MS)
AWAY_STRONG_TOTAL_MS = 600_000
AWAY_STRONG_SHARE = 0.30  # (needs >= AWAY_MEDIUM_TOTAL_MS)

# frequent_fullscreen_exits — whole attempt.
FULLSCREEN_EXITS_FREQUENT = 3
FULLSCREEN_EXITS_MANY = 6

# possible_screenshot — one key press is enough; a lone short blur is too common
# (a notification, a stray click) to mention.
SCREENSHOT_MIN_OVERLAY_BLURS = 2

# Raw-event pointers kept per hit (the verdict JSON stays small).
MAX_EVIDENCE_EVENTS = 40

# --------------------------------------------------------------------------- #
# Combination.
# --------------------------------------------------------------------------- #
SEVERITY_WEIGHT: dict[RuleSeverity, float] = {"strong": 0.65, "medium": 0.30, "weak": 0.10}
WEAK_TIER_CAP = 0.45  # < 0.6: weak signals alone never reach "высокая"
MAX_SUSPICION = 0.95  # never assert certainty

CONFIDENCE_NO_EVIDENCE = 0.1
CONFIDENCE_PARTIAL = 0.4
CONFIDENCE_FULL = 0.7
CONFIDENCE_MIN_QUESTIONS = 5

_SEVERITY_ORDER: dict[RuleSeverity, int] = {"strong": 0, "medium": 1, "weak": 2}
SEVERITY_RU: dict[RuleSeverity, str] = {
    "strong": "сильный сигнал",
    "medium": "средний сигнал",
    "weak": "слабый сигнал",
}

# code -> Russian title. EVERY code a rule can emit is here (a test scans this
# table against the dashboard's labels); the order is the tie-break inside one
# severity.
RULE_TITLES: dict[str, str] = {
    "systematic_tab_switching": "Систематические уходы со вкладки перед ответами",
    "text_appeared_without_typing": "Текст появился без набора с клавиатуры",
    "scripted_input": "Ввод сгенерирован скриптом",
    "long_time_away": "Долгое время вне вкладки теста",
    "typing_speed_implausible": "Неправдоподобно быстрая печать",
    "large_paste_attempt": "Попытка вставить большой фрагмент текста",
    "away_while_writing": "Уход со страницы во время письма",
    "device_changed": "Смена устройства во время теста",
    "frequent_fullscreen_exits": "Частые выходы из полноэкранного режима",
    "fast_answer_after_return": "Ответ сразу после возвращения на вкладку",
    "telemetry_gap": "Пробел в записи набора текста",
    "no_input_telemetry": "Текст сохранён без зафиксированного набора",
    "fullscreen_exit_while_writing": "Выход из полноэкранного режима во время письма",
    "possible_screenshot": "Возможный снимок экрана",
}
_RULE_ORDER = {code: i for i, code in enumerate(RULE_TITLES)}

# Severity of each per-writing-item rule (the code is the `WritingFeatures` flag).
WRITING_RULE_SEVERITY: dict[str, RuleSeverity] = {
    "text_appeared_without_typing": "strong",
    "scripted_input": "strong",
    "typing_speed_implausible": "medium",
    "large_paste_attempt": "medium",
    "away_while_writing": "medium",
    "telemetry_gap": "weak",
    "no_input_telemetry": "weak",
    "fullscreen_exit_while_writing": "weak",
}

_CHORD_RU: dict[str, str] = {
    "printscreen": "PrintScreen",
    "mac_shift_3": "Cmd+Shift+3",
    "mac_shift_4": "Cmd+Shift+4",
    "mac_shift_5": "Cmd+Shift+5",
    "win_shift_s": "Win+Shift+S",
}


# --------------------------------------------------------------------------- #
# Result.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuleReport:
    """What the rule engine concluded about one attempt (pure data)."""

    hits: tuple[RuleHit, ...] = ()
    suspicion_score: float = 0.0
    confidence: float = CONFIDENCE_NO_EVIDENCE

    def codes(self) -> list[str]:
        """Distinct codes of the hits, strongest first (the verdict's `flags`)."""
        return list(dict.fromkeys(h.code for h in self.hits))

    def count(self, severity: RuleSeverity) -> int:
        return sum(1 for h in self.hits if h.severity == severity)

    def top_titles(self, limit: int = 2) -> list[str]:
        """Distinct titles of the strongest hits (the results table's tooltip)."""
        return list(dict.fromkeys(h.title_ru for h in self.hits))[:limit]

    def item_hits(self, item_id: str) -> list[RuleHit]:
        """The per-writing-item hits of one item, strongest first."""
        return [
            h
            for h in self.hits
            if h.code in WRITING_RULE_SEVERITY and item_id in h.evidence.item_ids
        ]

    def item_likelihood(self, item_id: str) -> float:
        """Deterministic outside-help likelihood of one writing item, in [0, 0.95]."""
        return combine(h.severity for h in self.item_hits(item_id))

    def summary_ru(self) -> str:
        """One or two Russian sentences for the teacher."""
        if not self.hits:
            return "Правила не нашли заметных признаков нарушений."
        return (
            f"Сработало правил: {len(self.hits)} (сильных — {self.count('strong')}, "
            f"средних — {self.count('medium')}, слабых — {self.count('weak')}). "
            f"Главное: {'; '.join(self.top_titles())}. "
            "Это сигналы для проверки, а не доказательство."
        )


def _r(value: float) -> float:
    return round(value, 4)


def combine(severities: Iterable[RuleSeverity]) -> float:
    """The documented bounded combination of hit severities (module docstring)."""
    keep: dict[RuleSeverity, float] = {"strong": 1.0, "medium": 1.0, "weak": 1.0}
    for severity in severities:
        keep[severity] *= 1.0 - SEVERITY_WEIGHT[severity]
    weak = min(WEAK_TIER_CAP, 1.0 - keep["weak"])
    return _r(min(MAX_SUSPICION, 1.0 - keep["strong"] * keep["medium"] * (1.0 - weak)))


# --------------------------------------------------------------------------- #
# Russian wording helpers.
# --------------------------------------------------------------------------- #
def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _times(n: int) -> str:
    return f"{n} {_plural(n, 'раз', 'раза', 'раз')}"


def _chars(n: int) -> str:
    return f"{n} {_plural(n, 'символ', 'символа', 'символов')}"


def _dur(ms: int) -> str:
    """`13 с` / `2 мин 5 с` / `1 ч 4 мин` (whole seconds)."""
    secs = round(ms / 1000)
    if secs < 60:
        return f"{secs} с"
    if secs < 3600:
        minutes, rest = divmod(secs, 60)
        return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"
    return f"{secs // 3600} ч {secs % 3600 // 60} мин"


def _ts(event: IntegrityEvent) -> datetime:
    """Trusted timestamp: server time, falling back to client time if unstamped."""
    return event.server_ts or event.client_ts


def _refs(events: Iterable[IntegrityEvent]) -> list[RuleEventRef]:
    out: list[RuleEventRef] = []
    for event in events:
        if len(out) >= MAX_EVIDENCE_EVENTS:
            break
        out.append(RuleEventRef(type=event.type, at=_ts(event), item_id=event.item_id))
    return out


def _hit(
    code: str,
    severity: RuleSeverity,
    explanation: str,
    *,
    item_ids: Sequence[str] = (),
    events: Iterable[IntegrityEvent] = (),
) -> RuleHit:
    return RuleHit(
        code=code,
        title_ru=RULE_TITLES[code],
        severity=severity,
        explanation_ru=explanation,
        evidence=RuleEvidence(item_ids=list(item_ids), events=_refs(events)),
    )


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #
def evaluate_rules(profile: IntegrityProfile, events: Sequence[IntegrityEvent]) -> RuleReport:
    """Run every rule over `profile` + the raw `events` it was extracted from.

    Pure: the same inputs always give the same report. `events` is only used to
    count answered questions and to point each hit at the raw events behind it;
    every threshold is applied to numbers already in the profile.
    """
    chron = chronological(events)
    hits: list[RuleHit] = []
    hits += _tab_switching(profile, chron)
    hits += _time_away(profile, chron)
    for features in profile.writing:
        hits += _writing(features, chron)
    hits += _device(profile, chron)
    hits += _fullscreen(profile, chron)
    hits += _screenshot(chron)
    # Strongest first; inside a severity the fixed rule order; then emission order
    # (writing items in the profile's order) — `sorted` is stable.
    hits.sort(key=lambda h: (_SEVERITY_ORDER[h.severity], _RULE_ORDER[h.code]))
    return RuleReport(
        hits=tuple(hits),
        suspicion_score=combine(h.severity for h in hits),
        confidence=_confidence(profile),
    )


def _confidence(profile: IntegrityProfile) -> float:
    """Telemetry completeness (see the module docstring), not certainty of guilt."""
    with_text = [w for w in profile.writing if w.final_length > 0]
    has_writing_telemetry = any(w.has_keystroke_telemetry for w in profile.writing)
    if not profile.question_timings and not has_writing_telemetry:
        return CONFIDENCE_NO_EVIDENCE
    incomplete_writing = any(
        not w.has_keystroke_telemetry or w.telemetry_gap for w in with_text
    )
    if len(profile.question_timings) < CONFIDENCE_MIN_QUESTIONS or incomplete_writing:
        return CONFIDENCE_PARTIAL
    return CONFIDENCE_FULL


# --------------------------------------------------------------------------- #
# Tab switching.
# --------------------------------------------------------------------------- #
def _answered_items(chron: Sequence[IntegrityEvent]) -> dict[str, IntegrityEvent]:
    """item_id -> its FIRST `answer_change`, in first-answer order."""
    first: dict[str, IntegrityEvent] = {}
    for event in chron:
        if event.type == "answer_change" and event.item_id is not None:
            first.setdefault(event.item_id, event)
    return first


def _tab_switching(profile: IntegrityProfile, chron: Sequence[IntegrityEvent]) -> list[RuleHit]:
    fast = [
        t.item_id
        for t in profile.question_timings
        if t.post_return_latency_ms is not None
        and t.post_return_latency_ms <= POST_RETURN_FAST_MS
    ]
    if not fast:
        return []
    first_answer = _answered_items(chron)
    answered = len(first_answer)
    share = len(fast) / answered if answered else 0.0
    leaves = len(profile.hidden_intervals)
    window = f"{POST_RETURN_FAST_MS // 1000} с"

    # The raw moments: for each such answer the leave, the return and the answer.
    visibility = [e for e in chron if e.type in ("visibility_hidden", "visibility_visible")]
    evidence: list[IntegrityEvent] = []
    for item_id in fast:
        answer = first_answer.get(item_id)
        if answer is None:
            continue
        before = [e for e in visibility if _ts(e) <= _ts(answer)]
        if before and before[-1].type == "visibility_visible":
            if len(before) >= 2 and before[-2].type == "visibility_hidden":
                if not any(e is before[-2] for e in evidence):
                    evidence.append(before[-2])
            if not any(e is before[-1] for e in evidence):
                evidence.append(before[-1])
        evidence.append(answer)

    systematic = answered >= SYSTEMATIC_MIN_ANSWERED and len(fast) >= SYSTEMATIC_MIN_FAST
    if systematic and share >= SYSTEMATIC_MEDIUM_SHARE:
        severity: RuleSeverity = "strong" if share >= SYSTEMATIC_STRONG_SHARE else "medium"
        return [
            _hit(
                "systematic_tab_switching",
                severity,
                f"Ушёл со вкладки {_times(leaves)}; {len(fast)} из {answered} ответов дал "
                f"в течение {window} после возврата ({round(100 * share)} %).",
                item_ids=fast,
                events=evidence,
            )
        ]
    return [
        _hit(
            "fast_answer_after_return",
            "weak",
            f"{len(fast)} из {answered} ответов дан в течение {window} после возврата на "
            f"вкладку (уходов со вкладки: {leaves}). Для системы этого мало: нужно не "
            f"меньше {SYSTEMATIC_MIN_FAST} таких ответов и "
            f"{round(100 * SYSTEMATIC_MEDIUM_SHARE)} % от всех при "
            f"{SYSTEMATIC_MIN_ANSWERED}+ отвеченных вопросах.",
            item_ids=fast,
            events=evidence,
        )
    ]


def _hide_pairs(
    chron: Sequence[IntegrityEvent],
) -> list[tuple[IntegrityEvent, IntegrityEvent]]:
    """`(visibility_hidden, visibility_visible)` pairs, as `features._hidden_intervals`."""
    pairs: list[tuple[IntegrityEvent, IntegrityEvent]] = []
    pending: IntegrityEvent | None = None
    for event in chron:
        if event.type == "visibility_hidden":
            if pending is None:
                pending = event
        elif event.type == "visibility_visible" and pending is not None:
            pairs.append((pending, event))
            pending = None
    return pairs


def _time_away(profile: IntegrityProfile, chron: Sequence[IntegrityEvent]) -> list[RuleHit]:
    total = profile.total_hidden_ms
    longest = max((iv.duration_ms for iv in profile.hidden_intervals), default=0)
    span = profile.attempt_span_ms
    share = min(1.0, total / span) if span > 0 else 0.0

    severity: RuleSeverity | None = None
    if total >= AWAY_STRONG_TOTAL_MS or (
        share >= AWAY_STRONG_SHARE and total >= AWAY_MEDIUM_TOTAL_MS
    ):
        severity = "strong"
    elif total >= AWAY_MEDIUM_TOTAL_MS or (
        share >= AWAY_MEDIUM_SHARE and total >= AWAY_WEAK_TOTAL_MS
    ):
        severity = "medium"
    elif total >= AWAY_WEAK_TOTAL_MS or longest >= AWAY_SINGLE_MS:
        severity = "weak"
    if severity is None:
        return []

    leaves = len(profile.hidden_intervals)
    text = (
        f"Был вне вкладки теста {_dur(total)} за "
        f"{leaves} {_plural(leaves, 'уход', 'ухода', 'уходов')} "
        f"(самый долгий — {_dur(longest)})"
    )
    if span > 0:
        text += f"; это {round(100 * share)} % времени работы над тестом ({_dur(span)})"
    # The longest leaves first, then back in chronological order for the replay.
    pairs = _hide_pairs(chron)
    longest_pairs = sorted(
        range(len(pairs)),
        key=lambda i: -(_ts(pairs[i][1]) - _ts(pairs[i][0])).total_seconds(),
    )[: MAX_EVIDENCE_EVENTS // 2]
    evidence = [e for i in sorted(longest_pairs) for e in pairs[i]]
    return [_hit("long_time_away", severity, text + ".", events=evidence)]


# --------------------------------------------------------------------------- #
# Writing (one hit per flag of one writing item).
# --------------------------------------------------------------------------- #
def _is_bulk_insert(event: IntegrityEvent) -> bool:
    """An `input_burst` that is not ordinary typing (integrity's own classification)."""
    payload = event.payload or {}
    input_type = str(payload.get("input_type", ""))
    try:
        length = int(payload.get("length") or 0)
    except (TypeError, ValueError):
        length = 0
    return input_type not in KEYBOARD_INPUT_TYPES or length > KEYBOARD_MAX_INSERT


def _writing(w: WritingFeatures, chron: Sequence[IntegrityEvent]) -> list[RuleHit]:
    mine = [e for e in chron if e.item_id == w.item_id]
    hits: list[RuleHit] = []
    for flag in w.flags:
        severity = WRITING_RULE_SEVERITY.get(flag)
        if severity is None:  # a flag this engine does not know: not a rule
            continue
        text, evidence = _writing_explanation(flag, w, mine)
        hits.append(_hit(flag, severity, text, item_ids=[w.item_id], events=evidence))
    return hits


def _writing_explanation(
    flag: str, w: WritingFeatures, mine: list[IntegrityEvent]
) -> tuple[str, list[IntegrityEvent]]:
    if flag == "text_appeared_without_typing":
        parts: list[str] = []
        if w.non_keyboard_chars:
            parts.append(f"вставлено не с клавиатуры: {_chars(w.non_keyboard_chars)}")
        if w.unexplained_chars:
            parts.append(f"без единого события ввода появилось: {_chars(w.unexplained_chars)}")
        if (
            w.max_snapshot_growth >= SNAPSHOT_MIN_GROWTH
            and w.max_snapshot_growth_ms is not None
            and w.max_snapshot_growth * 60_000
            > SNAPSHOT_IMPLAUSIBLE_CPM * max(1, w.max_snapshot_growth_ms)
        ):  # the same "no human types this fast" jump the flag itself checks
            parts.append(
                f"за один шаг текст вырос на {_chars(w.max_snapshot_growth)} "
                f"за {_dur(w.max_snapshot_growth_ms)}"
            )
        return (
            "Текст появился без набора: "
            + ("; ".join(parts) or "без подтверждающих нажатий")
            + f". Итоговый текст — {_chars(w.final_length)}, с клавиатуры набрано "
            f"{w.typed_chars}.",
            [e for e in mine if e.type == "input_burst" and _is_bulk_insert(e)],
        )
    if flag == "scripted_input":
        return (
            f"Событий ввода, сгенерированных скриптом, а не клавиатурой: "
            f"{w.untrusted_inputs}.",
            [
                e
                for e in mine
                if e.type == "input_burst" and (e.payload or {}).get("trusted") is False
            ],
        )
    if flag == "typing_speed_implausible":
        return (
            f"Скорость набора ≈{w.chars_per_active_minute or 0:.0f} симв./мин на "
            f"{_chars(w.typed_chars)} — быстрее, чем печатает человек "
            f"(порог {TYPING_IMPLAUSIBLE_CPM} симв./мин).",
            [],
        )
    if flag == "large_paste_attempt":
        return (
            f"Попыток вставить текст: {w.paste_attempts}, самая большая — "
            f"{_chars(w.max_paste_attempt_length)}. Вставка заблокирована, но готовый "
            "текст был в буфере обмена.",
            [
                e
                for e in mine
                if e.type == "paste_attempt"
                and _int((e.payload or {}).get("length")) >= LARGE_PASTE_CHARS
            ],
        )
    if flag == "away_while_writing":
        away = max(w.hidden_ms_while_writing, w.blur_ms_while_writing)
        return (
            f"Во время письма был вне страницы {_dur(away)} (уходов: {w.away_count}).",
            [e for e in mine if e.type in ("visibility_hidden", "window_blur")],
        )
    if flag == "telemetry_gap":
        return (
            f"Не подтверждено записью набора: {_chars(w.unexplained_chars)} текста, но "
            f"запись прерывалась (перезагрузок страницы во время письма: "
            f"{w.recorder_restarts_while_writing}; набрано с обычной скоростью без записи "
            f"нажатий: {w.uncovered_snapshot_growth}). Поэтому сигнал «текст появился "
            "без набора» понижен до слабого.",
            [],
        )
    if flag == "no_input_telemetry":
        return (
            f"Ответ из {_chars(w.final_length)} сохранён без единого события ввода: "
            "он пришёл в обход страницы теста, либо телеметрия не дошла.",
            [],
        )
    # fullscreen_exit_while_writing
    return (
        f"Выходов из полноэкранного режима во время письма: "
        f"{w.fullscreen_exits_while_writing}.",
        [e for e in mine if e.type == "fullscreen_exit"],
    )


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Device, fullscreen, screenshot.
# --------------------------------------------------------------------------- #
def _device(profile: IntegrityProfile, chron: Sequence[IntegrityEvent]) -> list[RuleHit]:
    if not profile.device_changed:
        return []
    return [
        _hit(
            "device_changed",
            "medium",
            f"Тест продолжен на другом устройстве: замечено разных устройств — "
            f"{profile.device_count}.",
            events=[e for e in chron if e.type == "device_info"],
        )
    ]


def _fullscreen(profile: IntegrityProfile, chron: Sequence[IntegrityEvent]) -> list[RuleHit]:
    exits = profile.fullscreen_exit_count
    if exits < FULLSCREEN_EXITS_FREQUENT:
        return []
    severity: RuleSeverity = "medium" if exits >= FULLSCREEN_EXITS_MANY else "weak"
    return [
        _hit(
            "frequent_fullscreen_exits",
            severity,
            f"Вышел из полноэкранного режима {_times(exits)} за время теста.",
            events=[e for e in chron if e.type == "fullscreen_exit"],
        )
    ]


def _screenshot(chron: Sequence[IntegrityEvent]) -> list[RuleHit]:
    keys = [e for e in chron if e.type == "screenshot_key"]
    overlays = overlay_blurs(chron)
    if not keys and len(overlays) < SCREENSHOT_MIN_OVERLAY_BLURS:
        return []
    parts: list[str] = []
    if keys:
        chords = Counter(str((e.payload or {}).get("chord", "")) for e in keys)
        names = ", ".join(_CHORD_RU.get(c, c or "неизвестная") for c in chords)
        parts.append(f"клавиша снимка экрана нажата {_times(len(keys))} ({names})")
    if overlays:
        parts.append(
            f"окно {_times(len(overlays))} ненадолго теряло фокус, хотя вкладка "
            "оставалась открытой — так выглядит инструмент снимка экрана (или короткое "
            "переключение на другое окно)"
        )
    text = "; ".join(parts)
    return [
        _hit(
            "possible_screenshot",
            "weak",  # by design never more: see the module docstring
            text[0].upper() + text[1:] + ". Подтвердить снимок экрана невозможно, а "
            "фотографию экрана на телефон обнаружить нельзя — это только слабый сигнал.",
            events=sorted(
                [*keys, *(e for pair in overlays for e in pair)],
                key=_ts,
            ),
        )
    ]
