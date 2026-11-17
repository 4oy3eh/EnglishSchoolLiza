"""The runner's listening notes (a recording stopped / failed) in the teacher timeline.

`apps/web/exam.js` sends `audio_play` with `payload.note` when a locked track is
refused after a pause, fails to load, or is skipped after repeated load errors. The
dashboard turns it into a Russian sentence; a plain `audio_play` stays "Аудио ▶".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.admin.service import _AUDIO_NOTE_RU, _timeline_label


def _ev(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type="audio_play", payload=payload, duration_ms=None, item_id="sec-1")


@pytest.mark.parametrize("note", ["play_refused", "load_error", "skipped"])
def test_audio_note_is_a_russian_warning_sentence(note: str) -> None:
    label = _timeline_label(_ev({"position": 42.5, "note": note}), None, None, {}, None)
    assert label.startswith("⚠︎ Аудио: ")
    assert _AUDIO_NOTE_RU[note] in label
    assert "0:42" in label


def test_plain_audio_play_and_unknown_note_unchanged() -> None:
    assert _timeline_label(_ev({"position": 5}), None, None, {}, None) == "Аудио ▶ с 0:05"
    weird = _timeline_label(_ev({"position": 5, "note": "<b>x</b>"}), None, None, {}, None)
    assert weird == "Аудио ▶ с 0:05"  # unknown notes are never echoed
