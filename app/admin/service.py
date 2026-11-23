"""Admin (teacher) engine: the composition root for the dashboard (Phase 10).

This is the one place allowed to hold grading *and* integrity/analysis at once,
because the teacher dashboard shows them side by side. It never feeds one into
the other (golden rule #2): `attempt_result` grades the attempt and profiles its
behaviour on independent paths and merely *bundles* the two — a suspicion score
never moves a grade, and ranking "suspicious-first" reorders rows without
touching any score.

Responsibilities:
- **bank**: list/get/delete authored tests.
- **review queue**: list ingested `draft` tests and **approve** them — the single
  `draft -> published` human-approval gate (golden rule #5). Approval is the only
  way content goes live; Phase 9 ingestion only ever produces drafts.
- **roster**: add students, live roster status.
- **results**: per-attempt score + advisory verdict + raw replay; per-test ranked
  suspicious-first. The verdict + integrity profile are READ from the frozen
  `AttemptReport` (computed once at finalize); a dashboard read never calls an LLM
  — the read-path `AnalysisService` is required to carry no analyst.

Every admin action logs at INFO (auditability).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.cambridge_scale import estimate as scale_estimate
from app.admin.cambridge_scale import has_scale as scale_has
from app.admin.cambridge_scale import min_reported as scale_min
from app.admin.models import (
    AssetUploadResult,
    AttemptOverview,
    AttemptResult,
    BulkAddResult,
    CambridgeScale,
    DeviceCapture,
    DrawingResponse,
    ImportResult,
    ItemResponse,
    PaperInfo,
    PapersAssignResult,
    ResponseChange,
    ReviewDraft,
    RosterStatus,
    ScaleRow,
    SkillScore,
    SkillTotal,
    StudentReport,
    TestPapers,
    TestStats,
    TimelineEntry,
    WritingTaskReport,
)
from app.analysis import AnalysisService
from app.analysis.llm import AnalysisContext, AnalysisLLM
from app.content import (
    AttemptLayout,
    ContentService,
    RosterService,
    SectionPool,
    TestBank,
    build_attempt_layout,
)
from app.content.editor import (
    MAX_ASSET_BYTES,
    SNIFF_BYTES,
    ContentEditError,
    sniff_media_type,
)
from app.content.storage import StorageBackend
from app.core.logging import get_logger
from app.delivery.projection import project_test
from app.feedback import FeedbackService
from app.feedback.llm import FeedbackLLM
from app.grading.drawing import DrawingPayloadError, parse_drawing
from app.grading.llm import DEFAULT_WRITING_POINTS, LLMGrader
from app.grading.mark_toggle import is_marked
from app.grading.service import (
    GradingError,
    GradingService,
    build_iq_report,
    item_max_points,
)
from app.integrity import IntegrityService
from app.integrity.writing import (
    HISTORY_INPUT_TYPES,
    KEYBOARD_INPUT_TYPES,
    KEYBOARD_MAX_INSERT,
    LARGE_PASTE_CHARS,
)
from app.persistence import models as pm
from app.persistence.repository import (
    AttemptRepository,
    ContentRepository,
    EventRepository,
)
from app.results import ResultsService
from app.results.locks import is_locked
from app.results.service import FROM_SETTINGS, _FromSettings  # see NOTE below
from contracts import (
    AnalysisVerdict,
    Attempt,
    AttemptReport,
    ClientTest,
    DrawingItem,
    DrawingLinesTarget,
    GapFillItem,
    GradingResult,
    IntegrityProfile,
    IQReport,
    Item,
    KeyWordTransformationItem,
    ManualGrade,
    MarkToggleItem,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    RosterEntry,
    SingleChoiceItem,
    Test,
)
from contracts.content import FIVE_SKILL_LEVELS
from contracts.papers import (
    PAPER_LABELS,
    PaperSelectionError,
    explicit_minutes_valid,
    is_partial,
    normalize_papers,
    official_minutes,
    paper_minutes,
    papers_label,
    papers_of,
    partial_label,
    section_paper,
    served_minutes,
    served_papers,
    served_test,
)

log = get_logger(__name__)

# NOTE: `_FromSettings` (the type of the `FROM_SETTINGS` sentinel) is private to
# `app.results.service` — the package exposes no public name for it yet. It is
# imported only to annotate `build_admin_service(analysis_llm=...)`; switch to the
# public alias once `app/results` exports one.


# --------------------------------------------------------------------------- #
# Per-question review rendering (teacher surface — the key is allowed here).
# --------------------------------------------------------------------------- #
def _render_option(item: SingleChoiceItem, key: str) -> str:
    for opt in item.options:
        if opt.key == key:
            return opt.text if opt.kind == "text" else f"[картинка {opt.asset_id}]"
    return key


def _render_correct(item: Item, pool_by_key: dict[str, str]) -> str | None:
    if isinstance(item, SingleChoiceItem):
        return f"{item.correct} — {_render_option(item, item.correct)}"
    if isinstance(item, GapFillItem):
        full = " / ".join([*item.accepted, *item.accepted_variants])
        if item.partial:  # Phase 20: "плоды  ·  частично (1): фрукты / ягоды"
            worth = f"{item.partial_points:g}" if item.partial_points is not None else "½"
            full += f"  ·  частично ({worth}): " + " / ".join(item.partial)
        return full
    if isinstance(item, MatchingItem):
        return f"{item.correct} — {pool_by_key.get(item.correct, '')}"
    if isinstance(item, KeyWordTransformationItem):
        # "a good IDEA | to go  ИЛИ  it is said | to be" — each half 1 mark.
        return "  ИЛИ  ".join(
            f"{' / '.join(a.part_a)} | {' / '.join(a.part_b)}" for a in item.answers
        )
    if isinstance(item, MarkToggleItem):
        return "отметить" if item.correct else "не отмечать"
    if isinstance(item, DrawingItem):
        return "см. рисунок (эталон)"
    return None  # open_writing: no key (LLM/manual)


def _render_student(item: Item, resp: str | None, pool_by_key: dict[str, str]) -> str | None:
    if isinstance(item, MarkToggleItem):
        return "отмечено" if is_marked(resp) else "не отмечено"
    if resp is None or resp == "":
        return None
    if isinstance(item, DrawingItem):
        try:
            _w, _h, strokes = parse_drawing(resp)
        except DrawingPayloadError:
            return "рисунок не читается"
        return f"рисунок: линий {len(strokes)}" if strokes else None
    if isinstance(item, SingleChoiceItem):
        return f"{resp} — {_render_option(item, resp)}"
    if isinstance(item, MatchingItem):
        return f"{resp} — {pool_by_key.get(resp, '')}"
    return resp  # gap_fill / open_writing: stored response is the text itself


def _decode_change(
    item: Item, layout: AttemptLayout, raw: object, pool_by_key: dict[str, str]
) -> str:
    """Render one `answer_change` value as the teacher should read it.

    `single_choice` telemetry carries the *displayed* index, so de-shuffle it back
    to the canonical key first; other item types already report canonical text/key.
    """
    if isinstance(item, SingleChoiceItem):
        try:
            key = layout.to_canonical(item.id, int(raw))  # type: ignore[arg-type]
            return f"{key} — {_render_option(item, key)}"
        except (ValueError, TypeError, IndexError, KeyError):
            return str(raw)
    if isinstance(item, MatchingItem):
        return f"{raw} — {pool_by_key.get(str(raw), '')}"
    return str(raw)


def _render_change(
    item: Item | None, layout: AttemptLayout, payload: dict | None, pool_by_key: dict[str, str]
) -> str:
    """Render one `answer_change` payload (full or recorder-truncated) for the teacher.

    The student recorder truncates a long text answer (> 2048 chars) to
    `{"value": <head>, "tail": <tail>, "length": N, "truncated": true}` so one
    answer can't dominate a telemetry batch. Showing only `value` would pass the
    head off as the whole answer, so a truncated payload renders as
    `"<head> … [+N симв.] … <tail>"`, N being the characters actually omitted.
    """
    p = payload or {}
    raw = p.get("value")
    if p.get("truncated") and isinstance(raw, str):
        tail = p.get("tail") if isinstance(p.get("tail"), str) else ""
        try:
            length = int(p.get("length"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            length = len(raw) + len(tail)
        omitted = max(0, length - len(raw) - len(tail))
        return f"{raw} … [+{omitted} симв.] … {tail}"
    if item is None:
        return str(raw)
    return _decode_change(item, layout, raw, pool_by_key)


@dataclass
class _ReviewContext:
    """Shared lookups for building the per-question review and the timeline."""

    test: Test
    layout: AttemptLayout
    item_no: dict[str, str]  # item_id -> printed question number
    item_by_id: dict[str, tuple]  # item_id -> (section, item)
    pool_by_item: dict[str, dict]  # item_id -> {pool key: text}


@dataclass
class _ResultRow:
    """One started attempt of a test with its merged grade (shared by the results
    table and the CSV export)."""

    entry: RosterEntry
    attempt: Attempt
    report: AttemptReport | None
    grading: GradingResult | None  # None only when built with fallback_grade=False
    status: str
    event_count: int


def _finished_at(attempt: Attempt) -> datetime | None:
    """When an attempt finished: its submit, or the deadline for an expired one."""
    if attempt.status == "submitted":
        return attempt.submitted_at
    if attempt.status == "expired":
        return attempt.submitted_at or attempt.deadline
    return None


def _skill_by_item(test: Test) -> dict[str, str | None]:
    """item id -> the skill its marks are reported under (`Section.score_skill`:
    B2-C2 Use of English parts report `use_of_english`, not `reading`)."""
    return {
        item.id: section.score_skill for section in test.sections for item in section.items
    }


def _csv_skills(test: Test) -> tuple[str, ...]:
    """The per-skill CSV columns: Use of English (after Reading) for a B2-C2 test
    or any test with a section reporting it; otherwise the historical three."""
    reports_uoe = any(sec.score_skill == "use_of_english" for sec in test.sections)
    return FIVE_SKILL_SKILLS if test.level in FIVE_SKILL_LEVELS or reports_uoe else SKILLS


def _skills_average(per_skill: dict[str, tuple[float, float]]) -> float | None:
    """Mean of the per-skill percentages over the skills present (B2-C2 overall:
    Cambridge averages five skills; Speaking is not run here)."""
    pcts = [100.0 * s / m for s, m in per_skill.values() if m > 0]
    return round(sum(pcts) / len(pcts), 1) if pcts else None


def _cambridge_scale(level: str, per_skill: dict[str, tuple[float, float]]):
    """The approximate Cambridge English Scale view (B2-C2), else None."""
    if not scale_has(level):
        return None
    rows, overall, overall_cefr = scale_estimate(level, per_skill)
    if not rows:
        return None
    return CambridgeScale(
        skills=[
            ScaleRow(
                skill=r.skill, score=r.score, max_score=r.max_score, scale=r.scale, cefr=r.cefr
            )
            for r in rows
        ],
        overall=overall,
        overall_cefr=overall_cefr,
        min_reported=scale_min(level),
        note=(
            "Оценка по шкале Cambridge (примерно): пересчёт по официальным таблицам для "
            "пробных тестов, только для ориентира. Общий балл — среднее по навыкам без "
            "Speaking (он здесь не проводится)."
        ),
    )


def _choose_one_max(
    grading: GradingResult, item_id: str, groups: list[list[str]], test: Test | None
) -> float | None:
    """`max_points` a teacher's mark on `item_id` is out of (None = not graded).

    A choose-one writing task stored uncounted (`max_points=0`) is out of ITS OWN
    group's maximum: the counted sibling's `max_points` in `groups` (the test's
    choose-one groups, `GradingService.choose_one_groups`), else the item's own
    definition (`item_max_points`), else `DEFAULT_WRITING_POINTS`. Never another
    part's writing task: with Part 1 worth 10 and the Part 2 group worth 5, the
    uncounted Part 2 task is out of 5 (the grader's `_override_choose_one` uses the
    same number). Every other grade keeps its own `max_points`.
    """
    target = next((g for g in grading.items if g.item_id == item_id), None)
    if target is None:
        return None
    writing = ("open_writing_llm", "open_writing_manual")
    if target.max_points > 0 or target.method not in writing:
        return target.max_points
    group = next((ids for ids in groups if item_id in ids), [item_id])
    counted = [g.max_points for g in grading.items if g.item_id in group and g.max_points > 0]
    if counted:
        return max(counted)
    item = None
    if test is not None:
        item = next((i for sec in test.sections for i in sec.items if i.id == item_id), None)
    if item is None:
        return DEFAULT_WRITING_POINTS
    return item_max_points(item, test.level if test is not None else None)


def _per_skill(
    grading: GradingResult, skill_of: dict[str, str | None]
) -> dict[str, tuple[float, float]]:
    """Sum the (merged) item grades per skill -> {skill: (score, max)}."""
    out: dict[str, list[float]] = {}
    for g in grading.items:
        skill = skill_of.get(g.item_id)
        if skill is None:
            continue
        acc = out.setdefault(skill, [0.0, 0.0])
        acc[0] += g.awarded
        acc[1] += g.max_points
    return {k: (round(v[0], 2), round(v[1], 2)) for k, v in out.items()}


def _num(x: float) -> str:
    """A score cell: '7' rather than '7.0', '2.5' kept."""
    return f"{x:g}"


# A text cell starting with one of these is evaluated as a formula (or a DDE call)
# by Excel / LibreOffice / Google Sheets: `=`, `+`, `-`, `@`, their full-width
# look-alikes, and tab / CR / LF (which some importers strip before re-parsing).
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")


def _csv_text(value: str | None) -> str:
    """A free-text CSV cell made safe against formula ("CSV") injection.

    A student-controlled value such as `=HYPERLINK(...)` or `@SUM(...)` must reach
    the teacher's spreadsheet as text: it is prefixed with `'` (the spreadsheet
    "this is text" marker — the OWASP mitigation). Applied to every free-text cell;
    numeric cells are written by `_num` / as numbers and never pass through here.
    """
    text = value or ""
    return "'" + text if text.startswith(_CSV_FORMULA_PREFIXES) else text


# Events that signal where attention went (highlighted in the human timeline).
_INTEGRITY_EVENT_TYPES = frozenset(
    {
        "visibility_hidden",
        "visibility_visible",
        "window_blur",
        "window_focus",
        "pagehide",
        "device_info",
        "screenshot_key",
    }
)


_SCREENSHOT_CHORD_RU = {
    "printscreen": "PrintScreen",
    "mac_shift_3": "Cmd+Shift+3",
    "mac_shift_4": "Cmd+Shift+4",
    "mac_shift_5": "Cmd+Shift+5",
    "win_shift_s": "Win+Shift+S",
}

# The runner's notes on a listening recording that stopped (`audio_play` with
# `payload.note`, apps/web/exam.js `noteAudio`). Capture only: a technical event,
# never a cheating signal.
_AUDIO_NOTE_RU = {
    "play_refused": (
        "запись остановилась, браузер не дал продолжить (ученику предложено «Слушать дальше»)"
    ),
    "load_error": "запись не загрузилась (ученику предложено повторить)",
    "skipped": "запись так и не удалось воспроизвести — пропущена",
}


def _fmt_pos(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    return f"{total // 60}:{total % 60:02d}"


def _timeline_label(ev, no: str | None, item, pool: dict, layout: AttemptLayout) -> str:
    """Render one captured event as a plain sentence for a human reviewer.

    Russian, like the rest of the teacher dashboard (capture only — judges nothing)."""
    p = ev.payload or {}
    q = f"№{no}" if no else (ev.item_id or "")
    t = ev.type
    if t == "device_info":
        bits = [str(b) for b in (p.get("platform"), p.get("user_agent")) if b]
        ip = f" · IP {p['server_ip']}" if p.get("server_ip") else ""
        return "Устройство: " + (" · ".join(bits) or "неизвестно") + ip
    if t == "visibility_hidden":
        return "⚠︎ Ушёл со вкладки теста"
    if t == "visibility_visible":
        if ev.duration_ms:
            return f"Вернулся на вкладку (отсутствовал {round(ev.duration_ms / 1000)} с)"
        return "Вернулся на вкладку"
    if t == "window_blur":
        return "Окно потеряло фокус"
    if t == "window_focus":
        return "Окно снова в фокусе"
    if t == "pagehide":
        return "Закрыл страницу / ушёл с неё"
    if t == "interaction":
        if p.get("blocked"):
            return f"{q}: заблокировано «{p['blocked']}» (попытка копирования/вставки)"
        return f"{q}: взаимодействие"
    if t == "answer_change":
        value = _render_change(item, layout, p, pool)
        return f"{q}: ответ «{value}»"
    if t == "audio_play":
        note = _AUDIO_NOTE_RU.get(str(p.get("note") or ""))
        if note:
            return f"⚠︎ Аудио: {note} (на {_fmt_pos(p.get('position'))})"
        return f"Аудио ▶ с {_fmt_pos(p.get('position'))}"
    if t == "audio_seek":
        return f"Аудио: перемотка → {_fmt_pos(p.get('position'))}"
    if t == "screenshot_key":
        chord = _SCREENSHOT_CHORD_RU.get(str(p.get("chord", "")), "клавиша снимка экрана")
        return f"⚠︎ Нажата клавиша снимка экрана ({chord})"
    return t


# -- writing-field telemetry in the human timeline ---------------------------- #
# The recorder's writing events (shapes and counts only, never text) rendered as
# Russian sentences. Capture only — the sentences state what happened; the
# integrity layer's thresholds are reused solely to decide what to highlight.
_WRITING_TIMELINE_TYPES = frozenset(
    {
        "field_focus",
        "field_blur",
        "writing_stats",
        "input_burst",
        "paste_attempt",
        "fullscreen_exit",
    }
)
_INPUT_TYPE_RU = {
    "insertFromPaste": "вставка из буфера",
    "insertFromPasteAsQuotation": "вставка из буфера",
    "insertFromDrop": "перетаскивание",
    "insertFromYank": "вставка",
    "insertReplacementText": "автозамена / подсказка",
    "insertText": "ввод текста",
    "insertCompositionText": "ввод с экранной клавиатуры",
    "": "без типа — программный ввод",
}


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _chars(n: int) -> str:
    return f"{n} {_ru_plural(n, 'символ', 'символа', 'символов')}"


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _non_keyboard(input_type: str) -> bool:
    """Not typed and not undo/redo: paste/drop/replacement/script (integrity's rule)."""
    return input_type not in KEYBOARD_INPUT_TYPES and input_type not in HISTORY_INPUT_TYPES


def _input_kind(input_type: str) -> str:
    return _INPUT_TYPE_RU.get(input_type, input_type)


@dataclass
class _TypingFold:
    """Consecutive `writing_stats` flushes (+ short keyboard bursts) of ONE writing
    field, folded into a single timeline line per focus session — a 20-minute essay
    flushes every 10 s, which would otherwise flood the timeline."""

    item_id: str | None
    no: str | None
    at: datetime
    flushes: int = 0
    typed: int = 0
    non_keyboard: int = 0
    deleted: int = 0
    keystrokes: int = 0
    untrusted: int = 0
    active_ms: int = 0
    short_bursts: int = 0
    non_keyboard_kinds: list[str] = field(default_factory=list)

    def add_stats(self, ev, at: datetime) -> None:
        p = ev.payload or {}
        self.at = at
        self.flushes += 1
        added = p.get("added") if isinstance(p.get("added"), dict) else {}
        for input_type, n in added.items():
            if input_type in KEYBOARD_INPUT_TYPES:
                self.typed += _int(n)
            elif _non_keyboard(str(input_type)) and _int(n):
                self.non_keyboard += _int(n)
                kind = _input_kind(str(input_type))
                if kind not in self.non_keyboard_kinds:
                    self.non_keyboard_kinds.append(kind)
        self.deleted += _int(p.get("deleted_chars"))
        self.keystrokes += _int(p.get("keystrokes"))
        self.untrusted += _int(p.get("untrusted_inputs"))
        self.active_ms += _int(p.get("active_ms"))

    def add_short_burst(self, at: datetime) -> None:
        self.at = max(self.at, at)
        self.short_bursts += 1

    @property
    def suspicious(self) -> bool:
        return self.non_keyboard > 0 or self.untrusted > 0

    def entry(self) -> TimelineEntry:
        q = f"№{self.no}" if self.no else (self.item_id or "")
        if self.flushes:
            presses = _ru_plural(self.keystrokes, "нажатие", "нажатия", "нажатий")
            bits = [
                f"+{_chars(self.typed)} с клавиатуры",
                f"удалено {self.deleted}",
                f"{self.keystrokes} {presses}",
            ]
            if self.active_ms >= 1000:
                bits.append(f"≈{round(self.typed * 60_000 / self.active_ms)} симв./мин")
            label = f"{q}: набор текста — " + ", ".join(bits)
            if self.non_keyboard:
                label += (
                    f"; ⚠︎ без набора +{_chars(self.non_keyboard)}"
                    f" ({', '.join(self.non_keyboard_kinds)})"
                )
            if self.untrusted:
                label += f"; ⚠︎ сгенерировано скриптом: {self.untrusted}"
            if self.flushes > 1:
                label += f" · сводка {self.flushes} замеров"
        else:
            label = f"{q}: набор текста"
        if self.short_bursts:
            label += (
                f" · коротких групповых вводов (≤{KEYBOARD_MAX_INSERT} симв., "
                f"автозамена/свайп): {self.short_bursts}"
            )
        return TimelineEntry(
            at=self.at,
            type="writing_stats",
            item_id=self.item_id,
            item_no=self.no,
            label=label,
            integrity=self.suspicious,
        )


def _is_short_keyboard_burst(ev) -> bool:
    """A trusted keyboard-type burst no longer than a swiped/autocorrected word."""
    p = ev.payload or {}
    return (
        ev.type == "input_burst"
        and p.get("trusted", True) is not False
        and str(p.get("input_type", "")) in KEYBOARD_INPUT_TYPES
        and _int(p.get("length")) <= KEYBOARD_MAX_INSERT
    )


def _writing_event_label(ev, no: str | None) -> tuple[str, bool]:
    """Sentence + highlight for field_focus / field_blur / input_burst /
    paste_attempt / fullscreen_exit (writing_stats is folded, see `_TypingFold`)."""
    p = ev.payload or {}
    q = f"№{no}" if no else (ev.item_id or "")
    target = no or ev.item_id
    in_task = f" в задание {target}" if target else ""
    t = ev.type
    if t == "field_focus":
        length = _int(p.get("length"))
        now = f"в поле уже {_chars(length)}" if length else "поле пустое"
        return f"{q}: перешёл в поле ответа ({now})", False
    if t == "field_blur":
        return f"{q}: вышел из поля ответа (в поле {_chars(_int(p.get('length')))})", False
    if t == "paste_attempt":
        length = _int(p.get("length"))
        verb = "перетащить" if p.get("kind") == "drop" else "вставить"
        mark = "⚠︎ " if length >= LARGE_PASTE_CHARS else ""
        return f"{mark}Попытка {verb} текст ({_chars(length)}){in_task} — заблокирована", True
    if t == "fullscreen_exit":
        where = f" (задание {target})" if target else ""
        return f"⚠︎ Вышел из полноэкранного режима{where}", True
    if t == "input_burst":
        length = _int(p.get("length"))
        input_type = str(p.get("input_type", ""))
        if p.get("trusted", True) is False:
            return f"⚠︎ {q}: ввод сгенерирован скриптом: +{_chars(length)}", True
        if _non_keyboard(input_type):
            return (
                f"⚠︎ {q}: вставка без набора: +{_chars(length)} ({_input_kind(input_type)})",
                True,
            )
        if input_type in HISTORY_INPUT_TYPES:
            return f"{q}: отмена/повтор правки: +{_chars(length)}", False
        return (
            f"⚠︎ {q}: крупный ввод одним событием: +{_chars(length)} "
            f"({_input_kind(input_type)}) — не по одной букве",
            True,
        )
    return t, False


def _opt_str(value: object) -> str | None:
    """A captured free-text field as text (None stays None; anything else -> str)."""
    return None if value is None else str(value)


def _build_devices(events) -> list[DeviceCapture]:
    """Parse `device_info` captures into the teacher-facing device list."""
    out: list[DeviceCapture] = []
    for ev in events:
        if ev.type != "device_info":
            continue
        # `device_info` payloads are not typed on ingest: a malformed one (a string
        # where an object is expected, a list as the user-agent) must not break the
        # teacher's page.
        p = ev.payload if isinstance(ev.payload, dict) else {}
        screen = p.get("screen") if isinstance(p.get("screen"), dict) else {}
        viewport = p.get("viewport") if isinstance(p.get("viewport"), dict) else {}
        out.append(
            DeviceCapture(
                at=ev.server_ts or ev.client_ts,
                user_agent=_opt_str(p.get("user_agent")),
                platform=_opt_str(p.get("platform")),
                timezone=_opt_str(p.get("timezone")),
                language=_opt_str(p.get("language")),
                screen=f"{screen.get('width')}x{screen.get('height')}" if screen else None,
                viewport=f"{viewport.get('width')}x{viewport.get('height')}" if viewport else None,
                server_ip=_opt_str(p.get("server_ip")),
            )
        )
    return out


class AdminError(Exception):
    """An admin operation referenced something that does not exist (HTTP 404).

    `status` is the HTTP code the API layer maps it to; the subclasses below carry
    400 / 409 so a validation or state-conflict refusal is never reported as 404.
    The message is shown to the teacher verbatim (the API's `detail`), so it is
    written in Russian — the dashboard's language. Log lines stay English.
    """

    status: int = 404


class AdminValidationError(AdminError):
    """The request's values are invalid for this item (e.g. a mark out of range) -> 400."""

    status = 400


class AdminStateError(AdminError):
    """The target exists but is in the wrong state for this action -> 409."""

    status = 409


class AdminUnprocessableError(AdminError):
    """A well-formed request naming something the target does not have (a paper
    the test lacks) -> 422, like FastAPI's own refusal of an unknown paper code."""

    status = 422


STARTED_PAPERS_MESSAGE = (
    "Ученик уже начал тест — части менять нельзя: время и задания выданы при старте. "
    "Чтобы выдать другие части, сбросьте его работу («Сбросить»)."
)


# A partial B2-C2 attempt has no overall: Cambridge's overall score is the average
# of ALL skills, so a mean over the served ones (a Listening-only "overall 170")
# would mislead. Per-skill values stay.
OVERALL_PARTIAL_NOTE = "общий балл не считается — сданы не все части"

PAPERS_GONE_WARNING = "выбранных частей больше нет в тесте — будет выдан весь тест"


def _papers_fields(test: Test | None, papers: list[str] | None) -> dict[str, Any]:
    """RosterStatus fields for an assignment: the papers, their label, the minutes,
    and a warning when the stored choice no longer matches the (edited) test."""
    if test is None:
        return {"papers": papers, "papers_label": papers_label(papers) or None}
    present = papers_of(test)
    gone = [p for p in papers or [] if p not in present]
    warning = None
    if papers and len(gone) == len(papers):
        warning = PAPERS_GONE_WARNING  # delivery falls back to the whole test
    elif gone:
        names = ", ".join(PAPER_LABELS.get(p, p) for p in gone)
        warning = f"части «{names}» больше нет в тесте — она не будет выдана"
    if papers is None or not is_partial(test, papers):
        return {
            "papers": None,
            "papers_label": None,
            "minutes": test.duration_minutes,
            "papers_warning": warning,
        }
    shown = served_papers(test, papers)
    return {
        "papers": shown,
        "papers_label": papers_label(shown),
        "minutes": served_minutes(test, shown),
        "papers_warning": warning,
    }


_KEEP: Any = object()  # "use the default" marker for `_roster_row`


def _roster_row(
    entry: RosterEntry,
    attempt: Attempt | None,
    test: Test | None,
    *,
    papers: Any = _KEEP,
    deadline: Any = _KEEP,
    pin: str | None = None,
) -> RosterStatus:
    """One roster row: the entry + its attempt (deadline, served-papers snapshot)."""
    if papers is _KEEP:
        # Once started, show what the attempt actually serves (its snapshot).
        papers = attempt.papers if attempt is not None else entry.papers
    if deadline is _KEEP:
        deadline = attempt.deadline if attempt is not None else None
    return RosterStatus(
        roster_entry_id=entry.id,
        display_name=entry.display_name,
        status=entry.status,
        attempt_id=entry.attempt_id,
        deadline=deadline,
        pin=pin if pin is not None else entry.pin,
        birth_date=entry.birth_date,
        **_papers_fields(test, papers),
    )


# Oldest plausible birth year for the teacher-entered date (typo guard).
MIN_BIRTH_YEAR = 1900


def _check_birth_date(birth_date: date | None, today: date | None = None) -> None:
    """A birth date must be a real past date (422 otherwise). None = clear."""
    if birth_date is None:
        return
    today = today or datetime.now(UTC).date()
    if birth_date > today or birth_date.year < MIN_BIRTH_YEAR:
        log.warning("admin birth date REFUSED: out of range")
        raise AdminValidationError(
            f"Дата рождения должна быть в прошлом и не раньше {MIN_BIRTH_YEAR} года."
        )


def _drawing_response(item: DrawingItem, resp: str | None) -> DrawingResponse:
    """The student's strokes for the teacher's drawing card (key allowed here)."""
    square = item.target.square if isinstance(item.target, DrawingLinesTarget) else None
    base = DrawingResponse(
        asset_id=item.asset_id,
        mode=item.mode,
        square=square.model_dump() if square is not None else None,
        target=item.target.model_dump(mode="json"),
    )
    try:
        w, h, strokes = parse_drawing(resp)
    except DrawingPayloadError:
        return base.model_copy(update={"unreadable": True})
    return base.model_copy(
        update={"w": w, "h": h, "strokes": [[[x, y] for x, y in st] for st in strokes]}
    )


def _iq_raw_mode(test: Test) -> bool:
    """A test whose norms include raw-basis composites (Phase 20, e.g. Amthauer):
    the CSV adds % columns per subtest and raw / % per raw composite."""
    assert test.norm_scoring is not None
    return any(c.basis == "raw" for c in test.norm_scoring.composites)


def _iq_csv_header(test: Test) -> list[str]:
    """IQ columns (only for a test with `norm_scoring`), appended after `parts`:
    per subtest (test order, `iq_s<n>`) raw + scaled (+ `_pct` when the norms have
    raw composites), per composite the scaled sum + score (a raw composite: raw sum,
    % of its maximum, score), then overall, bracket, notes and the age used."""
    assert test.norm_scoring is not None
    raw_mode = _iq_raw_mode(test)
    cols: list[str] = []
    for n, _ in enumerate(test.sections, start=1):
        cols += [f"iq_s{n}_raw", f"iq_s{n}_scaled"]
        if raw_mode:
            cols.append(f"iq_s{n}_pct")
    for comp in test.norm_scoring.composites:
        if comp.basis == "raw":
            cols += [f"iq_{comp.key}_raw", f"iq_{comp.key}_pct", f"iq_{comp.key}_score"]
        else:
            cols += [f"iq_{comp.key}_sum", f"iq_{comp.key}_score"]
    return [*cols, "iq_overall", "iq_bracket", "iq_notes", "iq_age"]


def _iq_csv_cells(test: Test, report: IQReport | None) -> list[object]:
    if report is None:
        return [""] * len(_iq_csv_header(test))
    raw_mode = _iq_raw_mode(test)
    by_section = {s.section_id: s for s in report.sections}
    cells: list[object] = []
    for section in test.sections:
        sc = by_section.get(section.id)
        cells += [
            _num(sc.raw) if sc is not None and sc.raw is not None else "",
            sc.scaled if sc is not None and sc.scaled is not None else "",
        ]
        if raw_mode:
            cells.append(_num(sc.percent) if sc is not None and sc.percent is not None else "")
    by_comp = {c.key: c for c in report.composites}
    assert test.norm_scoring is not None
    for comp in test.norm_scoring.composites:
        c = by_comp.get(comp.key)
        score = _csv_text(c.score_display) if c is not None and c.score_display else ""
        if comp.basis == "raw":
            cells += [
                _num(c.raw_sum) if c is not None and c.raw_sum is not None else "",
                _num(c.percent) if c is not None and c.percent is not None else "",
                score,
            ]
        else:
            cells += [c.scaled_sum if c is not None and c.scaled_sum is not None else "", score]
    cells += [
        _csv_text(report.overall_display) if report.overall_display else "",
        _csv_text(report.bracket) if report.bracket else "",
        _csv_text(" ".join(report.notes)),
        report.age if report.age is not None else "",
    ]
    return cells


# A finished attempt with no report is "pending" (background finalize still
# expected) for this long after it finished; past that it is "missing" — the
# finalize was lost (crash / restart) and the teacher should re-grade. Generous on
# purpose: a slow Gemini writing grade + feedback run can take minutes.
PENDING_GRACE_SECONDS = 300

# Report-status value for an attempt the teacher re-opened after it was graded:
# the stored report is stale until the student submits again ("переоткрыт — ждёт
# сдачи" on the dashboard).
REOPENED = "reopened"

SKILLS = ("reading", "listening", "writing")
# B2-C2 report Use of English as its own skill (Reading and Use of English paper).
FIVE_SKILL_SKILLS = ("reading", "use_of_english", "listening", "writing")

# Seed for the teacher's "preview as a student" option shuffle (any fixed value).
PREVIEW_SEED = 0


# Shown in the detail when no verdict is stored and the live fallback failed.
VERDICT_UNAVAILABLE_NOTE = (
    "Анализ честности сейчас не удалось рассчитать: в записи событий есть "
    "повреждённые данные. Оценка и хронология ниже не затронуты; попробуйте "
    "«Переоценить» или посмотрите исходные события."
)
TOP_RULES_IN_LIST = 2


def _top_rule_titles(verdict: AnalysisVerdict) -> list[str]:
    """Distinct titles of the strongest stored rule hits (already strongest-first)."""
    return list(dict.fromkeys(h.title_ru for h in verdict.rules))[:TOP_RULES_IN_LIST]


def _report_status(report, attempt, now: datetime) -> str:
    """The dashboard chip state: complete / failed / pending / missing.

    With a stored report its own status wins. Without one: a student still working
    is "pending" (nothing to grade yet); a finished attempt whose finalize is in
    flight right now (its per-attempt lock is held — submit task, regrade, sweep or
    the "grade all missing" button) is "pending" however old it is; otherwise it is
    "pending" inside the grace window after it finished (the submit-time background
    finalize is still expected) and "missing" after it — the finalize was lost and
    needs a re-grade. An expired attempt has no `submitted_at`, so its `deadline`
    is the finish time; the in-flight check runs first, so a lazily-expired attempt
    (deadline long past) being finalized right now never flashes "missing".
    """
    if report is not None:
        # Re-opened by the teacher: the stored report grades the PREVIOUS submit and
        # will be redone when the student hands in again — never show it as current.
        if attempt.status == "in_progress":
            return REOPENED
        return report.status
    if attempt.status not in ("submitted", "expired"):
        return "pending"
    if is_locked(attempt.id):
        return "pending"
    finished_at = attempt.submitted_at or attempt.deadline
    if finished_at is None:
        return "missing"
    age = (now - finished_at).total_seconds()
    return "pending" if age < PENDING_GRACE_SECONDS else "missing"


def _default_finalize_missing(test_id: str) -> list[str]:
    """Production runner: per-attempt own-session finalize (looked up at call time).

    Expires the test's abandoned past-deadline attempts first (own short txn), so
    "Дооценить все" also recovers a student who closed the tab and never returned.
    """
    from app.results import finalize as results_finalize

    return results_finalize.finalize_missing(test_id, expire_overdue=True)


class AdminService:
    """Teacher-facing operations over the whole platform (read + the publish gate)."""

    def __init__(
        self,
        *,
        content: ContentService,
        roster: RosterService,
        grading: GradingService,
        integrity: IntegrityService,
        analysis: AnalysisService,
        results: ResultsService,
        attempts: AttemptRepository,
        events: EventRepository,
        finalize_missing_runner: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.content = content
        self.roster = roster
        # Read-path grader: LLM-FREE on purpose. The dashboard never re-runs the
        # writing LLM on load — it reads the frozen `AttemptReport` written once at
        # submit; this grader only fills the deterministic fallback for an attempt
        # not yet finalized (e.g. a pre-feature submit awaiting a manual re-grade).
        self.grading = grading
        # Read-path integrity/analysis: LLM-FREE by construction. The dashboard
        # reads the verdict stored at finalize; `analysis` only computes the
        # deterministic formula for an attempt with no stored verdict yet, and
        # `integrity` only re-extracts a profile a crash-path report lacks.
        if analysis.llm is not None:
            raise ValueError(
                "AdminService: the read-path AnalysisService must carry no LLM analyst "
                "(dashboard reads never call an LLM; the analyst belongs to the "
                "results finalize / regrade)"
            )
        self.integrity = integrity
        self.analysis = analysis
        # LLM-enabled finalize composition (writing grader + feedback analyst), used
        # ONLY for an explicit teacher re-grade — never on a read.
        self.results = results
        self.attempts = attempts
        self.events = events
        # "Grade all missing" runs through `app.results.finalize.finalize_missing`:
        # candidates read in a short transaction, then ONE own session per attempt.
        # It must never loop LLM calls on this (request) session — an open SQLite
        # write transaction would block every student's answer save meanwhile.
        self._finalize_missing_runner = finalize_missing_runner or _default_finalize_missing

    # -- bank --------------------------------------------------------------- #
    def list_tests(self) -> list[Test]:
        return self.content.list_tests()

    def get_test(self, test_id: str) -> Test:
        test = self.content.get_test(test_id)
        if test is None:
            raise AdminError(f"Тест {test_id!r} не найден.")
        return test

    def test_stats(self, test_id: str) -> TestStats:
        """Roster size + attempts (all / still in progress) — one grouped query."""
        self.get_test(test_id)
        session = self.attempts.session
        roster_count = int(
            session.scalar(
                select(func.count())
                .select_from(pm.RosterEntryRow)
                .where(pm.RosterEntryRow.test_id == test_id)
            )
            or 0
        )
        by_status = dict(
            session.execute(
                select(pm.AttemptRow.status, func.count())
                .where(pm.AttemptRow.test_id == test_id)
                .group_by(pm.AttemptRow.status)
            ).all()
        )
        stats = TestStats(
            test_id=test_id,
            roster_count=roster_count,
            attempt_count=int(sum(by_status.values())),
            active_count=int(by_status.get("in_progress", 0)),
        )
        log.info(
            "admin test_stats test=%s roster=%d attempts=%d active=%d",
            test_id,
            stats.roster_count,
            stats.attempt_count,
            stats.active_count,
        )
        return stats

    def delete_test(self, test_id: str, *, force: bool = False) -> None:
        """Delete a test. Refuses (409) while it has attempts unless `force`.

        A forced delete wipes every attempt (answers, events, manual marks,
        reports) and the roster explicitly first, so it never relies on the DB's
        FK cascade. Destructive teacher action — logged loudly."""
        stats = self.test_stats(test_id)
        if stats.attempt_count and not force:
            log.warning(
                "admin delete_test id=%s refused: %d attempt(s), no force",
                test_id,
                stats.attempt_count,
            )
            raise AdminStateError(
                f"В тесте уже есть работы учеников ({stats.attempt_count}). Удаление сотрёт "
                "их безвозвратно — подтвердите удаление вместе с работами."
            )
        session = self.attempts.session
        entry_ids = session.scalars(
            select(pm.RosterEntryRow.id).where(pm.RosterEntryRow.test_id == test_id)
        ).all()
        for entry_id in entry_ids:
            self.attempts.delete_roster_entry(entry_id)
        orphan_ids = session.scalars(
            select(pm.AttemptRow.id).where(pm.AttemptRow.test_id == test_id)
        ).all()
        for attempt_id in orphan_ids:
            self.attempts.delete_attempt(attempt_id)
        if not self.content.delete_test(test_id):  # pragma: no cover - stats guarded
            raise AdminError(f"Тест {test_id!r} не найден.")
        log.info(
            "admin delete_test id=%s force=%s (wiped %d attempt(s), %d roster entr(ies))",
            test_id,
            force,
            stats.attempt_count,
            stats.roster_count,
        )

    # -- review queue (the draft -> published human gate, rule #5) ---------- #
    def review_queue(self) -> list[ReviewDraft]:
        """Every ingested draft awaiting approval."""
        drafts = [
            ReviewDraft(
                test_id=t.id,
                title=t.title,
                level=t.level,
                section_count=len(t.sections),
                item_count=sum(len(s.items) for s in t.sections),
            )
            for t in self.content.list_tests()
            if t.status == "draft"
        ]
        log.info("admin review_queue -> %d draft(s)", len(drafts))
        return drafts

    def approve(self, test_id: str) -> Test:
        """Publish a draft after human review (golden rule #5).

        This is the ONLY path content goes live. Refuses to "approve" anything
        that is not currently a draft, so the gate can't be a silent no-op.
        """
        test = self.get_test(test_id)
        if test.status != "draft":
            raise AdminStateError(f"Тест уже опубликован ({test.status}), а не черновик.")
        if not self.content.publish(test_id):  # pragma: no cover - get_test guarded
            raise AdminError(f"Тест {test_id!r} не найден.")
        log.info("admin APPROVE id=%s draft->published (human-approved)", test_id)
        return self.get_test(test_id)

    def unpublish(self, test_id: str, *, force: bool = False) -> Test:
        """Pull a published test back to draft (revert an approval).

        Mirrors `approve`'s care: refuses anything not currently `published`, so
        the log line can't misrepresent a no-op as a real state transition. Also
        refuses (409) while students are mid-attempt unless `force` — pulling the
        test from under them would strand their session.
        """
        test = self.get_test(test_id)
        if test.status != "published":
            raise AdminStateError(f"Тест не опубликован (сейчас: {test.status}).")
        active = self.test_stats(test_id).active_count
        if active and not force:
            log.warning("admin unpublish id=%s refused: %d active attempt(s)", test_id, active)
            raise AdminStateError(
                f"Тест сейчас пишут ученики ({active}). Снятие с публикации прервёт их "
                "работу — подтвердите, если это действительно нужно."
            )
        self.content.unpublish(test_id)
        log.info(
            "admin unpublish id=%s published->draft force=%s active=%d", test_id, force, active
        )
        return self.get_test(test_id)

    # -- content editing (no code: update / copy / import / export / assets) - #
    def _content(self, action: str, target: str, fn: Callable[[], Any]) -> Any:
        """Run a content-engine write, mapping its refusals to AdminError (same Russian
        text, same 400 / 404 / 409) and logging the admin action."""
        try:
            result = fn()
        except ContentEditError as exc:
            log.warning("admin %s target=%s refused (%d): %s", action, target, exc.status, exc)
            cls = {400: AdminValidationError, 409: AdminStateError}.get(exc.status, AdminError)
            raise cls(str(exc)) from exc
        log.info("admin %s target=%s ok", action, target)
        return result

    def update_test_meta(self, test_id: str, patch: dict, *, force: bool = False) -> Test:
        return self._content(
            "update_test_meta",
            test_id,
            lambda: self.content.update_test_meta(test_id, patch, force=force),
        )

    # -- exam window (schedule meta; allowed on a published test with attempts) #
    def set_window(
        self,
        test_id: str,
        *,
        opens_at: datetime | None,
        closes_at: datetime | None,
        late_start_grace_minutes: int = 0,
    ) -> Test:
        """Set / clear the test's exam window (UTC-aware datetimes, None = no bound).

        Not key-bearing, so no lock and no `force`: running attempts keep their
        deadline (fixed at start, golden rule #3) — only NEW starts and the deadline
        cap of attempts started afterwards follow the new window."""
        return self._content(
            "set_window",
            test_id,
            lambda: self.content.set_schedule(
                test_id,
                opens_at=opens_at,
                closes_at=closes_at,
                late_start_grace_minutes=late_start_grace_minutes,
            ),
        )

    def open_window_now(
        self, test_id: str, minutes: int, *, now: datetime | None = None
    ) -> Test:
        """«Открыть сейчас на N минут»: opens_at = now, closes_at = now + N (server
        clock); the late-start grace is kept. N is the WHOLE session: an attempt
        started later gets min(duration, time left + grace)."""
        now = now or datetime.now(UTC)
        test = self.get_test(test_id)
        log.info("admin open_window_now test=%s minutes=%d", test_id, minutes)
        return self.set_window(
            test_id,
            opens_at=now,
            closes_at=now + timedelta(minutes=minutes),
            late_start_grace_minutes=test.late_start_grace_minutes,
        )

    def close_window_now(self, test_id: str, *, now: datetime | None = None) -> Test:
        """«Закрыть сейчас»: no NEW starts from this moment (closes_at = now, grace 0).

        Students already writing keep their own deadline and may resume until it."""
        now = now or datetime.now(UTC)
        test = self.get_test(test_id)
        opens_at = test.opens_at if test.opens_at is not None and test.opens_at < now else None
        log.info("admin close_window_now test=%s", test_id)
        return self.set_window(
            test_id, opens_at=opens_at, closes_at=now, late_start_grace_minutes=0
        )

    def update_section(
        self, test_id: str, section_id: str, patch: dict, *, force: bool = False
    ) -> Test:
        return self._content(
            "update_section",
            f"{test_id}/{section_id}",
            lambda: self.content.update_section(test_id, section_id, patch, force=force),
        )

    def reorder_sections(self, test_id: str, order: list[str]) -> Test:
        return self._content(
            "reorder_sections", test_id, lambda: self.content.reorder_sections(test_id, order)
        )

    def reorder_items(self, test_id: str, section_id: str, order: list[str]) -> Test:
        return self._content(
            "reorder_items",
            f"{test_id}/{section_id}",
            lambda: self.content.reorder_items(test_id, section_id, order),
        )

    def update_item(self, test_id: str, item_id: str, item: dict, *, force: bool = False) -> Test:
        return self._content(
            "update_item",
            f"{test_id}/{item_id}",
            lambda: self.content.update_item(test_id, item_id, item, force=force),
        )

    def add_item(
        self, test_id: str, section_id: str, item: dict, *, position: int | None = None
    ) -> Test:
        return self._content(
            "add_item",
            f"{test_id}/{section_id}",
            lambda: self.content.add_item(test_id, section_id, item, position=position),
        )

    def remove_item(self, test_id: str, item_id: str) -> Test:
        return self._content(
            "remove_item",
            f"{test_id}/{item_id}",
            lambda: self.content.remove_item(test_id, item_id),
        )

    def add_section(self, test_id: str, section: dict, *, position: int | None = None) -> Test:
        return self._content(
            "add_section",
            test_id,
            lambda: self.content.add_section(test_id, section, position=position),
        )

    def remove_section(self, test_id: str, section_id: str) -> Test:
        return self._content(
            "remove_section",
            f"{test_id}/{section_id}",
            lambda: self.content.remove_section(test_id, section_id),
        )

    def duplicate_test(self, test_id: str) -> Test:
        """A draft copy (`<id>-v2`) — the safe way to change a published test's key."""
        return self._content(
            "duplicate_test", test_id, lambda: self.content.duplicate_test(test_id)
        )

    def export_test(self, test_id: str) -> Test:
        """The authoring Test WITH its answer key — teacher-only download."""
        return self._content("export_test", test_id, lambda: self.content.export_test(test_id))

    def import_test(
        self,
        data: bytes | str | dict,
        assets: dict[str, bytes | BinaryIO],
        *,
        replace: bool = False,
        new_id: str | None = None,
    ) -> ImportResult:
        """Store an authored Test JSON (+ files) as a DRAFT -> the review queue.

        `assets` values may be bytes or seekable streams (the HTTP layer passes the
        spooled upload files, so nothing is held in memory). `unpublished` is True
        when `replace` turned a published test back into a draft."""
        outcome = self._content(
            "import_test",
            new_id or "(id from file)",
            lambda: self.content.import_test(data, assets, replace=replace, new_id=new_id),
        )
        test = outcome.test
        return ImportResult(
            test_id=test.id,
            title=test.title,
            status=test.status,
            replaced=outcome.replaced,
            unpublished=outcome.unpublished,
            section_count=len(test.sections),
            item_count=sum(len(s.items) for s in test.sections),
            stored_assets=outcome.stored_assets,
            ignored_assets=outcome.ignored_assets,
        )

    def upload_asset(
        self, filename: str, data: bytes | BinaryIO, *, max_bytes: int = MAX_ASSET_BYTES
    ) -> AssetUploadResult:
        """Store one file (bytes or a seekable stream, copied in chunks)."""
        asset_id = self._content(
            "upload_asset",
            filename,
            lambda: self.content.upload_asset(filename, data, max_bytes=max_bytes),
        )
        if isinstance(data, bytes | bytearray):
            size, head = len(data), bytes(data[:SNIFF_BYTES])
        else:
            data.seek(0, io.SEEK_END)
            size = data.tell()
            data.seek(0)
            head = data.read(SNIFF_BYTES)
        return AssetUploadResult(
            asset_id=asset_id,
            bytes=size,
            content_type=sniff_media_type(head) or "application/octet-stream",
        )

    def preview_test(self, test_id: str) -> ClientTest:
        """The student-facing projection (NO answer key, rule #1) for the teacher's
        "preview as a student". Same projection + default bank as delivery; options
        shuffled with a fixed preview seed (each student gets their own order)."""
        test = self.get_test(test_id)
        bank = TestBank(
            test_id=test.id,
            pools=[SectionPool(key=s.id, sections=[s], pick=1) for s in test.sections],
        )
        client = project_test(test, build_attempt_layout(bank, PREVIEW_SEED))
        log.info("admin preview_test id=%s sections=%d (keyless)", test_id, len(client.sections))
        return client

    # -- roster ------------------------------------------------------------- #
    def _normalize_papers(self, test: Test, papers: Iterable[str] | None) -> list[str] | None:
        """The teacher's choice of papers validated against the test (422 Russian)."""
        try:
            return normalize_papers(test, papers)
        except PaperSelectionError as exc:
            log.warning("admin papers REFUSED test=%s papers=%s: %s", test.id, papers, exc)
            raise AdminUnprocessableError(str(exc)) from exc

    def test_papers(self, test_id: str) -> TestPapers:
        """The test's Cambridge papers with their effective minutes (editor +
        assignment UI): explicit `paper_minutes`, else the proportional split."""
        test = self.get_test(test_id)
        minutes = paper_minutes(test)
        infos = []
        for paper in papers_of(test):
            sections = [s for s in test.sections if section_paper(test.level, s) == paper]
            infos.append(
                PaperInfo(
                    paper=paper,
                    label=PAPER_LABELS[paper],
                    minutes=minutes[paper],
                    official_minutes=official_minutes(test.level, paper),
                    section_count=len(sections),
                    item_count=sum(len(s.items) for s in sections),
                )
            )
        return TestPapers(
            test_id=test.id,
            level=test.level,
            duration_minutes=test.duration_minutes,
            explicit=explicit_minutes_valid(test),
            papers=infos,
        )

    def set_paper_minutes(self, test_id: str, minutes: dict[str, Any] | None) -> Test:
        """Explicit per-paper minutes (None = proportional). Timing meta: allowed on
        a published test with attempts; running deadlines never move."""
        self.get_test(test_id)
        test = self._content(
            "set_paper_minutes", test_id, lambda: self.content.set_paper_minutes(test_id, minutes)
        )
        log.info("admin set_paper_minutes test=%s -> %s", test_id, test.paper_minutes)
        return test

    def add_student(
        self,
        test_id: str,
        display_name: str,
        papers: Iterable[str] | None = None,
        birth_date: date | None = None,
    ) -> RosterEntry:
        test = self.get_test(test_id)  # 404 on an unknown test rather than a dangling entry
        chosen = self._normalize_papers(test, papers)
        _check_birth_date(birth_date)
        entry = self.roster.add_student(test_id, display_name, chosen)
        if birth_date is not None:
            self.roster.set_birth_date(entry.id, birth_date)
            entry = entry.model_copy(update={"birth_date": birth_date})
        log.info(
            "admin add_student test=%s name=%s entry=%s papers=%s",
            test_id,
            display_name,
            entry.id,
            chosen or "all",
        )
        return entry

    def set_student_papers(
        self, roster_entry_id: str, papers: Iterable[str] | None
    ) -> RosterStatus:
        """Assign one student the whole test (None) or some papers — only before
        they start (409 afterwards: the served parts and the deadline are fixed)."""
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise AdminError("Ученик не найден в списке (возможно, уже удалён).")
        test = self.get_test(entry.test_id)
        chosen = self._normalize_papers(test, papers)
        if entry.attempt_id or entry.status != "not_started" or not self.roster.set_papers(
            roster_entry_id, chosen
        ):
            log.warning(
                "admin set_papers REFUSED entry=%s status=%s: already started",
                roster_entry_id,
                entry.status,
            )
            raise AdminStateError(STARTED_PAPERS_MESSAGE)
        log.info(
            "admin set_papers entry=%s test=%s papers=%s",
            roster_entry_id,
            entry.test_id,
            chosen or "all",
        )
        return _roster_row(entry, None, test, papers=chosen)

    def set_class_papers(
        self,
        test_id: str,
        papers: Iterable[str] | None,
        roster_entry_ids: Iterable[str] | None = None,
    ) -> PapersAssignResult:
        """Assign papers to the whole class (or the listed students). Students who
        already started keep theirs and are listed in `skipped_started`."""
        test = self.get_test(test_id)
        chosen = self._normalize_papers(test, papers)
        wanted = set(roster_entry_ids) if roster_entry_ids is not None else None
        updated: list[RosterStatus] = []
        skipped: list[str] = []
        for entry in self.roster.list_students(test_id):
            if wanted is not None and entry.id not in wanted:
                continue
            if (
                entry.attempt_id
                or entry.status != "not_started"
                or not self.roster.set_papers(entry.id, chosen)
            ):
                skipped.append(entry.display_name)
                continue
            updated.append(_roster_row(entry, None, test, papers=chosen))
        log.info(
            "admin set_class_papers test=%s papers=%s updated=%d skipped_started=%d",
            test_id,
            chosen or "all",
            len(updated),
            len(skipped),
        )
        return PapersAssignResult(updated=updated, skipped_started=skipped)

    def add_students_bulk(
        self,
        test_id: str,
        names: Iterable[str],
        papers: Iterable[str] | None = None,
        birth_dates: Mapping[str, date] | None = None,
    ) -> BulkAddResult:
        """Add many roster names at once (the "one name per line" box).

        Names are stripped; blank lines are ignored. A name already on this test's
        roster — or repeated within the request — is skipped (exact display-name
        match) and reported back, never silently dropped or doubled.
        `birth_dates` (IQ tests): {name: birth date} for the names being added."""
        test = self.get_test(test_id)
        chosen = self._normalize_papers(test, papers)
        births = {k.strip(): v for k, v in (birth_dates or {}).items()}
        for value in births.values():
            _check_birth_date(value)
        existing = {e.display_name for e in self.roster.list_students(test_id)}
        added: list[RosterEntry] = []
        skipped: list[str] = []
        for raw in names:
            name = raw.strip()
            if not name:
                continue
            if name in existing:
                skipped.append(name)
                continue
            existing.add(name)
            entry = self.roster.add_student(test_id, name, chosen)
            if name in births:
                self.roster.set_birth_date(entry.id, births[name])
                entry = entry.model_copy(update={"birth_date": births[name]})
            added.append(entry)
        log.info(
            "admin add_students_bulk test=%s added=%d skipped=%d papers=%s",
            test_id,
            len(added),
            len(skipped),
            chosen or "all",
        )
        return BulkAddResult(added=added, skipped=skipped)

    def set_birth_date(self, roster_entry_id: str, birth_date: date | None) -> RosterStatus:
        """Set / clear a student's teacher-only birth date (IQ norms by age). Any
        time — the IQ report is recomputed on every read, so a date set after the
        attempt still applies (age = at the attempt start). The date is never logged."""
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise AdminError("Ученик не найден в списке (возможно, уже удалён).")
        _check_birth_date(birth_date)
        self.roster.set_birth_date(roster_entry_id, birth_date)
        entry = entry.model_copy(update={"birth_date": birth_date})
        attempt = self.attempts.get_attempt(entry.attempt_id) if entry.attempt_id else None
        log.info(
            "admin set_birth_date entry=%s set=%s", roster_entry_id, birth_date is not None
        )
        return _roster_row(entry, attempt, self.content.get_test(entry.test_id))

    def _iq_report(self, grading: GradingResult) -> IQReport | None:
        """The IQ report of a merged grade (overrides included), recomputed on every
        read; a failure never breaks the results page (WARNING + None)."""
        try:
            return self.grading.iq_report(grading)
        except Exception as exc:  # norm data / attempt gone: the page still renders
            log.warning("admin iq_report attempt=%s failed: %s", grading.attempt_id, exc)
            return None

    def _attempts_by_id(self, attempt_ids: Iterable[str | None]) -> dict[str, Attempt]:
        """Load many attempts in ONE query (the polled views must not N+1)."""
        ids = [a for a in attempt_ids if a]
        if not ids:
            return {}
        rows = self.attempts.session.scalars(
            select(pm.AttemptRow).where(pm.AttemptRow.id.in_(ids))
        ).all()
        return {
            r.id: Attempt(
                id=r.id,
                test_id=r.test_id,
                roster_entry_id=r.roster_entry_id,
                status=r.status,  # type: ignore[arg-type]
                seed=r.seed,
                started_at=r.started_at,
                submitted_at=r.submitted_at,
                deadline=r.deadline,
                audio_progress_seconds=r.audio_progress_seconds,
                papers=r.papers,  # type: ignore[arg-type]
            )
            for r in rows
        }

    def roster_status(self, test_id: str) -> list[RosterStatus]:
        """Live roster: who is assigned, who is in-progress / submitted."""
        test = self.content.get_test(test_id)
        entries = self.roster.list_students(test_id)
        attempts = self._attempts_by_id(e.attempt_id for e in entries)
        statuses = []
        for e in entries:
            attempt = attempts.get(e.attempt_id) if e.attempt_id else None
            statuses.append(_roster_row(e, attempt, test))
        log.info("admin roster_status test=%s -> %d", test_id, len(statuses))
        return statuses

    def reset_attempt(self, roster_entry_id: str) -> RosterStatus:
        """Discard a student's attempt so they can sit the test again.

        Wipes the attempt + its answers/events and clears the roster back-pointer,
        returning the entry to `not_started`. Used for a genuine retake (e.g. a
        technical failure) — it is a destructive teacher action, logged loudly.
        """
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise AdminError("Ученик не найден в списке (возможно, уже удалён).")
        if entry.attempt_id:
            self.attempts.delete_attempt(entry.attempt_id)
        entry.attempt_id = None
        entry.status = "not_started"
        self.attempts.update_roster_entry(entry)
        log.info("admin reset_attempt entry=%s (student may retake)", roster_entry_id)
        return _roster_row(entry, None, self.content.get_test(entry.test_id))

    def reopen_attempt(
        self,
        roster_entry_id: str,
        *,
        extend_minutes: int | None = None,
        now: datetime | None = None,
    ) -> RosterStatus:
        """Let a student back in WITHOUT losing progress (non-destructive).

        Flips a submitted/expired attempt back to `in_progress`, keeping every
        answer. By default the **original deadline** is kept — the clock keeps
        running (no extra time), so past the deadline the attempt re-expires at
        once. With `extend_minutes=N` the teacher deliberately grants time: the
        deadline becomes `max(old deadline, now + N)` for this one attempt (never
        shortened). That is an explicit teacher override, not the student timer
        pausing (golden rule #3 forbids pausing on a hidden tab; it does not forbid
        a teacher granting time), and it is logged at INFO. The teacher's deadline
        also wins over the exam window's `hard_close` (which delivery only applies
        when it fixes the deadline at start): an explicit, logged override.

        Only a finished attempt can be reopened: submitted, expired, or still
        `in_progress` in the DB but already past its deadline (the lazy expiry has
        not run yet — the roster shows it as "истёк"). An attempt the student is
        still writing inside their time is refused (409), so nothing is logged as
        a "reopen" (or silently extended) for a student who never handed in.
        """
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise AdminError("Ученик не найден в списке (возможно, уже удалён).")
        if entry.attempt_id is None:
            raise AdminStateError("Ученик ещё не начинал тест — переоткрывать нечего.")
        attempt = self.attempts.get_attempt(entry.attempt_id)
        if attempt is None:
            raise AdminError("Работа ученика не найдена.")
        now = now or datetime.now(UTC)
        overdue = (
            attempt.status == "in_progress"
            and attempt.deadline is not None
            and attempt.deadline <= now
        )
        if attempt.status not in ("submitted", "expired") and not overdue:
            log.warning(
                "admin reopen_attempt REFUSED entry=%s attempt=%s: status=%s (not finished)",
                roster_entry_id,
                attempt.id,
                attempt.status,
            )
            raise AdminStateError(
                "Ученик ещё пишет тест — переоткрывать нечего. Переоткрыть можно "
                "только сданную работу или работу, у которой вышло время."
            )
        update: dict = {"status": "in_progress", "submitted_at": None}
        if extend_minutes is not None:
            if extend_minutes <= 0:
                raise AdminValidationError("Продление должно быть больше 0 минут.")
            new_deadline = now + timedelta(minutes=extend_minutes)
            if attempt.deadline is not None and attempt.deadline > new_deadline:
                new_deadline = attempt.deadline
            update["deadline"] = new_deadline
        # Written through the existing attempt update path (status/timestamps/deadline).
        self.attempts.update_attempt(attempt.model_copy(update=update))
        entry.status = "in_progress"
        self.attempts.update_roster_entry(entry)
        if extend_minutes is not None:
            log.info(
                "admin reopen_attempt entry=%s attempt=%s TEACHER EXTENDED deadline "
                "+%d min: %s -> %s (progress kept)",
                roster_entry_id,
                attempt.id,
                extend_minutes,
                attempt.deadline.isoformat() if attempt.deadline else None,
                update["deadline"].isoformat(),
            )
        else:
            log.info(
                "admin reopen_attempt entry=%s (progress kept, clock running)", roster_entry_id
            )
        return _roster_row(
            entry,
            attempt,
            self.content.get_test(entry.test_id),
            deadline=update.get("deadline", attempt.deadline),
        )

    def regenerate_pin(self, roster_entry_id: str) -> RosterStatus:
        """Give one student a new classroom PIN ("Сгенерировать новый PIN").

        Affects only FUTURE starts: a session token the student already holds (an
        open exam tab) keeps working; to throw a student out, the teacher resets
        the attempt. The PIN is returned to the teacher, never logged.
        """
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise AdminError("Ученик не найден в списке (возможно, уже удалён).")
        pin = self.roster.regenerate_pin(roster_entry_id)
        attempt = self.attempts.get_attempt(entry.attempt_id) if entry.attempt_id else None
        log.info("admin regenerate_pin entry=%s test=%s", roster_entry_id, entry.test_id)
        return _roster_row(entry, attempt, self.content.get_test(entry.test_id), pin=pin)

    def remove_student(self, roster_entry_id: str) -> None:
        """Remove a roster entry (and its attempt, if any) from a test."""
        if not self.attempts.delete_roster_entry(roster_entry_id):
            raise AdminError("Ученик не найден в списке (возможно, уже удалён).")
        log.info("admin remove_student entry=%s", roster_entry_id)

    # -- manual grading (teacher marks writing / overrides a gap ✓/✗) -------- #
    def _apply_manual_grades(
        self, attempt_id, grading, manual: list[ManualGrade] | None = None
    ) -> tuple[GradingResult, set[str]]:
        """Overlay the teacher's stored marks onto an auto `GradingResult`.

        Each manual mark is applied via the pure `GradingService.apply_override`
        (grading stays auto-only per its boundary), recomputing the totals. A stale
        mark (item removed from the test) is skipped. Returns the merged result and
        the set of item ids a human graded. `manual` may be pre-fetched (the
        per-test views load every attempt's marks in one query)."""
        if manual is None:
            manual = self.attempts.list_manual_grades(attempt_id)
        ids: set[str] = set()
        for mg in manual:
            try:
                grading = self.grading.apply_override(grading, mg.item_id, awarded=mg.awarded)
                ids.add(mg.item_id)
            except GradingError:
                log.warning("stale manual grade attempt=%s item=%s skipped", attempt_id, mg.item_id)
        return grading, ids

    def set_manual_grade(self, attempt_id: str, item_id: str, awarded: float) -> AttemptResult:
        """Persist a teacher's mark for one item, then return the merged detail.

        Validates the score against the item's `max_points` (its authored
        `points`, else 5 for writing, 1 for an objective gap ✓/✗). A choose-one
        writing task (B1 Writing Part 2) is out of its group's maximum even while
        it is the uncounted one: marking it makes it the
        counted task (`GradingService.apply_override` swaps the pair). This is the
        human teacher's judgement — independent of any integrity signal (golden
        rule #2)."""
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise AdminError("Работа ученика не найдена.")
        if attempt.status not in ("submitted", "expired"):
            raise AdminStateError(
                "Ученик ещё пишет тест — оценку можно поставить только после сдачи."
            )
        whole = self.content.get_test(attempt.test_id)
        if whole is not None and attempt.papers is not None:
            in_whole = any(i.id == item_id for sec in whole.sections for i in sec.items)
            served = served_test(whole, attempt.papers)
            in_served = any(i.id == item_id for sec in served.sections for i in sec.items)
            if in_whole and not in_served:
                log.warning(
                    "admin set_manual_grade REFUSED attempt=%s item=%s: part not served (%s)",
                    attempt_id,
                    item_id,
                    attempt.papers,
                )
                raise AdminValidationError(
                    f"Задание {item_id!r} не входило в выданные ученику части "
                    f"({papers_label(served_papers(whole, attempt.papers))}) — "
                    "оценку за него поставить нельзя."
                )
        max_points = self._item_max_points(attempt, item_id)
        if max_points is None:
            raise AdminError(f"Задание {item_id!r} не входит в эту работу.")
        if not 0.0 <= awarded <= max_points:
            log.warning(
                "admin set_manual_grade attempt=%s item=%s awarded %s out of range [0, %s]",
                attempt_id,
                item_id,
                awarded,
                max_points,
            )
            raise AdminValidationError(
                f"Оценка {awarded:g} вне допустимого диапазона 0–{max_points:g}."
            )
        self.attempts.upsert_manual_grade(
            ManualGrade(
                attempt_id=attempt_id,
                item_id=item_id,
                awarded=awarded,
                graded_at=datetime.now(UTC),
            )
        )
        log.info(
            "admin set_manual_grade attempt=%s item=%s -> %.2f/%.2f",
            attempt_id,
            item_id,
            awarded,
            max_points,
        )
        return self.attempt_result(attempt_id)

    def _item_max_points(self, attempt: Attempt, item_id: str) -> float | None:
        """`max_points` for one item of an attempt WITHOUT a grading pass.

        The frozen report's grade wins (it holds exactly what was scored, incl. a
        writing grader's scale) — except the uncounted task of a choose-one group
        (stored with `max_points=0`): it is out of ITS group's full maximum (the
        counted sibling's), since marking it makes it the counted one
        (`_choose_one_max`). Without a report,
        the item definition decides (`app.grading.item_max_points`): the item's
        authored `points`, else objective 1 / writing + colouring 5 — the same
        numbers the grader uses. A blank optional writing task (the B1
        "choose one of two" the student skipped) is not graded, so it can't be
        marked either. None = not part of the attempt.
        """
        report = self.attempts.get_report(attempt.id)
        if report is not None:
            whole = self.content.get_test(attempt.test_id)
            served = served_test(whole, attempt.papers) if whole is not None else None
            served_ids = (
                {i.id for sec in served.sections for i in sec.items} if served else set()
            )
            if served is not None and item_id not in served_ids:
                return None  # an unserved part's item is not part of this attempt
            # Choose-one groups of the SERVED sections only (cached per whole test).
            groups = [
                g
                for g in self.grading.choose_one_groups(attempt.test_id)
                if set(g) <= served_ids
            ]
            return _choose_one_max(report.grading, item_id, groups, served)
        test = self.content.get_test(attempt.test_id)
        if test is None:  # pragma: no cover - attempt implies a test
            return None
        test = served_test(test, attempt.papers)  # an unserved part's item: not markable
        item = next((i for sec in test.sections for i in sec.items if i.id == item_id), None)
        if item is None:
            return None
        if isinstance(item, OpenWritingItem):
            if item.optional:
                answer = next(
                    (a for a in self.attempts.get_answers(attempt.id) if a.item_id == item_id),
                    None,
                )
                if answer is None or not (answer.response or "").strip():
                    return None
        return item_max_points(item, test.level)

    # -- results (score + advisory verdict + replay) ------------------------ #
    def _base_grading(self, attempt_id: str, report: AttemptReport | None):
        """The auto-grade for a read, plus the per-skill report and finalize status.

        Reads the frozen `AttemptReport` written once at submit (no LLM on load). If
        the attempt isn't finalized yet, falls back to a DETERMINISTIC-only grade
        (objective items scored, writing -> needs-review) and a "pending" status, so
        the page still renders and the teacher can trigger a re-grade.
        """
        if report is not None:
            return report.grading, report.skills, report.summary, report.status, report.error
        return self.grading.grade(attempt_id), [], "", "pending", None

    def _attempt_or_404(self, attempt_id: str) -> Attempt:
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise AdminError("Работа ученика не найдена.")
        return attempt

    def attempt_result(self, attempt_id: str) -> AttemptResult:
        """Full results detail for one attempt (rule #6: replay next to verdict).

        Grading and integrity/analysis are computed on independent paths and only
        bundled here (rule #2) — neither influences the other. The auto-grade is the
        frozen one from submit; manual marks overlay on top.
        """
        attempt = self._attempt_or_404(attempt_id)

        report = self.attempts.get_report(attempt_id)
        base, skills, summary, report_status, report_error = self._base_grading(attempt_id, report)
        if attempt.status == "in_progress" and report_status in ("complete", "failed"):
            report_status = REOPENED  # stale until the student hands in again
        grading, manual_ids = self._apply_manual_grades(attempt_id, base)
        events = self.events.list_events(attempt_id)
        ctx = self._review_context(attempt)
        profile, verdict, verdict_source = self._integrity_view(attempt_id, report, ctx, base)
        responses = self._build_responses(ctx, grading, events, manual_ids) if ctx else []
        timeline = self._build_timeline(ctx, events) if ctx else []
        whole = self.content.get_test(attempt.test_id)
        partial = partial_label(whole, attempt.papers) if whole is not None else None
        if partial and attempt.started_at is not None:
            # Metadata only (rule #6): which parts this attempt served.
            timeline.insert(
                0,
                TimelineEntry(
                    at=attempt.started_at,
                    type="papers",
                    item_id=None,
                    item_no=None,
                    label=(
                        "Выданы только части: "
                        f"{papers_label(served_papers(whole, attempt.papers))} "
                        f"({served_minutes(whole, attempt.papers)} мин)"
                    ),
                    integrity=False,
                ),
            )
        devices = _build_devices(events)
        skill_totals: list[SkillTotal] = []
        skills_avg = None
        cambridge = None
        overall_note = None
        if ctx is not None and ctx.test.level in FIVE_SKILL_LEVELS:
            per_skill = _per_skill(grading, _skill_by_item(ctx.test))
            skill_totals = [
                SkillTotal(
                    skill=s,
                    score=per_skill[s][0],
                    max_score=per_skill[s][1],
                    percent=(
                        round(100.0 * per_skill[s][0] / per_skill[s][1], 1)
                        if per_skill[s][1]
                        else None
                    ),
                )
                for s in FIVE_SKILL_SKILLS
                if s in per_skill
            ]
            skills_avg = _skills_average(per_skill)
            cambridge = _cambridge_scale(ctx.test.level, per_skill)
            if partial:  # per-skill values only — no overall over a subset
                skills_avg = None
                overall_note = OVERALL_PARTIAL_NOTE
                if cambridge is not None:
                    cambridge = cambridge.model_copy(
                        update={"overall": None, "overall_cefr": None}
                    )

        entry = (
            self.attempts.get_roster_entry(attempt.roster_entry_id)
            if attempt.roster_entry_id
            else None
        )
        log.info(
            "admin result attempt=%s score=%.2f/%.2f suspicion=%s verdict=%s events=%d",
            attempt_id,
            grading.score,
            grading.max_score,
            f"{verdict.suspicion_score:.3f}" if verdict is not None else "-",
            verdict_source,
            len(events),
        )
        return AttemptResult(
            attempt_id=attempt_id,
            roster_entry_id=attempt.roster_entry_id,
            display_name=entry.display_name if entry else None,
            grading=grading,
            responses=responses,
            devices=devices,
            timeline=timeline,
            profile=profile,
            verdict=verdict,
            verdict_source=verdict_source,
            verdict_note=VERDICT_UNAVAILABLE_NOTE if verdict is None else None,
            event_count=len(events),
            events=events,
            skills=skills,
            summary=summary,
            report_status=report_status,
            report_error=report_error,
            skill_totals=skill_totals,
            skills_average_percent=skills_avg,
            cambridge_scale=cambridge,
            papers=attempt.papers if partial else None,
            papers_label=partial,
            overall_note=overall_note,
            iq=self._iq_report(grading),
        )

    def _integrity_view(
        self,
        attempt_id: str,
        report: AttemptReport | None,
        ctx: _ReviewContext | None,
        grading: GradingResult,
    ) -> tuple[IntegrityProfile | None, AnalysisVerdict | None, str]:
        """The advisory verdict + its profile for the detail view (rule #6).

        * Stored (`report.verdict`): exactly what finalize computed — the rule
          engine (+ the optional LLM second opinion on writing) — read back, never
          recomputed (`"stored"`). A crash-path report that stored a verdict
          without its profile gets a freshly extracted deterministic profile for
          display (no LLM); if even that fails the verdict is shown without it.
        * No stored verdict (still writing, finalize pending, a pre-`e5f6a7b8c9d0`
          report): the rule engine over the current events through the LLM-free
          read-path `AnalysisService` (`"live_fallback"`).
        * That live computation failed (a malformed stored event the extractor
          cannot digest, a DB hiccup): `(None, None, "unavailable")` + a WARNING —
          the page still renders (grade, answers, timeline, raw events) and shows
          "—" with a note instead of answering 500.
        Either way the verdict is bundled next to the grade, never fed into it.
        """

        def context() -> AnalysisContext | None:
            # Writing tasks + submitted texts, so the writing features cover the
            # test's open_writing items. Built only when something is recomputed.
            if ctx is None:
                return None
            answers = {a.item_id: a.response for a in self.attempts.get_answers(attempt_id)}
            return ResultsService.analysis_context(ctx.test, grading, answers)

        if report is not None and report.verdict is not None:
            profile = report.integrity
            if profile is None:
                # Crash-path report (verdict stored without its profile): re-extract
                # the deterministic profile for display. Integrity only — no LLM.
                try:
                    analysis_ctx = context()
                    profile = (
                        self.integrity.profile(
                            attempt_id,
                            answers={t.item_id: t.text for t in analysis_ctx.writing},
                            writing_items=[t.item_id for t in analysis_ctx.writing],
                        )
                        if analysis_ctx is not None
                        else self.integrity.profile(attempt_id)
                    )
                except Exception as exc:
                    log.warning(
                        "admin result attempt=%s cannot re-extract the integrity "
                        "profile (%s: %s) -> stored verdict shown without it",
                        attempt_id,
                        type(exc).__name__,
                        exc,
                    )
                    profile = None
            return profile, report.verdict, "stored"
        log.info(
            "admin result attempt=%s has no stored verdict -> live rule-engine "
            "fallback (no LLM on a read)",
            attempt_id,
        )
        try:
            profile, verdict = self.analysis.assess(attempt_id, context=context())
        except Exception as exc:
            # A read must never 500 because the advisory axis cannot be computed.
            log.warning(
                "admin result attempt=%s live integrity fallback failed (%s: %s) -> "
                "shown as not assessed",
                attempt_id,
                type(exc).__name__,
                exc,
            )
            return None, None, "unavailable"
        return profile, verdict, "live_fallback"

    def regrade(self, attempt_id: str) -> AttemptResult:
        """Re-run the one-shot finalize (grade + skill report + verdict) for one attempt.

        The explicit teacher action behind the dashboard's 'Re-grade' button: used
        for an attempt submitted before this feature existed, or to refresh a report
        whose feedback LLM previously failed (`status="failed"`). It also recomputes
        and re-stores the integrity profile + advisory verdict (the configured
        analyst, else the formula). This and `finalize_missing` are the ONLY admin
        paths that run an LLM; reads never do.
        """
        self._attempt_or_404(attempt_id)
        self.results.finalize(attempt_id)
        log.info("admin regrade attempt=%s (re-finalized)", attempt_id)
        return self.attempt_result(attempt_id)

    def finalize_missing(self, test_id: str) -> list[str]:
        """Finalize every finished attempt of a test with no report or a failed one.

        The dashboard's "Дооценить все" action: recovers finalizes lost to a
        restart / outage and retries every failed report (no age cutoff, unlike
        the startup sweep). Attempts whose finalize is in flight are skipped. Each
        attempt runs in its OWN transaction via the runner — the request session's
        read transaction is ended first so it holds nothing while the LLM runs.
        Returns the attempt ids processed.
        """
        self.get_test(test_id)
        self.attempts.session.commit()  # end the request's read txn before the slow loop
        log.info("admin finalize_missing test=%s started", test_id)
        ids = self._finalize_missing_runner(test_id)
        log.info("admin finalize_missing test=%s -> %d attempt(s)", test_id, len(ids))
        return ids

    def _review_context(self, attempt) -> _ReviewContext | None:
        """Authoring test + per-attempt option layout + question lookups.

        Built once per `attempt_result` and shared by the per-question review and
        the human timeline. The layout de-shuffles `single_choice` exactly as
        delivery's default builder did (mirrors `app/delivery/service.py:_default_bank`),
        so a displayed index maps back to the option the student actually saw.
        """
        test = self.content.get_test(attempt.test_id)
        if test is None:  # pragma: no cover - attempt implies a test
            return None
        # Only the served papers (a partial assignment): the review rows, question
        # numbers and the de-shuffle layout mirror exactly what delivery served.
        test = served_test(test, attempt.papers)
        bank = TestBank(
            test_id=test.id,
            pools=[SectionPool(key=s.id, sections=[s], pick=1) for s in test.sections],
        )
        layout = build_attempt_layout(bank, attempt.seed)

        item_no: dict[str, str] = {}
        item_by_id: dict[str, tuple] = {}
        pool_by_item: dict[str, dict] = {}
        running = 0
        for section in test.sections:
            pool = (
                {p.key: p.text for p in section.stimulus.options}
                if isinstance(section.stimulus, MatchingPoolStimulus)
                else {}
            )
            for item in section.items:
                running += 1
                m = re.search(r"q(\d+)$", item.id)
                item_no[item.id] = m.group(1) if m else str(running)
                item_by_id[item.id] = (section, item)
                pool_by_item[item.id] = pool
        return _ReviewContext(test, layout, item_no, item_by_id, pool_by_item)

    def _build_responses(
        self, ctx: _ReviewContext, grading, events, manual_ids: set[str]
    ) -> list[ItemResponse]:
        """Join the authoring key, the stored answers, grades and the replay into
        one per-question review (answer vs key, ✓/✗, full writing, change history).

        Read-only and key-bearing on purpose: this is the teacher surface, so it
        carries `correct` (golden rule #1 forbids that only on the student runner),
        and no integrity signal is consulted (golden rule #2)."""
        answers = {a.item_id: a.response for a in self.attempts.get_answers(grading.attempt_id)}
        grades = {g.item_id: g for g in grading.items}
        history: dict[str, list] = {}
        for ev in events:
            if ev.type == "answer_change" and ev.item_id:
                history.setdefault(ev.item_id, []).append(ev)

        rows: list[ItemResponse] = []
        for section in ctx.test.sections:
            for item in section.items:
                pool_by_key = ctx.pool_by_item[item.id]
                grade = grades.get(item.id)
                resp = answers.get(item.id)
                is_writing = isinstance(item, OpenWritingItem)
                is_manual = item.id in manual_ids
                rows.append(
                    ItemResponse(
                        item_id=item.id,
                        item_no=ctx.item_no.get(item.id),
                        section_id=section.id,
                        section_title=section.title,
                        skill=section.score_skill,
                        item_type=item.item_type,
                        prompt=item.prompt,
                        bullet_points=item.bullet_points if is_writing else [],
                        word_min=item.word_min if is_writing else None,
                        student_answer=_render_student(item, resp, pool_by_key),
                        writing_text=resp if is_writing else None,
                        correct_answer=_render_correct(item, pool_by_key),
                        is_correct=(
                            # Objective items are ✓/✗ from points (gap overrides move
                            # them). Writing is *scored* (0..5), never pass/fail, so it
                            # has no ✓/✗ — its mark shows in the writing section.
                            # A mark_toggle row: marked as it should be (a distractor
                            # row is worth 0, so points alone can't tell).
                            None
                            if is_writing or grade is None
                            else (is_marked(resp) == item.correct)
                            if isinstance(item, MarkToggleItem)
                            else grade.awarded >= grade.max_points
                        ),
                        partial=(
                            grade is not None
                            and not is_writing
                            and 0 < grade.awarded < grade.max_points
                        ),
                        note=(
                            grade.feedback
                            if grade is not None
                            and isinstance(
                                item,
                                (
                                    KeyWordTransformationItem,
                                    DrawingItem,
                                    MarkToggleItem,
                                    GapFillItem,  # partial credit / "not in the key"
                                ),
                            )
                            else None
                        ),
                        awarded=grade.awarded if grade else 0.0,
                        # ungraded (e.g. unanswered optional task): the item's worth
                        max_points=(
                            grade.max_points if grade else item_max_points(item, ctx.test.level)
                        ),
                        needs_review=grade.needs_review if grade else False,
                        manual=is_manual,
                        history=[
                            ResponseChange(
                                at=ev.server_ts or ev.client_ts,
                                value=_render_change(item, ctx.layout, ev.payload, pool_by_key),
                            )
                            for ev in history.get(item.id, [])
                        ],
                        drawing=(
                            _drawing_response(item, resp)
                            if isinstance(item, DrawingItem)
                            else None
                        ),
                    )
                )
        return rows

    def _build_timeline(self, ctx: _ReviewContext, events) -> list[TimelineEntry]:
        """Decode the raw replay into a human, chronological 'what they did' list.

        Writing telemetry is rendered compactly: `writing_stats` flushes (and short
        keyboard bursts — a swiped / autocorrected word) of one field are folded
        into ONE line per focus session, closed by that field's focus/blur or the
        end of the stream; pastes, non-keyboard inserts, scripted input and
        fullscreen exits get their own highlighted line. The raw `events` stay
        complete next to it (rule #6)."""
        ordered = sorted(events, key=lambda e: e.server_ts or e.client_ts)
        out: list[TimelineEntry] = []
        folds: dict[str | None, _TypingFold] = {}

        def close(item_id: str | None) -> None:
            fold = folds.pop(item_id, None)
            if fold is not None:
                out.append(fold.entry())

        for ev in ordered:
            at = ev.server_ts or ev.client_ts
            no = ctx.item_no.get(ev.item_id) if ev.item_id else None
            if ev.type == "writing_stats" or _is_short_keyboard_burst(ev):
                fold = folds.get(ev.item_id)
                if fold is None:
                    fold = folds[ev.item_id] = _TypingFold(ev.item_id, no, at)
                if ev.type == "writing_stats":
                    fold.add_stats(ev, at)
                else:
                    fold.add_short_burst(at)
                continue
            if ev.type in ("field_focus", "field_blur"):
                close(ev.item_id)
            if ev.type in _WRITING_TIMELINE_TYPES:
                label, flagged = _writing_event_label(ev, no)
            else:
                item = ctx.item_by_id.get(ev.item_id, (None, None))[1] if ev.item_id else None
                pool = ctx.pool_by_item.get(ev.item_id, {}) if ev.item_id else {}
                label = _timeline_label(ev, no, item, pool, ctx.layout)
                flagged = ev.type in _INTEGRITY_EVENT_TYPES
            out.append(
                TimelineEntry(
                    at=at,
                    type=ev.type,
                    item_id=ev.item_id,
                    item_no=no,
                    label=label,
                    integrity=flagged,
                )
            )
        for item_id in list(folds):
            close(item_id)
        # A fold is stamped with its LAST flush, so re-sort (stable: ties keep order).
        out.sort(key=lambda e: e.at)
        return out

    def results_for_test(self, test_id: str) -> list[AttemptOverview]:
        """All started attempts for a test, ranked most-suspicious first.

        Cheap on purpose (this is polled live every few seconds): the score AND the
        advisory verdict come from the frozen per-attempt reports read in ONE query —
        it never re-runs the writing LLM, the analyst, or the integrity extractor
        (that work happens once at finalize). An attempt with no stored verdict
        (still writing, finalize pending, a pre-migration report) has suspicion None
        ("—") and sorts after every scored row. Ranking only reorders rows; it never
        changes a score (rule #2).
        """
        rows = self._result_rows(test_id)
        test = self.content.get_test(test_id)
        overviews: list[AttemptOverview] = []
        for row in rows:
            assert row.grading is not None  # built with the deterministic fallback
            verdict = row.report.verdict if row.report is not None else None
            partial = partial_label(test, row.attempt.papers) if test is not None else None
            overviews.append(
                AttemptOverview(
                    attempt_id=row.attempt.id,
                    roster_entry_id=row.entry.id,
                    display_name=row.entry.display_name,
                    score=row.grading.score,
                    max_score=row.grading.max_score,
                    needs_review=row.grading.needs_review,
                    suspicion_score=verdict.suspicion_score if verdict else None,
                    confidence=verdict.confidence if verdict else None,
                    ai_assist_max=(
                        max((w.ai_assist_likelihood for w in verdict.writing), default=None)
                        if verdict
                        else None
                    ),
                    verdict_model_id=verdict.model_id if verdict else None,
                    top_rules=_top_rule_titles(verdict) if verdict else [],
                    verdict_legacy=bool(verdict and verdict.rules_version is None),
                    event_count=row.event_count,
                    attempt_status=row.attempt.status,
                    report_status=row.status,
                    report_error=row.report.error if row.report is not None else None,
                    submitted_at=_finished_at(row.attempt),
                    papers=row.attempt.papers if partial else None,
                    papers_label=partial,
                )
            )
        # Most suspicious first by the STORED score; rows without one go last
        # (stable sort: ties keep the roster order).
        overviews.sort(key=lambda o: (o.suspicion_score is None, -(o.suspicion_score or 0.0)))
        log.info(
            "admin results_for_test test=%s -> %d attempt(s), %d with a stored verdict",
            test_id,
            len(overviews),
            sum(o.suspicion_score is not None for o in overviews),
        )
        return overviews

    def _result_rows(self, test_id: str, *, fallback_grade: bool = True) -> list[_ResultRow]:
        """Every started attempt of a test with its merged grade — batched reads.

        Roster, attempts, reports, manual marks and event counts are each ONE query
        for the whole test (this backs the live-polled results table), never one per
        student. The grade is the frozen report's (+ the teacher's manual marks);
        with no report yet it is the LLM-free deterministic fallback when
        `fallback_grade`, else None (the CSV export never triggers grading).
        """
        self.get_test(test_id)
        entries = [e for e in self.roster.list_students(test_id) if e.attempt_id]
        attempts = self._attempts_by_id(e.attempt_id for e in entries)
        reports = self.attempts.get_reports_for_test(test_id)
        ids = list(attempts)
        session = self.attempts.session
        manual: dict[str, list[ManualGrade]] = {}
        counts: dict[str, int] = {}
        if ids:
            for r in session.scalars(
                select(pm.ManualGradeRow).where(pm.ManualGradeRow.attempt_id.in_(ids))
            ).all():
                manual.setdefault(r.attempt_id, []).append(
                    ManualGrade(
                        attempt_id=r.attempt_id,
                        item_id=r.item_id,
                        awarded=r.awarded,
                        graded_at=r.graded_at,
                    )
                )
            counts = {
                aid: int(n)
                for aid, n in session.execute(
                    select(pm.IntegrityEventRow.attempt_id, func.count())
                    .where(pm.IntegrityEventRow.attempt_id.in_(ids))
                    .group_by(pm.IntegrityEventRow.attempt_id)
                ).all()
            }
        now = datetime.now(UTC)
        rows: list[_ResultRow] = []
        for entry in entries:
            attempt = attempts.get(entry.attempt_id)  # type: ignore[arg-type]
            if attempt is None:  # pragma: no cover - dangling roster pointer
                continue
            report = reports.get(attempt.id)
            if report is not None:
                base: GradingResult | None = report.grading
            elif fallback_grade:
                base = self.grading.grade(attempt.id)
            else:
                base = None
            grading = (
                self._apply_manual_grades(attempt.id, base, manual.get(attempt.id, []))[0]
                if base is not None
                else None
            )
            rows.append(
                _ResultRow(
                    entry=entry,
                    attempt=attempt,
                    report=report,
                    grading=grading,
                    status=_report_status(report, attempt, now),
                    event_count=counts.get(attempt.id, 0),
                )
            )
        return rows

    # -- export + printable report (no grading / LLM calls) ------------------ #
    def results_csv(self, test_id: str) -> str:
        """The test's results as CSV text (UTF-8 BOM added by the caller's encoding).

        Built only from stored data — frozen reports + manual marks + event counts —
        never a grading pass or an LLM call. An attempt without a report (still
        grading / lost / still writing / re-opened) gets empty score cells. The
        suspicion column (teacher-only export) is the verdict STORED at finalize —
        the same number the results table shows, empty without one; it is
        informational only and never moves a score (golden rule #2).
        """
        test = self.get_test(test_id)
        skill_of = _skill_by_item(test)
        skills = _csv_skills(test)
        five = test.level in FIVE_SKILL_LEVELS
        rows = self._result_rows(test_id, fallback_grade=False)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        header = ["student", "status", "submitted_at", "score", "max", "percent"]
        for skill in skills:
            header += [f"{skill}_score", f"{skill}_max"]
        header += ["report_status", "suspicion_score", "event_count"]
        if five:  # B2-C2: mean of the per-skill percentages (no Speaking)
            header += ["skills_avg_percent"]
        # Last, so the historical column positions stay put: "все части" or
        # «Частично: Listening» — score / max / skills cover only those parts.
        header += ["parts"]
        iq = test.norm_scoring is not None  # IQ family: the norm columns after it
        if iq:
            header += _iq_csv_header(test)
        writer.writerow(header)
        for row in rows:
            graded = row.grading is not None and row.status != REOPENED
            finished = _finished_at(row.attempt)
            line: list[object] = [
                _csv_text(row.entry.display_name),  # student-typed: never a formula
                _csv_text(row.attempt.status),
                finished.isoformat() if finished else "",
            ]
            if graded and row.grading is not None:
                g = row.grading
                pct = round(100.0 * g.score / g.max_score, 1) if g.max_score else ""
                line += [_num(g.score), _num(g.max_score), pct]
                per_skill = _per_skill(g, skill_of)
                for skill in skills:
                    got = per_skill.get(skill)
                    line += [_num(got[0]), _num(got[1])] if got else ["", ""]
            else:
                per_skill = {}
                line += ["", "", ""] + ["", ""] * len(skills)
            verdict = row.report.verdict if row.report is not None else None
            line += [
                _csv_text(row.status),
                f"{verdict.suspicion_score:.2f}" if verdict is not None else "",
                row.event_count,
            ]
            if five:  # a partial attempt has no overall (OVERALL_PARTIAL_NOTE)
                partial_row = partial_label(test, row.attempt.papers) is not None
                avg = _skills_average(per_skill) if per_skill and not partial_row else None
                line += [avg if avg is not None else ""]
            line += [_csv_text(partial_label(test, row.attempt.papers) or "все части")]
            if iq:
                report = (
                    build_iq_report(
                        test,
                        row.grading,
                        papers=row.attempt.papers,
                        birth_date=row.entry.birth_date,
                        when=row.attempt.started_at or datetime.now(UTC),
                    )
                    if graded and row.grading is not None
                    else None
                )
                line += _iq_csv_cells(test, report)
            writer.writerow(line)
        log.info("admin results_csv test=%s -> %d row(s)", test_id, len(rows))
        return buf.getvalue()

    def student_report(self, attempt_id: str) -> StudentReport:
        """The printable student/parent report for one attempt.

        Scores (frozen report + the teacher's marks), the stored per-skill
        strengths/weaknesses and the writing tasks with their marks. NO integrity
        data of any kind, and no grading/LLM call: an attempt without a report falls
        back to the deterministic LLM-free grade (writing then shows "not graded").
        """
        attempt = self._attempt_or_404(attempt_id)
        test = self.get_test(attempt.test_id)
        base, skills, summary, report_status, _ = self._base_grading(
            attempt_id, self.attempts.get_report(attempt_id)
        )
        if attempt.status == "in_progress" and report_status in ("complete", "failed"):
            report_status = REOPENED
        grading, manual_ids = self._apply_manual_grades(attempt_id, base)
        skill_of = _skill_by_item(test)
        per_skill = _per_skill(grading, skill_of)
        feedback = {s.skill: s for s in skills}
        skill_rows = [
            SkillScore(
                skill=skill,
                score=per_skill[skill][0],
                max_score=per_skill[skill][1],
                strengths=feedback[skill].strengths if skill in feedback else "",
                weaknesses=feedback[skill].weaknesses if skill in feedback else "",
            )
            for skill in FIVE_SKILL_SKILLS
            if skill in per_skill
        ]
        answers = {a.item_id: a.response for a in self.attempts.get_answers(attempt_id)}
        grades = {g.item_id: g for g in grading.items}
        partial = partial_label(test, attempt.papers)
        writing: list[WritingTaskReport] = []
        for section in served_test(test, attempt.papers).sections:
            for item in section.items:
                if not isinstance(item, OpenWritingItem):
                    continue
                grade = grades.get(item.id)
                if grade is None:  # the unchosen optional task: not graded, not printed
                    continue
                text = answers.get(item.id) or None
                writing.append(
                    WritingTaskReport(
                        item_id=item.id,
                        section_title=section.title,
                        prompt=item.prompt,
                        bullet_points=item.bullet_points,
                        word_min=item.word_min,
                        text=text,
                        word_count=len(text.split()) if text else 0,
                        awarded=grade.awarded,
                        max_points=grade.max_points,
                        graded=item.id in manual_ids or not grade.needs_review,
                        manual=item.id in manual_ids,
                        feedback=grade.feedback,
                    )
                )
        entry = (
            self.attempts.get_roster_entry(attempt.roster_entry_id)
            if attempt.roster_entry_id
            else None
        )
        log.info(
            "admin student_report attempt=%s score=%.2f/%.2f skills=%d writing=%d",
            attempt_id,
            grading.score,
            grading.max_score,
            len(skill_rows),
            len(writing),
        )
        return StudentReport(
            attempt_id=attempt_id,
            display_name=entry.display_name if entry else None,
            test_title=test.title,
            level=test.level,
            attempt_status=attempt.status,
            finished_at=_finished_at(attempt),
            score=grading.score,
            max_score=grading.max_score,
            report_status=report_status,
            skills=skill_rows,
            summary=summary,
            writing=writing,
            skills_average_percent=(
                _skills_average(per_skill)
                if test.level in FIVE_SKILL_LEVELS and not partial
                else None
            ),
            papers_label=partial,
            overall_note=(
                OVERALL_PARTIAL_NOTE if partial and test.level in FIVE_SKILL_LEVELS else None
            ),
            iq=self._iq_report(grading),
        )


def build_admin_service(
    session: Session,
    storage: StorageBackend,
    *,
    llm_grader: LLMGrader | None = None,
    analysis_llm: AnalysisLLM | None | _FromSettings = FROM_SETTINGS,
    feedback_llm: FeedbackLLM | None = None,
) -> AdminService:
    """Construct an `AdminService` and all the engines it composes from a session.

    Kept as a factory so the API layer wires one line and tests can inject mock
    LLM seams (a writing grader / an analyst / a feedback analyst).

    Two grading paths on purpose: the dashboard's read grader is LLM-FREE (it only
    fills the deterministic fallback for an un-finalized attempt — reads never call
    the writing LLM), while the `ResultsService` behind `regrade` carries the
    LLM-enabled writing grader + feedback analyst for the one-shot finalize.

    The same split for the integrity verdict: `analysis_llm` goes ONLY to that
    regrade `ResultsService` (default: the settings-configured analyst, as the
    submit-time finalize uses); the read-path `AnalysisService` is always built
    with `llm=None`, so no dashboard read can ever reach an LLM.
    """
    content_repo = ContentRepository(session)
    attempt_repo = AttemptRepository(session)
    event_repo = EventRepository(session)
    integrity = IntegrityService(event_repo)
    results = ResultsService(
        content=content_repo,
        attempts=attempt_repo,
        grading=GradingService(content_repo, attempt_repo, llm_grader=llm_grader),
        feedback=FeedbackService(llm=feedback_llm),
        analysis_llm=analysis_llm,
    )
    return AdminService(
        content=ContentService(content_repo, storage),
        roster=RosterService(attempt_repo),
        grading=GradingService(content_repo, attempt_repo, llm_grader=None),
        integrity=integrity,
        analysis=AnalysisService(event_repo, integrity, llm=None),  # reads: never an LLM
        results=results,
        attempts=attempt_repo,
        events=event_repo,
    )
