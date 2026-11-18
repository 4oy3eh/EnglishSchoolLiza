"""Listening tracks: which recording plays for which part (per-part audio).

Pure helpers shared by the server (validating `/audio-progress`) and mirrored by
the runner (`apps/web/exam.js` `buildModel`). The rule — see
`contracts.content.AudioAssetStimulus`:

* Consecutive sections are grouped by `skill` into blocks (Reading / Listening …),
  exactly as the runner does; only LISTENING blocks play audio.
* Inside a listening block, a section whose `audio_asset` stimulus names a
  different `asset_id` than the current track starts a NEW track; the same
  `asset_id` (every pre-per-part fixture repeats one long mp3 on each part) or a
  non-audio stimulus (e.g. A2 Listening Part 5's matching pool) continues it.
* A listening section before any audio has no track.

Tracks play strictly in order (track k+1 never starts before track k ended);
the runner enforces it, the server only stores the furthest point per track.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from contracts import AudioAssetStimulus, ClientSection, Section


@dataclass(frozen=True)
class ListeningTrack:
    """One recording and the parts (section ids) it plays under."""

    asset_id: str
    preview_asset_id: str | None
    plays: int
    locked: bool
    section_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def first_section_id(self) -> str:
        return self.section_ids[0]


def listening_tracks(sections: Sequence[Section | ClientSection]) -> list[ListeningTrack]:
    """The listening tracks of `sections` (served order), in play order."""
    opened: list[tuple[AudioAssetStimulus, list[str]]] = []
    current: tuple[AudioAssetStimulus, list[str]] | None = None
    prev_skill: str | None = None
    for n, section in enumerate(sections):
        skill = section.skill or "other"
        if n == 0 or skill != prev_skill:
            current = None  # a new block never continues the previous block's track
            prev_skill = skill
        if skill != "listening":
            continue
        stim = section.stimulus
        if isinstance(stim, AudioAssetStimulus) and (
            current is None or current[0].asset_id != stim.asset_id
        ):
            current = (stim, [])
            opened.append(current)
        if current is not None:
            current[1].append(section.id)
    return [
        ListeningTrack(
            asset_id=stim.asset_id,
            preview_asset_id=stim.preview_asset_id,
            plays=stim.plays,
            locked=stim.locked,
            section_ids=tuple(ids),
        )
        for stim, ids in opened
    ]


def track_asset_ids(sections: Sequence[Section | ClientSection]) -> set[str]:
    """Main-track asset ids a runner may report progress for.

    Callers pass the AUTHORED `test.sections`, while the runner builds its tracks
    from the SERVED sections (`ClientTest`, after pooling). That is correct for the
    default bank (one singleton pool per section: served == authored, and in any
    case the authored set is a superset of what any draw can serve). A custom bank
    whose pools drop / reorder sections could split tracks differently (e.g. two
    parts that are adjacent only after a draw); there, pass the attempt's served
    sections (rebuild the `AttemptLayout` from `Attempt.seed`) instead.
    """
    return {t.asset_id for t in listening_tracks(sections)}
