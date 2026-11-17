"""Deterministic per-writing-item features (outside-help evidence), NO LLM.

Pure functions over the attempt's event stream + the submitted text: same inputs
-> identical `WritingFeatures`. No clock, no randomness, no I/O. This layer
computes numbers and threshold flags (documented in `app/integrity/CLAUDE.md`);
it never decides guilt — the advisory verdict is `app/analysis` (golden rule #6).

Inputs per writing item (all captured by `apps/web/recorder.js`, shapes only):

* `field_focus` / `field_blur` — the field gained/lost focus (+ its length);
* `writing_stats` — additive per-window typing aggregates keyed by the raw
  `InputEvent.inputType` (chars added, insert counts, max single insert), deleted
  chars, keystroke count, inter-key-interval histogram, active typing ms;
* `input_burst` — one input event that added >= 2 chars at once;
* `paste_attempt` — a BLOCKED paste/drop (length only);
* `answer_change` — the debounced text snapshots (length is what matters here);
* `visibility_*` / `window_blur|focus` / `fullscreen_exit` — overlap with the
  writing window.

The submitted text itself is attempt data (the canonical `Answer`), not grading:
its cheap statistics (word count, sentence length, type-token ratio, long-word
share) are level-mismatch evidence for the analyst only. Nothing here touches a
score (golden rule #2).

Two capture realities are handled here, deterministically:

* **Replays.** The recorder is at-least-once (unsent events are resent after a
  reload, a timed-out batch is retried), so the stream may hold the same event
  twice. `dedupe_events` drops exact replays — same `(type, client_ts, item_id,
  duration_ms, canonical payload)` — before anything is summed; otherwise a
  replayed `writing_stats` window would double the typed chars.
* **Gaps.** Telemetry can also be LOST (reload/crash before a flush, a dropped
  batch). Then the typed counts are short and `unexplained_chars` grows although
  the student typed everything. `telemetry_gap` records that this probably
  happened, and `text_appeared_without_typing` is then only raised on positively
  observed evidence (see `_flags`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from contracts import IntegrityEvent, WritingFeatures

# --------------------------------------------------------------------------- #
# Classification of raw inputTypes (see CLAUDE.md "Writing features").
# --------------------------------------------------------------------------- #
# Input types a physical/virtual keyboard produces one small insert at a time.
# (Mobile keyboards type through IME composition; swipe/autocorrect commit a word.)
KEYBOARD_INPUT_TYPES = frozenset(
    {"insertText", "insertCompositionText", "insertLineBreak", "insertParagraph"}
)
# Undo/redo restore text the student already produced: neither typed nor external.
HISTORY_INPUT_TYPES = frozenset({"historyUndo", "historyRedo"})
# A single keyboard-type insert larger than this is NOT typing (an extension's
# execCommand('insertText', <essay>), an autofill, voice dictation committing a
# whole phrase): it is reclassified as non-keyboard. A swipe/autocorrected word
# stays below it.
KEYBOARD_MAX_INSERT = 12

# --------------------------------------------------------------------------- #
# Flag thresholds (documented in app/integrity/CLAUDE.md — keep in sync).
# --------------------------------------------------------------------------- #
EXTERNAL_TEXT_MIN_CHARS = 40  # text_appeared_without_typing: absolute floor
EXTERNAL_TEXT_MIN_SHARE = 0.25  # ... and share of the final text
SNAPSHOT_MIN_GROWTH = 40  # snapshot route: growth of at least this many chars
SNAPSHOT_IMPLAUSIBLE_CPM = 1500  # ... at a rate no human types (chars / minute)
LARGE_PASTE_CHARS = 50  # large_paste_attempt
TYPING_IMPLAUSIBLE_CPM = 600  # typing_speed_implausible (sustained over ...)
TYPING_IMPLAUSIBLE_MIN_CHARS = 100  # ... at least this many typed chars
AWAY_WHILE_WRITING_MS = 30_000  # away_while_writing (hidden or blurred)
NO_TELEMETRY_MIN_CHARS = 40  # no_input_telemetry: text with no captured input at all
# telemetry_gap: answer snapshots show at least this many chars arriving at human
# typing speed that no captured input event accounts for.
TELEMETRY_GAP_MIN_CHARS = 20
# A snapshot-to-snapshot growth counts as "typed at human speed" when it is small
# (< SNAPSHOT_MIN_GROWTH, whatever the measured time — snapshots sharing an ingest
# batch have near-identical server timestamps) or not faster than this (chars/min).
GRADUAL_GROWTH_MAX_CPM = TYPING_IMPLAUSIBLE_CPM
# Events that mark a page (re)load / unload: inside the writing window they mean
# the recorder restarted mid-writing.
_RESTART_EVENT_TYPES = ("device_info", "pagehide")

# Inter-key-interval histogram upper edges (ms). MUST equal
# `app.telemetry.schema.IK_BUCKET_EDGES_MS` and recorder.js `IK_EDGES`
# (integrity does not import telemetry; a test asserts they match).
IK_BUCKET_EDGES_MS: tuple[int, ...] = (50, 100, 150, 200, 300, 500, 750, 1000, 2000, 10000)

LONG_WORD_LETTERS = 8

_WRITING_EVENT_TYPES = ("field_focus", "field_blur", "writing_stats", "input_burst")
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")


def _ts(event: IntegrityEvent) -> datetime:
    """Trusted timestamp: server time, falling back to client time if unstamped."""
    return event.server_ts or event.client_ts


def _ms(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() * 1000))


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _r(value: float) -> float:
    return round(value, 4)


# --------------------------------------------------------------------------- #
# Replay dedupe.
# --------------------------------------------------------------------------- #
def _event_key(event: IntegrityEvent) -> tuple:
    payload = json.dumps(
        event.payload or {}, sort_keys=True, separators=(",", ":"), default=str
    )
    return (event.type, event.client_ts, event.item_id, event.duration_ms, payload)


def dedupe_events(events: Sequence[IntegrityEvent]) -> list[IntegrityEvent]:
    """Drop exact replays, keeping the FIRST occurrence and the input order.

    Two events are the same capture when their `(type, client_ts, item_id,
    duration_ms, canonical payload)` are identical: `client_ts` has millisecond
    resolution and every additive payload (`writing_stats`, `input_burst`) carries
    the field length, so two genuinely distinct events never collide in practice.
    `server_ts` is deliberately NOT part of the key — a replay is ingested later.
    Pure and deterministic.
    """
    seen: set[tuple] = set()
    out: list[IntegrityEvent] = []
    for event in events:
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


# --------------------------------------------------------------------------- #
# Public entry.
# --------------------------------------------------------------------------- #
def writing_item_ids(chron: Sequence[IntegrityEvent]) -> list[str]:
    """open_writing items that carry writing-field telemetry, in first-seen order."""
    seen: dict[str, None] = {}
    for e in chron:
        if e.item_id is None or e.type not in _WRITING_EVENT_TYPES:
            continue
        if (e.payload or {}).get("field", "open_writing") == "open_writing":
            seen.setdefault(e.item_id, None)
    return list(seen)


def extract_writing_features(
    chron: Sequence[IntegrityEvent],
    *,
    answers: Mapping[str, str] | None = None,
    writing_items: Sequence[str] | None = None,
) -> list[WritingFeatures]:
    """One `WritingFeatures` per writing item (pure; `chron` is already ordered).

    `writing_items` (the test's open_writing ids, from the results layer) wins when
    given — then an answer that reached the server with NO captured input at all is
    still examined. Otherwise the items are derived from the writing telemetry.
    `answers` (canonical submitted text) wins over the last `answer_change` value.

    Exact replays are dropped first (`dedupe_events`, idempotent), so a re-sent
    batch never doubles a sum or a count.
    """
    chron = dedupe_events(chron)
    ids = list(writing_items) if writing_items is not None else writing_item_ids(chron)
    hidden = _paired_intervals(chron, "visibility_hidden", "visibility_visible")
    blurred = _paired_intervals(chron, "window_blur", "window_focus")
    return [
        _features_for(item_id, chron, answers or {}, hidden, blurred)
        for item_id in dict.fromkeys(ids)  # de-duplicated, order kept
    ]


# --------------------------------------------------------------------------- #
# Per-item reduction.
# --------------------------------------------------------------------------- #
def _features_for(
    item_id: str,
    chron: Sequence[IntegrityEvent],
    answers: Mapping[str, str],
    hidden: list[tuple[datetime, datetime, str | None]],
    blurred: list[tuple[datetime, datetime, str | None]],
) -> WritingFeatures:
    mine = [e for e in chron if e.item_id == item_id]
    focuses = [e for e in mine if e.type == "field_focus"]
    changes = [e for e in mine if e.type == "answer_change"]
    bursts = [e for e in mine if e.type == "input_burst"]
    stats = [e for e in mine if e.type == "writing_stats"]
    pastes = [e for e in mine if e.type == "paste_attempt"]

    text, final_length = _final_text(item_id, answers, changes)
    agg = _aggregate_stats(stats)
    has_stats = bool(stats)

    # -- keyboard vs non-keyboard chars ------------------------------------- #
    added: dict[str, int] = agg["added"]
    keyboard_added = sum(v for t, v in added.items() if t in KEYBOARD_INPUT_TYPES)
    history_added = sum(v for t, v in added.items() if t in HISTORY_INPUT_TYPES)
    other_added = sum(
        v
        for t, v in added.items()
        if t not in KEYBOARD_INPUT_TYPES and t not in HISTORY_INPUT_TYPES
    )
    bulk_keyboard = [
        _int(b.payload.get("length"))
        for b in bursts
        if b.payload.get("input_type") in KEYBOARD_INPUT_TYPES
        and _int(b.payload.get("length")) > KEYBOARD_MAX_INSERT
    ]
    # Bursts are a separate stream from the aggregates (and may be replayed), so
    # never move more than the aggregates actually counted as keyboard.
    bulk = min(sum(bulk_keyboard), keyboard_added)
    typed = keyboard_added - bulk
    non_keyboard = other_added + bulk
    deleted = agg["deleted_chars"]
    unexplained = (
        max(0, final_length - (typed + non_keyboard + history_added - deleted)) if has_stats else 0
    )
    external = non_keyboard + unexplained
    denom = typed + external
    typed_ratio = _r(typed / denom) if has_stats and denom > 0 else None
    revision_ratio = _r(deleted / max(1, typed + non_keyboard + history_added))

    max_insert: dict[str, int] = agg["max_insert"]
    max_non_kb = max(
        [v for t, v in max_insert.items() if t not in KEYBOARD_INPUT_TYPES | HISTORY_INPUT_TYPES]
        + bulk_keyboard
        + [
            _int(b.payload.get("length"))
            for b in bursts
            if b.payload.get("input_type") not in KEYBOARD_INPUT_TYPES | HISTORY_INPUT_TYPES
        ]
        + [0]
    )

    # -- timing ------------------------------------------------------------- #
    active_ms = agg["active_ms"]
    cpm = _r(typed / (active_ms / 60_000)) if active_ms >= 1000 else None
    ik_count = agg["ik_count"]
    ik_mean = _r(agg["ik_sum_ms"] / ik_count) if ik_count else None
    hist = agg["ik_hist"]
    p50 = _hist_percentile(hist, 0.5)
    p90 = _hist_percentile(hist, 0.9)

    growth, growth_ms = _max_snapshot_growth(mine)

    starts = [_ts(e) for e in focuses] or [_ts(e) for e in changes]
    window_start = min(starts) if starts else None
    output_ts = [_ts(e) for e in changes + bursts]
    window_end = max(output_ts) if output_ts else window_start
    first_output = min(output_ts) if output_ts else None
    first_focus = min(_ts(e) for e in focuses) if focuses else None
    time_to_first = (
        _ms(first_focus, first_output)
        if first_focus is not None and first_output is not None and first_output >= first_focus
        else None
    )
    span = _ms(window_start, window_end) if window_start and window_end else 0

    hidden_ms, n_hidden = _overlap(hidden, item_id, window_start, window_end)
    blur_ms, n_blur = _overlap(blurred, item_id, window_start, window_end)
    fs_exits = sum(
        1
        for e in mine
        if e.type == "fullscreen_exit"
        and window_start is not None
        and window_end is not None
        and window_start <= _ts(e) <= window_end
    )

    paste_lengths = [_int(p.payload.get("length")) for p in pastes]

    # -- telemetry completeness (a capture fact; see `_flags`) --------------- #
    accounted = typed + non_keyboard + history_added
    uncovered = max(0, _gradual_snapshot_growth(mine) - accounted)
    restarts = _restarts_inside(chron, window_start, window_end)
    telemetry_gap = uncovered >= TELEMETRY_GAP_MIN_CHARS or restarts > 0

    feats = WritingFeatures(
        item_id=item_id,
        has_keystroke_telemetry=has_stats,
        final_length=final_length,
        typed_chars=typed,
        non_keyboard_chars=non_keyboard,
        unexplained_chars=unexplained,
        typed_ratio=typed_ratio,
        deleted_chars=deleted,
        revision_ratio=revision_ratio,
        keystrokes=agg["keystrokes"],
        untrusted_inputs=agg["untrusted_inputs"]
        + sum(1 for b in bursts if b.payload.get("trusted") is False),
        paste_attempts=len(pastes),
        max_paste_attempt_length=max(paste_lengths, default=0),
        max_non_keyboard_insert=max_non_kb,
        max_snapshot_growth=growth,
        max_snapshot_growth_ms=growth_ms,
        active_typing_ms=active_ms,
        chars_per_active_minute=cpm,
        inter_key_mean_ms=ik_mean,
        inter_key_p50_ms=p50,
        inter_key_p90_ms=p90,
        pauses_over_2s=agg["pauses_2s"],
        pauses_over_10s=agg["pauses_10s"],
        time_to_first_char_ms=time_to_first,
        writing_span_ms=span,
        hidden_ms_while_writing=hidden_ms,
        blur_ms_while_writing=blur_ms,
        away_count=max(n_hidden, n_blur),
        fullscreen_exits_while_writing=fs_exits,
        telemetry_gap=telemetry_gap,
        uncovered_snapshot_growth=uncovered,
        recorder_restarts_while_writing=restarts,
        **_text_stats(text),
    )
    return feats.model_copy(
        update={"flags": _flags(feats, has_changes=bool(changes) or bool(bursts))}
    )


def _final_text(
    item_id: str, answers: Mapping[str, str], changes: list[IntegrityEvent]
) -> tuple[str | None, int]:
    """(text or None if only a truncated snapshot exists, final length in chars)."""
    if item_id in answers:
        text = str(answers[item_id] or "")
        return text, len(text)
    if not changes:
        return None, 0
    last = changes[-1].payload or {}
    if last.get("truncated"):
        return None, _int(last.get("length"))
    value = last.get("value")
    text = "" if value is None else str(value)
    return text, len(text)


def _snapshot_len(event: IntegrityEvent) -> int:
    p = event.payload or {}
    if p.get("truncated"):
        return _int(p.get("length"))
    value = p.get("value")
    return 0 if value is None else len(str(value))


def _max_snapshot_growth(mine: list[IntegrityEvent]) -> tuple[int, int | None]:
    """Largest length growth between consecutive answer snapshots, and its time.

    The baseline for a snapshot is the later of the previous snapshot and the most
    recent `field_focus` (which carries the field's length when focused). A first
    snapshot with no baseline at all is skipped (no time origin to measure from).
    """
    prev: tuple[datetime, int] | None = None
    best = 0
    best_ms: int | None = None
    for e in mine:
        if e.type == "field_focus":
            prev = (_ts(e), _int((e.payload or {}).get("length")))
        elif e.type == "answer_change":
            cur = (_ts(e), _snapshot_len(e))
            if prev is not None:
                growth = cur[1] - prev[1]
                if growth > best:
                    best, best_ms = growth, _ms(prev[0], cur[0])
            prev = cur
    return best, best_ms


def _gradual_snapshot_growth(mine: list[IntegrityEvent]) -> int:
    """Chars the answer snapshots show arriving at HUMAN typing speed.

    Sum of the positive growths between consecutive baselines (previous snapshot /
    latest `field_focus`, as in `_max_snapshot_growth`) that are either small
    (< `SNAPSHOT_MIN_GROWTH`) or no faster than `GRADUAL_GROWTH_MAX_CPM`. A complete
    stream always accounts for at least this much in `writing_stats` (every typed
    char is counted, deletions only shrink the net growth), so an excess over the
    accounted chars means input events were lost — not that text was pasted: a
    paste / programmatic insert shows up as ONE fast jump, which is excluded here.
    """
    prev: tuple[datetime, int] | None = None
    total = 0
    for e in mine:
        if e.type == "field_focus":
            prev = (_ts(e), _int((e.payload or {}).get("length")))
        elif e.type == "answer_change":
            cur = (_ts(e), _snapshot_len(e))
            if prev is not None:
                growth = cur[1] - prev[1]
                if growth > 0 and (
                    growth < SNAPSHOT_MIN_GROWTH
                    or growth * 60_000 <= GRADUAL_GROWTH_MAX_CPM * max(1, _ms(prev[0], cur[0]))
                ):
                    total += growth
            prev = cur
    return total


def _restarts_inside(
    chron: Sequence[IntegrityEvent], start: datetime | None, end: datetime | None
) -> int:
    """Recorder restarts (page reloads) strictly inside the writing window.

    A reload produces a `pagehide` (unless the page crashed) and then a
    `device_info` from the new recorder; either one inside the window means the
    student kept writing after the page was torn down. The first load's
    `device_info` precedes the first focus and is not counted.
    """
    if start is None or end is None:
        return 0
    counts = dict.fromkeys(_RESTART_EVENT_TYPES, 0)
    for e in chron:
        if e.type in counts and start < _ts(e) < end:
            counts[e.type] += 1
    return max(counts.values())


def _aggregate_stats(stats: list[IntegrityEvent]) -> dict:
    """Sum the additive `writing_stats` windows for one item."""
    added: dict[str, int] = {}
    inserts: dict[str, int] = {}
    max_insert: dict[str, int] = {}
    hist = [0] * (len(IK_BUCKET_EDGES_MS) + 1)
    out = {
        "deleted_chars": 0,
        "keystrokes": 0,
        "untrusted_inputs": 0,
        "ik_count": 0,
        "ik_sum_ms": 0,
        "pauses_2s": 0,
        "pauses_10s": 0,
        "active_ms": 0,
    }
    for e in stats:
        p = e.payload or {}
        for key in out:
            out[key] += _int(p.get(key))
        for t, v in (p.get("added") or {}).items():
            added[t] = added.get(t, 0) + _int(v)
        for t, v in (p.get("inserts") or {}).items():
            inserts[t] = inserts.get(t, 0) + _int(v)
        for t, v in (p.get("max_insert") or {}).items():
            max_insert[t] = max(max_insert.get(t, 0), _int(v))
        for i, v in enumerate((p.get("ik_hist") or [])[: len(hist)]):
            hist[i] += _int(v)
    return {**out, "added": added, "inserts": inserts, "max_insert": max_insert, "ik_hist": hist}


def _hist_percentile(hist: list[int], q: float) -> float | None:
    """Percentile estimate from the fixed-bucket histogram (linear within a bucket).

    The open-ended last bucket (>= the last edge) returns its lower edge.
    """
    total = sum(hist)
    if total == 0:
        return None
    target = q * total
    cum = 0
    lower = 0
    for i, count in enumerate(hist):
        if count and cum + count >= target:
            if i >= len(IK_BUCKET_EDGES_MS):
                return float(IK_BUCKET_EDGES_MS[-1])
            upper = IK_BUCKET_EDGES_MS[i]
            return _r(lower + (upper - lower) * (target - cum) / count)
        cum += count
        lower = IK_BUCKET_EDGES_MS[i] if i < len(IK_BUCKET_EDGES_MS) else lower
    return float(IK_BUCKET_EDGES_MS[-1])  # pragma: no cover - float rounding guard


def _paired_intervals(
    chron: Sequence[IntegrityEvent], open_type: str, close_type: str
) -> list[tuple[datetime, datetime, str | None]]:
    """(start, end, item in focus at start) for each open->close pair; unclosed dropped."""
    out: list[tuple[datetime, datetime, str | None]] = []
    pending: IntegrityEvent | None = None
    for e in chron:
        if e.type == open_type:
            if pending is None:
                pending = e
        elif e.type == close_type and pending is not None:
            out.append((_ts(pending), _ts(e), pending.item_id))
            pending = None
    return out


def _overlap(
    intervals: list[tuple[datetime, datetime, str | None]],
    item_id: str,
    start: datetime | None,
    end: datetime | None,
) -> tuple[int, int]:
    """Total ms (and count) of intervals begun ON this item overlapping [start, end]."""
    if start is None or end is None:
        return 0, 0
    total = 0
    count = 0
    for s, e, item in intervals:
        if item != item_id:
            continue
        lo, hi = max(s, start), min(e, end)
        if hi > lo:
            total += _ms(lo, hi)
            count += 1
    return total, count


def _text_stats(text: str | None) -> dict:
    """Cheap, deterministic text statistics (level-mismatch evidence only)."""
    if not text:
        return {
            "word_count": 0,
            "sentence_count": 0,
            "mean_sentence_length": 0.0,
            "type_token_ratio": 0.0,
            "long_word_share": 0.0,
        }
    words = _WORD_RE.findall(text)
    n = len(words)
    if n == 0:
        return {
            "word_count": 0,
            "sentence_count": 0,
            "mean_sentence_length": 0.0,
            "type_token_ratio": 0.0,
            "long_word_share": 0.0,
        }
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if _WORD_RE.search(s)]
    n_sent = max(1, len(sentences))
    letters = [sum(ch.isalpha() for ch in w) for w in words]
    return {
        "word_count": n,
        "sentence_count": n_sent,
        "mean_sentence_length": _r(n / n_sent),
        "type_token_ratio": _r(len({w.lower() for w in words}) / n),
        "long_word_share": _r(sum(1 for c in letters if c >= LONG_WORD_LETTERS) / n),
    }


def _external_text(f: WritingFeatures, chars: int) -> bool:
    """`chars` of non-typed text clear the `text_appeared_without_typing` thresholds."""
    return (
        f.has_keystroke_telemetry
        and f.final_length > 0
        and chars >= EXTERNAL_TEXT_MIN_CHARS
        and chars >= EXTERNAL_TEXT_MIN_SHARE * f.final_length
    )


def _flags(f: WritingFeatures, *, has_changes: bool) -> list[str]:
    """Deterministic threshold facts (NOT a verdict). Order is fixed.

    `text_appeared_without_typing` has three routes: (a) OBSERVED non-keyboard
    inserts alone clear the thresholds, (b) a snapshot jump at an implausible rate,
    (c) observed + UNEXPLAINED chars clear them. Unexplained chars are a
    subtraction ("final length minus what we saw typed"), so lost telemetry
    inflates them: with `telemetry_gap` route (c) is NOT trusted and the weaker
    `telemetry_gap` flag is raised in its place. Routes (a) and (b) rest on
    positive observations and are unaffected by a gap.
    """
    flags: list[str] = []
    by_observed = _external_text(f, f.non_keyboard_chars)
    by_stats = _external_text(f, f.non_keyboard_chars + f.unexplained_chars)
    by_snapshot = (
        f.max_snapshot_growth >= SNAPSHOT_MIN_GROWTH
        and f.max_snapshot_growth_ms is not None
        and f.max_snapshot_growth * 60_000
        > SNAPSHOT_IMPLAUSIBLE_CPM * max(1, f.max_snapshot_growth_ms)
    )
    if by_observed or by_snapshot or (by_stats and not f.telemetry_gap):
        flags.append("text_appeared_without_typing")
    elif by_stats:  # rests on unexplained chars while telemetry was lost: weak
        flags.append("telemetry_gap")
    if f.untrusted_inputs > 0:
        flags.append("scripted_input")
    if f.max_paste_attempt_length >= LARGE_PASTE_CHARS:
        flags.append("large_paste_attempt")
    if (
        f.typed_chars >= TYPING_IMPLAUSIBLE_MIN_CHARS
        and f.chars_per_active_minute is not None
        and f.chars_per_active_minute > TYPING_IMPLAUSIBLE_CPM
    ):
        flags.append("typing_speed_implausible")
    if max(f.hidden_ms_while_writing, f.blur_ms_while_writing) >= AWAY_WHILE_WRITING_MS:
        flags.append("away_while_writing")
    if f.fullscreen_exits_while_writing > 0:
        flags.append("fullscreen_exit_while_writing")
    if (
        not f.has_keystroke_telemetry
        and not has_changes
        and f.final_length >= NO_TELEMETRY_MIN_CHARS
    ):
        flags.append("no_input_telemetry")
    return flags
