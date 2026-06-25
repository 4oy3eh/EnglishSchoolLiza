"""Seed a demo test for wire-up / E2E (Phase 12).

`make seed` (or `python -m app.content.seed`) loads one **human-approved** demo
test into the dev database so the student runner and teacher dashboard have real
content to drive end-to-end. It exists for local/demo use, not production.

The sample mirrors what the ingestion engine emits — a Cambridge A2 Key-style
mix of all four item types plus a listening section — but is hand-curated here so
the seed is self-contained and runnable without a PDF/LLM/ASR pipeline (real
ingestion is exercised separately). Honoring golden rule #5, the test is built as
a **draft** and then *explicitly published* (the human-approval step), with the
asset blobs written through the same `StorageBackend` the asset route reads, so
`/assets/{id}` resolves the listening mp3 and the image options.

Idempotent: re-running replaces the demo test, its roster, and its attempts.
"""

from __future__ import annotations

import io
import struct
import wave

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.content.service import ContentService
from app.content.storage import FilesystemStorage, StorageBackend
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    AudioAssetStimulus,
    GapFillItem,
    ImageOption,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    SingleChoiceItem,
    Test,
    TextOption,
)

log = get_logger(__name__)

SEED_TEST_ID = "seed-a2-demo"
SEED_ROSTER = ("Anna", "Bao", "Carlos", "Dmitri")

# Asset ids used by the demo (extensions drive the served content type).
_AUDIO_ID = "seed-listening-1.wav"
_IMG_IDS = ("seed-sign-a.png", "seed-sign-b.png", "seed-sign-c.png")


# --------------------------------------------------------------------------- #
# Tiny self-contained asset bytes (so the demo needs no external files).
# --------------------------------------------------------------------------- #
def _silent_wav(seconds: float = 0.3, rate: int = 8000) -> bytes:
    """A short, valid, silent mono WAV — playable by the runner's <audio>."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _solid_png() -> bytes:
    """A minimal valid 1x1 PNG — a placeholder image option."""
    # Hand-built 1x1 greyscale PNG (IHDR + IDAT + IEND), CRCs precomputed.
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)  # 1x1, 8-bit greyscale
    raw = b"\x00\xff"  # one scanline: filter byte + one white pixel
    idat = zlib.compress(raw)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def sample_assets() -> dict[str, tuple[bytes, str]]:
    """The demo's blobs keyed by asset_id -> (bytes, content_type)."""
    png = _solid_png()
    return {
        _AUDIO_ID: (_silent_wav(), "audio/wav"),
        **{img_id: (png, "image/png") for img_id in _IMG_IDS},
    }


