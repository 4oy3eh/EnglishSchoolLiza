"""Writing-integrity goldens: synthetic event streams -> WritingFeatures + flags.

Deterministic (golden rule #6): the integrity layer reduces the writing-field
telemetry to reproducible numbers and threshold flags; same events -> identical
profile. Also covers the rule engine's verdict over those flags (per item), the
shared inter-key histogram edges (telemetry / integrity / recorder.js), and the
telemetry payload validation that makes "no clipboard text stored" structural.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.analysis import deterministic_verdict, flag_segments
from app.analysis.fallback import writing_likelihood
from app.integrity import evaluate_rules, extract_profile
from app.integrity import writing as W
from app.telemetry.schema import IK_BUCKET_EDGES_MS, ClientEvent
from contracts import IntegrityEvent

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parent.parent

TEXT_A1 = "I like my dog. He is big and very funny."
TEXT_A2 = TEXT_A1 + " We play in the park every day after school."
TEXT_B = (
    "Furthermore, contemporary educational paradigms increasingly emphasise "
    "collaborative methodologies. "
) * 4


def ev(t: str, secs: float, item: str | None = "w1", payload: dict | None = None) -> IntegrityEvent:
    ts = T0 + timedelta(seconds=secs)
    return IntegrityEvent(
        attempt_id="att-w",
        item_id=item,
        type=t,  # type: ignore[arg-type]
        client_ts=ts,
        server_ts=ts,
        payload=payload or {},
    )


def _hist(bucket: int, n: int) -> list[int]:
    h = [0] * (len(IK_BUCKET_EDGES_MS) + 1)
    h[bucket] = n
    return h


def stats(**kw: object) -> dict:
    base: dict = {
        "field": "open_writing",
        "reason": "interval",
        "added": {},
        "inserts": {},
        "max_insert": {},
        "deleted_chars": 0,
        "keystrokes": 0,
        "untrusted_inputs": 0,
        "ik_count": 0,
        "ik_sum_ms": 0,
        "pauses_2s": 0,
        "pauses_10s": 0,
        "ik_hist": [],
        "active_ms": 0,
    }
    base.update(kw)
    return base


def honest_stream() -> list[IntegrityEvent]:
    """Typed in two windows, a few corrections, never left the page."""
    return [
        ev("field_focus", 0, payload={"field": "open_writing", "length": 0}),
        ev(
            "writing_stats",
            10,
            payload=stats(
                added={"insertText": 45},
                inserts={"insertText": 45},
                max_insert={"insertText": 1},
                deleted_chars=5,
                keystrokes=50,
                ik_count=49,
                ik_sum_ms=24_500,
                ik_hist=_hist(5, 49),
                active_ms=24_500,
            ),
        ),
        ev("answer_change", 10.6, payload={"value": TEXT_A1}),
        ev(
            "writing_stats",
            20,
            payload=stats(
                added={"insertText": 44},
                inserts={"insertText": 44},
                max_insert={"insertText": 1},
                keystrokes=44,
                ik_count=43,
                ik_sum_ms=21_500,
                ik_hist=_hist(5, 43),
                active_ms=21_500,
            ),
        ),
        ev("answer_change", 21, payload={"value": TEXT_A2}),
        ev("field_blur", 22, payload={"field": "open_writing", "length": 84}),
    ]


def cheat_stream() -> list[IntegrityEvent]:
    """Left for a minute, tried to paste 420 chars, then 400 chars appeared at once."""
    return [
        ev("field_focus", 0, payload={"field": "open_writing", "length": 0}),
        ev("window_blur", 5),
        ev("visibility_hidden", 5),
        ev("fullscreen_exit", 6),
        ev("visibility_visible", 65),
        ev("window_focus", 65),
        ev("paste_attempt", 66, payload={"kind": "paste", "length": 420, "field": "open_writing"}),
        ev(
            "input_burst",
            70,
            payload={
                "field": "open_writing",
                "input_type": "insertText",
                "length": 400,
                "field_length": 410,
                "trusted": True,
            },
        ),
        ev("answer_change", 70.6, payload={"value": TEXT_B}),
        ev(
            "writing_stats",
            75,
            payload=stats(
                added={"insertText": 410},
                inserts={"insertText": 11},
                max_insert={"insertText": 400},
                deleted_chars=10,
                keystrokes=12,
                ik_count=11,
                ik_sum_ms=2_200,
                ik_hist=_hist(4, 11),
                active_ms=2_200,
            ),
        ),
    ]


# --------------------------------------------------------------------------- #
# Goldens.
# --------------------------------------------------------------------------- #
def test_honest_writing_golden() -> None:
    profile = extract_profile("att-w", honest_stream())
    assert [w.item_id for w in profile.writing] == ["w1"]
    w = profile.writing[0]
    assert len(TEXT_A2) == 84
    assert w.has_keystroke_telemetry is True
    assert w.final_length == 84
    assert w.typed_chars == 89
    assert w.non_keyboard_chars == 0
    assert w.unexplained_chars == 0
    assert w.typed_ratio == 1.0
    assert w.deleted_chars == 5
    assert w.revision_ratio == pytest.approx(0.0562)
    assert w.keystrokes == 94
    assert w.active_typing_ms == 46_000
    assert w.chars_per_active_minute == pytest.approx(116.087)
    assert w.inter_key_mean_ms == 500.0
    assert w.inter_key_p50_ms == 400.0
    assert w.inter_key_p90_ms == 480.0
    assert w.max_snapshot_growth == 44
    assert w.max_snapshot_growth_ms == 10_400
    assert w.time_to_first_char_ms == 10_600
    assert w.writing_span_ms == 21_000
    assert w.hidden_ms_while_writing == 0
    assert w.paste_attempts == 0
    # Text statistics of the final text.
    assert w.word_count == 19
    assert w.sentence_count == 3
    assert w.mean_sentence_length == pytest.approx(6.3333)
    assert w.type_token_ratio == 1.0
    assert w.long_word_share == 0.0
    assert w.flags == []


def test_cheat_writing_golden() -> None:
    profile = extract_profile("att-w", cheat_stream())
    w = profile.writing[0]
    assert w.final_length == 400
    # The 400-char insertText burst is reclassified as non-keyboard (bulk).
    assert w.typed_chars == 10
    assert w.non_keyboard_chars == 400
    assert w.unexplained_chars == 0
    assert w.typed_ratio == pytest.approx(0.0244)
    assert w.max_non_keyboard_insert == 400
    assert w.paste_attempts == 1
    assert w.max_paste_attempt_length == 420
    assert w.max_snapshot_growth == 400
    assert w.max_snapshot_growth_ms == 70_600
    assert w.time_to_first_char_ms == 70_000
    assert w.hidden_ms_while_writing == 60_000
    assert w.blur_ms_while_writing == 60_000
    assert w.away_count == 1
    assert w.fullscreen_exits_while_writing == 1
    assert w.inter_key_p50_ms == 250.0
    assert w.inter_key_p90_ms == 290.0
    assert w.word_count == 32
    assert w.sentence_count == 4
    assert w.mean_sentence_length == 8.0
    assert w.type_token_ratio == 0.25
    assert w.long_word_share == 1.0
    assert w.flags == [
        "text_appeared_without_typing",
        "large_paste_attempt",
        "away_while_writing",
        "fullscreen_exit_while_writing",
    ]
    assert profile.paste_attempt_count == 1
    assert profile.fullscreen_exit_count == 1


def test_answers_override_snapshot_text() -> None:
    # The canonical submitted text (results layer) wins over the last snapshot.
    profile = extract_profile("att-w", honest_stream(), answers={"w1": TEXT_A1})
    assert profile.writing[0].final_length == 40
    # 89 typed - 5 deleted = 84 > 40: nothing unexplained.
    assert profile.writing[0].unexplained_chars == 0


def test_text_appearing_without_any_input_event_is_unexplained() -> None:
    # Stats only explain 84 chars, but the submitted text is 300 chars long.
    big = "x" * 300
    profile = extract_profile("att-w", honest_stream(), answers={"w1": big})
    w = profile.writing[0]
    assert w.unexplained_chars == 300 - 84
    assert "text_appeared_without_typing" in w.flags


def test_snapshot_route_without_keystroke_telemetry() -> None:
    # Old recorder / lost stats: 300 chars appeared one second after focusing.
    events = [
        ev("field_focus", 0, payload={"length": 0}),
        ev("answer_change", 1, payload={"value": "y" * 300}),
    ]
    w = extract_profile("att-w", events).writing[0]
    assert w.has_keystroke_telemetry is False
    assert w.typed_ratio is None
    assert w.max_snapshot_growth == 300
    assert w.max_snapshot_growth_ms == 1000
    assert w.flags == ["text_appeared_without_typing"]


def test_truncated_snapshot_uses_reported_length() -> None:
    events = [
        ev("field_focus", 0, payload={"length": 0}),
        ev(
            "answer_change",
            600,
            payload={"value": "a" * 200, "tail": "b" * 200, "length": 3000, "truncated": True},
        ),
    ]
    w = extract_profile("att-w", events).writing[0]
    assert w.final_length == 3000
    assert w.word_count == 0  # text unknown -> no text stats
    assert w.flags == []  # 3000 chars over 10 minutes is plausible typing


def test_answer_with_no_telemetry_at_all_is_flagged() -> None:
    # A writing answer that reached the server with no captured input whatsoever.
    profile = extract_profile("att-w", [], answers={"w9": "z" * 60}, writing_items=["w9"])
    assert [w.item_id for w in profile.writing] == ["w9"]
    assert profile.writing[0].flags == ["no_input_telemetry"]


def test_scripted_and_implausibly_fast_typing() -> None:
    events = [
        ev("field_focus", 0, payload={"length": 0}),
        ev(
            "writing_stats",
            30,
            payload=stats(
                added={"insertText": 300},
                inserts={"insertText": 300},
                max_insert={"insertText": 1},
                keystrokes=300,
                untrusted_inputs=3,
                ik_count=299,
                ik_sum_ms=20_000,
                ik_hist=_hist(1, 299),
                active_ms=20_000,
            ),
        ),
        ev("answer_change", 31, payload={"value": "w " * 150}),
    ]
    w = extract_profile("att-w", events).writing[0]
    assert w.chars_per_active_minute == 900.0
    assert w.flags == ["scripted_input", "typing_speed_implausible"]


def test_only_open_writing_fields_are_derived() -> None:
    # Telemetry tagged gap_fill does not create WritingFeatures.
    events = [ev("field_focus", 0, "g1", {"field": "gap_fill", "length": 0})]
    assert extract_profile("att-w", events).writing == []


def test_writing_features_are_deterministic_under_reordering() -> None:
    events = cheat_stream() + honest_stream_other_item()
    expected = extract_profile("att-w", events)
    for seed in range(5):
        shuffled = events[:]
        random.Random(seed).shuffle(shuffled)
        assert extract_profile("att-w", shuffled) == expected


def honest_stream_other_item() -> list[IntegrityEvent]:
    return [
        IntegrityEvent(
            **{
                **e.model_dump(),
                "item_id": "w2",
                "server_ts": e.server_ts + timedelta(seconds=500),
                "client_ts": e.client_ts + timedelta(seconds=500),
            }
        )
        for e in honest_stream()
    ]


def test_hist_percentile_edges() -> None:
    assert W._hist_percentile([0] * 11, 0.5) is None
    open_bucket = [0] * 10 + [4]
    assert W._hist_percentile(open_bucket, 0.5) == 10_000.0


def test_ik_bucket_edges_match_across_layers() -> None:
    # integrity must not import telemetry, so the edges are duplicated: keep them equal.
    assert W.IK_BUCKET_EDGES_MS == IK_BUCKET_EDGES_MS
    js = (REPO / "apps" / "web" / "recorder.js").read_text(encoding="utf-8")
    m = re.search(r"var IK_EDGES = \[([^\]]+)\]", js)
    assert m is not None
    assert tuple(int(x) for x in m.group(1).split(",")) == IK_BUCKET_EDGES_MS


# --------------------------------------------------------------------------- #
# The rule engine's verdict over the writing flags (documented combination).
# --------------------------------------------------------------------------- #
def test_deterministic_verdict_formula_on_cheat_stream() -> None:
    events = cheat_stream()
    profile = extract_profile("att-w", events)
    segments = flag_segments(profile, events)
    assert [s.kind for s in segments] == ["long_hidden", "writing_flagged"]
    writing_seg = segments[-1]
    assert {e.type for e in writing_seg.events} >= {"paste_attempt", "input_burst"}

    draft = deterministic_verdict(profile, evaluate_rules(profile, events))
    # writing item: strong (text appeared) + 2 medium (paste, away) + weak (fullscreen)
    #   1 - 0.35 * 0.70 * 0.70 * 0.90
    assert writing_likelihood(profile.writing[0]) == pytest.approx(0.8457, abs=1e-4)
    # attempt: the same hits + medium long_time_away (60 s = 80 % of the 75 s span)
    #   1 - 0.35 * 0.70^3 * 0.90
    assert draft.suspicion_score == pytest.approx(0.892)
    assert draft.confidence == 0.4  # CONFIDENCE_PARTIAL: one item seen
    assert draft.model_id is None
    assert draft.flags == (
        "text_appeared_without_typing",
        "long_time_away",
        "large_paste_attempt",
        "away_while_writing",
        "fullscreen_exit_while_writing",
    )
    assert draft.writing[0].item_id == "w1"
    assert draft.writing[0].signals == profile.writing[0].flags
    assert draft.writing[0].rationale.startswith("Сработавшие правила: Текст появился")


def test_deterministic_likelihood_never_reaches_certainty() -> None:
    profile = extract_profile("att-w", cheat_stream())
    weak_only = profile.writing[0].model_copy(
        update={"flags": ["telemetry_gap", "no_input_telemetry", "fullscreen_exit_while_writing"]}
    )
    assert writing_likelihood(weak_only) == pytest.approx(0.271)  # 1 - 0.9^3: stays low
    every = profile.writing[0].model_copy(update={"flags": list(W_ALL_FLAGS)})
    assert writing_likelihood(every) == 0.95  # capped: never certainty
    stacked = profile.writing[0].model_copy(update={"flags": list(W_ALL_FLAGS) * 3})
    assert writing_likelihood(stacked) == 0.95
    unknown = profile.writing[0].model_copy(update={"flags": ["not_a_rule"]})
    assert writing_likelihood(unknown) == 0.0  # an unknown flag weighs nothing


W_ALL_FLAGS = (
    "text_appeared_without_typing",
    "scripted_input",
    "typing_speed_implausible",
    "large_paste_attempt",
    "away_while_writing",
    "no_input_telemetry",
    "fullscreen_exit_while_writing",
)


# --------------------------------------------------------------------------- #
# Telemetry: typed payload validation — never content.
# --------------------------------------------------------------------------- #
def _client(t: str, payload: dict) -> ClientEvent:
    return ClientEvent(type=t, client_ts=T0, item_id="w1", payload=payload)  # type: ignore[arg-type]


def test_paste_attempt_payload_cannot_carry_text() -> None:
    ok = _client("paste_attempt", {"kind": "paste", "length": 12})
    assert ok.payload == {"kind": "paste", "length": 12, "field": None}
    with pytest.raises(ValidationError):
        _client("paste_attempt", {"kind": "paste", "length": 12, "text": "secret"})


def test_input_burst_payload_cannot_carry_data() -> None:
    with pytest.raises(ValidationError):
        _client("input_burst", {"input_type": "insertText", "length": 3, "data": "abc"})


def test_writing_stats_payload_is_normalized_and_bounded() -> None:
    e = _client("writing_stats", {"added": {"insertText": 5}, "keystrokes": 5})
    assert e.payload["added"] == {"insertText": 5}
    assert e.payload["deleted_chars"] == 0  # defaults filled in
    with pytest.raises(ValidationError):
        _client("writing_stats", {"keystrokes": -1})
    with pytest.raises(ValidationError):
        _client("writing_stats", {"added": {f"t{i}": 1 for i in range(40)}})


def test_legacy_event_payloads_are_untouched() -> None:
    e = _client("interaction", {"blocked": "copy", "anything": 1})
    assert e.payload == {"blocked": "copy", "anything": 1}


def test_numeric_payload_fields_have_upper_bounds() -> None:
    day = 86_400_000
    ok = _client("writing_stats", {"ik_sum_ms": day, "active_ms": day, "window_ms": day})
    assert ok.payload["ik_sum_ms"] == day
    for field in ("ik_sum_ms", "active_ms", "window_ms", "ik_mean_ms", "ik_p50_ms", "ik_p90_ms"):
        with pytest.raises(ValidationError):
            _client("writing_stats", {field: day + 1})
    for field in ("keystrokes", "deleted_chars", "ik_count", "pauses_2s", "length"):
        with pytest.raises(ValidationError):
            _client("writing_stats", {field: 10_000_001})
    with pytest.raises(ValidationError):
        _client("writing_stats", {"ik_hist": [10**12]})
    with pytest.raises(ValidationError):
        _client("writing_stats", {"added": {"insertText": 10**12}})
    # The top-level duration is bounded too (still fits a 32-bit column).
    week = 7 * day
    assert (
        ClientEvent(type="visibility_visible", client_ts=T0, duration_ms=week).duration_ms == week
    )
    with pytest.raises(ValidationError):
        ClientEvent(type="visibility_visible", client_ts=T0, duration_ms=week + 1)


# --------------------------------------------------------------------------- #
# Replayed telemetry (the recorder is at-least-once) must not double anything.
# --------------------------------------------------------------------------- #
def _replayed(events: list[IntegrityEvent], delay: float = 300.0) -> list[IntegrityEvent]:
    """The same captures ingested a second time, later (new server_ts only)."""
    return [
        e.model_copy(update={"server_ts": e.server_ts + timedelta(seconds=delay)}) for e in events
    ]


def test_dedupe_events_drops_exact_replays_only() -> None:
    a = ev("input_burst", 1, payload={"input_type": "insertText", "length": 5, "field_length": 5})
    later = ev(
        "input_burst", 2, payload={"input_type": "insertText", "length": 5, "field_length": 10}
    )
    replay = a.model_copy(update={"server_ts": a.server_ts + timedelta(minutes=5)})
    reordered_keys = a.model_copy(
        update={"payload": {"field_length": 5, "length": 5, "input_type": "insertText"}}
    )
    assert W.dedupe_events([a, later, replay, reordered_keys]) == [a, later]
    # Idempotent, order-preserving, and a different item / client time is distinct.
    other_item = a.model_copy(update={"item_id": "w2"})
    assert W.dedupe_events(W.dedupe_events([a, other_item, later])) == [a, other_item, later]


@pytest.mark.parametrize("stream", [honest_stream, cheat_stream])
def test_duplicated_batch_does_not_change_the_profile(stream) -> None:  # noqa: ANN001
    events = stream()
    once = extract_profile("att-w", events)
    # The whole stream replayed after a reload, and one batch sent three times.
    twice = extract_profile("att-w", events + _replayed(events))
    thrice = extract_profile("att-w", events + _replayed(events) + _replayed(events[:4], 900))
    assert twice == once
    assert thrice == once
    assert twice.writing[0].typed_chars == once.writing[0].typed_chars
    assert twice.writing[0].keystrokes == once.writing[0].keystrokes
    assert twice.paste_attempt_count == once.paste_attempt_count


def test_replayed_stats_do_not_double_the_typed_counts() -> None:
    # Regression: a doubled writing_stats window used to double typed chars /
    # keystrokes (hiding `unexplained_chars`), a doubled paste_attempt the count.
    events = honest_stream()
    w = extract_profile("att-w", events + _replayed(events)).writing[0]
    assert (w.typed_chars, w.deleted_chars, w.non_keyboard_chars) == (89, 5, 0)
    assert w.keystrokes == 94 and w.active_typing_ms == 46_000
    assert w.flags == []
    direct = W.extract_writing_features(events + _replayed(events))  # extractor alone
    assert direct[0].typed_chars == 89
    cheat = cheat_stream()
    assert extract_profile("att-w", cheat + _replayed(cheat)).writing[0].paste_attempts == 1


# --------------------------------------------------------------------------- #
# Telemetry gaps: lost keystroke telemetry is not "text without typing".
# --------------------------------------------------------------------------- #
GAP_TEXT = "I went to the park with my friends and we played football all day. " * 3  # 201


def _typed_snapshots(
    start: float, upto: int, step: int = 30, every: float = 10.0
) -> list[IntegrityEvent]:
    """answer_change snapshots growing `step` chars every `every` s (human speed)."""
    out, t, n = [], start, step
    while n < upto:
        out.append(ev("answer_change", t, payload={"value": GAP_TEXT[:n]}))
        t, n = t + every, n + step
    out.append(ev("answer_change", t, payload={"value": GAP_TEXT[:upto]}))
    return out


def lost_stats_stream() -> list[IntegrityEvent]:
    """201 chars typed at human speed, but only the first 60 have writing_stats
    (the later batches were dropped): the snapshots prove the typing."""
    return [
        ev("field_focus", 0, payload={"field": "open_writing", "length": 0}),
        *_typed_snapshots(10, len(GAP_TEXT)),
        ev(
            "writing_stats",
            21,
            payload=stats(
                added={"insertText": 60},
                inserts={"insertText": 60},
                max_insert={"insertText": 1},
                keystrokes=60,
                ik_count=59,
                ik_sum_ms=19_000,
                ik_hist=_hist(5, 59),
                active_ms=19_000,
            ),
        ),
    ]


def test_lost_stats_are_a_telemetry_gap_not_text_without_typing() -> None:
    w = extract_profile("att-w", lost_stats_stream()).writing[0]
    assert w.final_length == 201 and w.typed_chars == 60
    assert w.unexplained_chars == 141  # would trip text_appeared_without_typing
    assert w.uncovered_snapshot_growth == 141  # ...but the snapshots show typing
    assert w.recorder_restarts_while_writing == 0
    assert w.telemetry_gap is True
    assert w.flags == ["telemetry_gap"]  # downgraded: a weak rule, not the strong one
    assert writing_likelihood(w) == 0.1


def test_reload_inside_the_writing_window_is_a_telemetry_gap() -> None:
    # Typed 40 chars with stats, the page reloaded (pagehide + the new recorder's
    # device_info), and what was typed around the reload never got its stats.
    events = [
        *honest_stream()[:3],  # focus, stats(45 typed, 5 deleted), snapshot TEXT_A1 (40)
        ev("pagehide", 12),
        ev("device_info", 14, item=None, payload={"user_agent": "UA", "platform": "p"}),
        ev("field_focus", 15, payload={"field": "open_writing", "length": 40}),
        ev("answer_change", 30, payload={"value": TEXT_A1 + "y" * 200}),
    ]
    w = extract_profile("att-w", events).writing[0]
    assert w.recorder_restarts_while_writing == 1
    assert w.unexplained_chars == 200
    # 200 chars in 15 s = 800 cpm: under the 1500 cpm snapshot threshold, over the
    # 600 cpm "typed" one -> the snapshots are no positive evidence either way.
    assert w.uncovered_snapshot_growth == 0
    assert w.telemetry_gap is True
    assert w.flags == ["telemetry_gap"]
    # The very same stream WITHOUT the reload keeps the strong flag.
    no_reload = [e for e in events if e.type not in ("pagehide", "device_info")]
    w2 = extract_profile("att-w", no_reload).writing[0]
    assert w2.telemetry_gap is False
    assert w2.flags == ["text_appeared_without_typing"]


def test_first_load_device_info_is_not_a_restart() -> None:
    events = [ev("device_info", -5, item=None, payload={"user_agent": "UA"}), *honest_stream()]
    w = extract_profile("att-w", events).writing[0]
    assert w.recorder_restarts_while_writing == 0 and w.telemetry_gap is False
    assert w.uncovered_snapshot_growth == 0


def test_telemetry_gap_never_hides_observed_external_text() -> None:
    # A gap (reload) does not excuse POSITIVELY observed evidence: the 400-char
    # bulk insert keeps the strong flag...
    events = [
        *cheat_stream(),
        ev("pagehide", 68),
        ev("device_info", 69, item=None, payload={"user_agent": "UA"}),
    ]
    w = extract_profile("att-w", events).writing[0]
    assert w.telemetry_gap is True and w.non_keyboard_chars == 400
    assert w.flags[0] == "text_appeared_without_typing"
    assert "telemetry_gap" not in w.flags

    # ...and so does an implausible snapshot jump (300 chars in under a second).
    jump = [
        ev("field_focus", 0, payload={"length": 0}),
        ev("pagehide", 0.2),
        ev("device_info", 0.4, item=None, payload={"user_agent": "UA"}),
        ev("answer_change", 1, payload={"value": "y" * 300}),
    ]
    assert extract_profile("att-w", jump).writing[0].flags == ["text_appeared_without_typing"]


def test_gap_features_reach_the_analyst_prompt() -> None:
    from app.analysis.llm_gemini import build_prompt, system_prompt

    events = lost_stats_stream()
    profile = extract_profile("att-w", events)
    prompt = build_prompt(profile, flag_segments(profile, events), None, nonce="ab" * 8)
    assert '"telemetry_gap": true' in prompt
    assert '"uncovered_snapshot_growth": 141' in prompt
    assert "telemetry_gap" in system_prompt("ab" * 8)  # ...and the rule to discount it