# --------------------------------------------------------------------------- #
# The sample test (the shape ingestion produces; built as a draft).
# --------------------------------------------------------------------------- #
def build_sample_test() -> Test:
    """A draft A2 Key-style test covering all four item types + listening."""
    return Test(
        id=SEED_TEST_ID,
        title="A2 Key — Demo (seeded)",
        level="A2_KEY",
        status="draft",  # published explicitly below = the human-approval step
        duration_minutes=30,
        sections=[
            Section(
                id="seed-sec-reading",
                title="Reading Part 1",
                skill="reading",
                stimulus=PassageTextStimulus(
                    text="NOTICE: The swimming pool is closed on Mondays for cleaning."
                ),
                items=[
                    SingleChoiceItem(
                        id="seed-q-read",
                        prompt="When is the pool closed?",
                        options=[
                            TextOption(key="A", text="On Mondays"),
                            TextOption(key="B", text="On weekends"),
                            TextOption(key="C", text="Every day"),
                        ],
                        correct="A",
                    ),
                ],
            ),
            Section(
                id="seed-sec-listening",
                title="Listening Part 2",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=2),
                items=[
                    SingleChoiceItem(
                        id="seed-q-listen",
                        prompt="Which sign did the speaker describe?",
                        options=[
                            ImageOption(key="A", asset_id=_IMG_IDS[0], alt="Sign A"),
                            ImageOption(key="B", asset_id=_IMG_IDS[1], alt="Sign B"),
                            ImageOption(key="C", asset_id=_IMG_IDS[2], alt="Sign C"),
                        ],
                        correct="B",
                    ),
                ],
            ),
            Section(
                id="seed-sec-vocab",
                title="Reading Part 5 — Word matching",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    options=[
                        PoolOption(key="A", text="because"),
                        PoolOption(key="B", text="although"),
                        PoolOption(key="C", text="however"),
                    ],
                ),
                items=[
                    MatchingItem(
                        id="seed-q-match",
                        prompt="I went out ____ it was raining.",
                        correct="B",
                    ),
                ],
            ),
            Section(
                id="seed-sec-writing",
                title="Writing Part 6",
                skill="writing",
                stimulus=PassageTextStimulus(text="Complete the message to your friend."),
                items=[
                    GapFillItem(
                        id="seed-q-gap",
                        prompt="I have lived here ____ 2015.",
                        accepted=["since"],
                        accepted_variants=["scince"],
                    ),
                    OpenWritingItem(
                        id="seed-q-write",
                        prompt="Write an email inviting a friend to your birthday party.",
                        word_min=25,
                        bullet_points=["say when", "say where", "ask them to reply"],
                        rubric="Up to 5 for content, 5 for communicative achievement.",
                        grade_mode="llm",
                    ),
                ],
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# Seeding.
# --------------------------------------------------------------------------- #
def _clear_existing(session: Session, test_id: str) -> None:
    """Make the seed idempotent: drop the demo's attempts, roster, and test.

    Sqlite doesn't enforce FK `ondelete=CASCADE`, and roster has no ORM
    relationship off `TestRow`, so wipe the dependents explicitly before the
    ORM-cascade delete of the test (which clears sections + items).
    """
    attempt_ids = list(
        session.scalars(select(m.AttemptRow.id).where(m.AttemptRow.test_id == test_id))
    )
    if attempt_ids:
        session.execute(delete(m.AnswerRow).where(m.AnswerRow.attempt_id.in_(attempt_ids)))
        session.execute(
            delete(m.IntegrityEventRow).where(m.IntegrityEventRow.attempt_id.in_(attempt_ids))
        )
    session.execute(delete(m.AttemptRow).where(m.AttemptRow.test_id == test_id))
    session.execute(delete(m.RosterEntryRow).where(m.RosterEntryRow.test_id == test_id))
    ContentRepository(session).delete_test(test_id)


def seed(session: Session, storage: StorageBackend) -> str:
    """Write assets, load the draft, publish it (human-approval), add a roster."""
    _clear_existing(session, SEED_TEST_ID)

    content = ContentService(ContentRepository(session), storage)
    for asset_id, (data, content_type) in sample_assets().items():
        content.add_asset(asset_id, data, content_type=content_type)

    test = build_sample_test()
    content.create_test(test)
    # The human-approval gate (golden rule #5): a person reviewed the draft and
    # flips it to published. Here the seed stands in for that approval.
    content.publish(test.id)
    log.info("seed: published demo test id=%s (human-approved)", test.id)

    attempts = AttemptRepository(session)
    for name in SEED_ROSTER:
        entry = RosterEntry(id=f"seed-roster-{name.lower()}", test_id=test.id, display_name=name)
        attempts.add_roster_entry(entry)
    log.info("seed: roster of %d students for test=%s", len(SEED_ROSTER), test.id)
    return test.id


def main() -> None:
    configure_logging(settings.log_level)
    Base.metadata.create_all(engine)  # safe if migrations already ran
    storage = FilesystemStorage(settings.assets_dir)
    session = SessionLocal()
    try:
        test_id = seed(session, storage)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("seed failed")
        raise
    finally:
        session.close()
    log.info("seed complete: test=%s roster=%s", test_id, ", ".join(SEED_ROSTER))


if __name__ == "__main__":
    main()
