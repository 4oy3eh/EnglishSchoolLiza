"""Builder for the A2 Key for Schools 2022 sample test (manual `pdf`-skill ingest).

This is the first *real* content ingest (Option B in
`docs/INGEST_VIA_CLAUDE_CODE.md`): Claude Code read the Cambridge sample PDFs with
the `pdf` skill, transcribed the question paper, cropped the sign/picture images,
and took every `correct`/`accepted` value **only** from the answer-key PDF. The
result is assembled here as a typed `contracts.Test` and loaded as a **draft**
(golden rule #5 — a human reviews before publish).

Provenance of the source bytes (local, gitignored — see the runbook §1):

* sign / picture / story crops: rendered + cropped from the QP PDFs into
  ``var/extract/crops/`` (see the ingest session); read back here by filename.
* listening audio: the bundled ``assets/a2/...listening-sample-test.mp3``, split
  (lossless) into an intro ``preview`` + the test track under ``var/extract/``.

Run ``python -m app.content.ingest_a2_2022`` to (re)load the draft. It does NOT
publish and adds NO roster — those are the post-review steps.
"""

from __future__ import annotations

from pathlib import Path

from app.content.load_test import load_test
from app.content.storage import FilesystemStorage
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from contracts import (
    AudioAssetStimulus,
    GapFillItem,
    GappedTextStimulus,
    ImageOption,
    ImageSetStimulus,
    Item,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    PassageTextStimulus,
    PoolOption,
    Section,
    SingleChoiceItem,
    Test,
    TextOption,
)

log = get_logger(__name__)

TEST_ID = "a2-2022"

# --- where the source asset bytes live (local, gitignored) ----------------- #
# The source mp3 was split (lossless stream-copy) into a freely-replayable intro
# preview ([0, 32s) — the recording's opening explanation) and the test track
# (everything after, keeping Cambridge's baked-in double play). See runbook §10.
_CROPS = Path("var/extract/crops")
_TEST_MP3 = Path("var/extract/a2-2022-listening.mp3")
_PREVIEW_MP3 = Path("var/extract/a2-2022-listening-preview.mp3")

_AUDIO_ID = "a2-2022-listening.mp3"
_PREVIEW_ID = "a2-2022-listening-preview.mp3"


# --------------------------------------------------------------------------- #
# Assets: clean asset_id -> (bytes, content_type), read from the local crops.
# --------------------------------------------------------------------------- #
def _png(path: Path) -> tuple[bytes, str]:
    if not path.is_file():
        raise FileNotFoundError(f"missing crop {path} (re-run the ingest crop step)")
    return path.read_bytes(), "image/png"


def assets() -> dict[str, tuple[bytes, str | None]]:
    """All blobs the test references, keyed by the asset_id used in the contract."""
    out: dict[str, tuple[bytes, str | None]] = {}
    # Reading Part 1 signs (one per question).
    for q in range(1, 7):
        out[f"a2-2022-r1-q{q}-sign.png"] = _png(_CROPS / f"rw_p1_q{q}.png")
    # Listening Part 1 picture options (3 per question).
    for q in range(1, 6):
        for c in ("a", "b", "c"):
            out[f"a2-2022-l1-q{q}-{c}.png"] = _png(_CROPS / f"lis_p1_q{q}_{c.upper()}.png")
    # Writing Part 7 story pictures.
    for n in range(1, 4):
        out[f"a2-2022-w7-pic{n}.png"] = _png(_CROPS / f"rw_p7_pic{n}.png")
    # Listening audio: a free intro preview + the test track (see runbook §8/§10).
    for asset_id, path in ((_PREVIEW_ID, _PREVIEW_MP3), (_AUDIO_ID, _TEST_MP3)):
        if not path.is_file():
            raise FileNotFoundError(f"missing audio {path} (re-run the mp3 split step)")
        out[asset_id] = (path.read_bytes(), "audio/mpeg")
    return out


# --------------------------------------------------------------------------- #
# Section builders. Item order within a section is exactly the printed order
# (golden rule #7). Every `correct`/`accepted` is from the answer-key PDF.
# --------------------------------------------------------------------------- #
def _choice(
    item_id: str, prompt: str, options: list[tuple[str, str]], correct: str
) -> SingleChoiceItem:
    return SingleChoiceItem(
        id=item_id,
        prompt=prompt,
        options=[TextOption(key=k, text=t) for k, t in options],
        correct=correct,
    )


# ---- Reading Part 1: six signs, one single-item section each -------------- #
# (sign image binds to its own question; runbook §8 per-question stimulus.)
_R1 = [
    (
        1,
        "Go upstairs if you want to",
        [
            ("A", "buy a dress for a party."),
            ("B", "pay less for something to read."),
            ("C", "find a game for a teenager."),
        ],
        "B",
    ),
    (
        2,
        "Choose the correct answer.",
        [
            ("A", "Greta has forgotten when the next maths class is."),
            ("B", "Greta hopes Fiona will help her find her maths notes."),
            ("C", "Greta wants to know what the maths homework is."),
        ],
        "C",
    ),
    (
        3,
        "Choose the correct answer.",
        [
            ("A", "Students not going on the trip cannot have a day off school."),
            ("B", "Students have to decide today if they would like to join the trip."),
            ("C", "Students going on the trip must come to school first."),
        ],
        "A",
    ),
    (
        4,
        "Choose the correct answer.",
        [
            ("A", "Pay for tickets online before picking them up at school."),
            ("B", "Check the website for information about when tickets will be available."),
            ("C", "Let the office know soon if you are planning to buy tickets."),
        ],
        "A",
    ),
    (
        5,
        "What should Andy do?",
        [
            ("A", "invite some friends to play football"),
            ("B", "tell Jake if he can join him later"),
            ("C", "show Tom where Woodside School is"),
        ],
        "B",
    ),
    (
        6,
        "Choose the correct answer.",
        [
            ("A", "Swimmers at all levels can enter this competition."),
            ("B", "This competition is for people who can swim over 200 metres."),
            ("C", "The races in the competition will be 200 metres long."),
        ],
        "B",
    ),
]


def _reading_part1() -> list[Section]:
    sections = []
    for q, prompt, options, correct in _R1:
        sections.append(
            Section(
                id=f"a2-2022-sec-r1-q{q}",
                title=f"Reading Part 1 — Question {q}",
                skill="reading",
                stimulus=ImageSetStimulus(asset_ids=[f"a2-2022-r1-q{q}-sign.png"]),
                items=[_choice(f"a2-2022-r1-q{q}", prompt, options, correct)],
            )
        )
    return sections


# ---- Reading Part 2: match people to texts -------------------------------- #
# Modelled as single_choice (options A=Amy, B=Flora, C=Louisa) over a passage
# holding all three texts, because MatchingPoolStimulus cannot also carry the
# three readable paragraphs (one stimulus per section). See runbook §8 + report.
_R2_PASSAGE = (
    "School gardens competition\n\n"
    "Amy:\n"
    "Our class has just won a prize for our school garden in a competition — and "
    "they're going to make a TV film about it! The judges liked our garden because "
    "the flowers are all different colours — and we painted some more on the wall "
    "around it. My cousin gave us advice about what to grow — she's learning about "
    "gardening at college. We're planning to grow some vegetables next year. I just "
    "hope the insects don't eat them all!\n\n"
    "Flora:\n"
    "Our teacher heard about the school garden competition on TV and told us about "
    "it. We decided to enter and won second prize! There's a high wall in our garden "
    "where many red and yellow climbing flowers grow and it looks as pretty as a "
    "painting! Our prize is a visit to a special garden where there are lots of "
    "butterflies and other insects. My aunt works there and she says it's amazing.\n\n"
    "Louisa:\n"
    "The garden our class entered in the competition is very special. The flowers "
    "we've grown are all yellow! They look lovely on the video we made of the garden. "
    "We also grew lots of carrots and potatoes, and everyone says they taste "
    "fantastic. It was an interesting project. Our teacher taught us lots of things "
    "about the butterflies in our garden. We also watched a TV programme about them, "
    "and did some paintings to put on the classroom wall."
)

_R2_PEOPLE = [("A", "Amy"), ("B", "Flora"), ("C", "Louisa")]
_R2 = [
    (7, "Whose class learnt about the garden competition from a TV programme?", "B"),
    (8, "Whose class grew some vegetables?", "C"),
    (9, "Whose class won a trip in the school garden competition?", "B"),
    (10, "Whose class painted flowers on their garden wall?", "A"),
    (11, "Whose class learnt about the insects in their garden?", "C"),
    (12, "Whose class got help from someone in a pupil's family?", "A"),
    (13, "Whose class chose flowers that were the same colour?", "C"),
]


def _reading_part2() -> Section:
    return Section(
        id="a2-2022-sec-r2",
        title="Reading Part 2 — School gardens competition",
        skill="reading",
        stimulus=PassageTextStimulus(text=_R2_PASSAGE),
        items=[
            _choice(f"a2-2022-r2-q{q}", prompt, _R2_PEOPLE, correct) for q, prompt, correct in _R2
        ],
    )


# ---- Reading Part 3: long text, 3-option questions ------------------------ #
_R3_PASSAGE = (
    "Starting at a new school\nBy Anna Gray, age 11\n\n"
    "I've just finished my first week at a new school and I'd like to tell you about "
    "it. Like other children in my country, I went to primary school until I was "
    "eleven and then I had to go to a different school for older children. I loved my "
    "primary school but I was excited to move to a new school.\n\n"
    "It was very strange on our first day. There were some kids from my primary school "
    "there, but most of the children in my year group were from different schools. But "
    "I soon started talking to the girl who was sitting beside me in maths. She lives "
    "near me so we walked home together. We're best friends now.\n\n"
    "When I saw our timetable there were lots of subjects, some were quite new to me! "
    "Lessons are harder now. They're longer and the subjects are more difficult, but "
    "the teachers help us a lot.\n\n"
    "At primary school we had all our lessons in one classroom. Now each subject is "
    "taught in a different room. It was difficult to find the classrooms at first "
    "because the school is so big. But the teachers gave us each a map of the school, "
    "so it's getting easier now.\n\n"
    "The worst thing is that I have lots more homework to do now. Some of it is fun "
    "but I need to get better at remembering when I have to give different pieces of "
    "work to the teachers!"
)
_R3 = [
    (
        14,
        "How did Anna feel about moving to a new school?",
        [
            ("A", "worried about being with lots of older children"),
            ("B", "happy about the idea of doing something different"),
            ("C", "pleased because she was bored at her primary school"),
        ],
        "B",
    ),
    (
        15,
        "Who has become Anna's best friend at her new school?",
        [
            ("A", "someone from her primary school"),
            ("B", "someone she knew from her home area"),
            ("C", "someone she met in her new class"),
        ],
        "C",
    ),
    (
        16,
        "What does Anna say about the timetable at her new school?",
        [
            ("A", "It includes subjects she didn't do at primary school."),
            ("B", "She has shorter lessons than she had at her old school."),
            ("C", "It is quite difficult to understand."),
        ],
        "A",
    ),
    (
        17,
        "Why couldn't Anna find her classrooms?",
        [
            ("A", "She couldn't read a map."),
            ("B", "There was little time between lessons."),
            ("C", "The school building was very large."),
        ],
        "C",
    ),
    (
        18,
        "What does Anna say about the homework she has now?",
        [
            ("A", "She gets more help from some teachers than others."),
            ("B", "She thinks it is the hardest part of school life."),
            ("C", "She remembers everything she's told to do."),
        ],
        "B",
    ),
]


def _reading_part3() -> Section:
    return Section(
        id="a2-2022-sec-r3",
        title="Reading Part 3 — Starting at a new school",
        skill="reading",
        stimulus=PassageTextStimulus(text=_R3_PASSAGE),
        items=[_choice(f"a2-2022-r3-q{q}", prompt, opts, c) for q, prompt, opts, c in _R3],
    )


# ---- Reading Part 4: gapped text, choose the word ------------------------- #
_R4_TEXT = (
    "Wivenhoe hotel\n\n"
    "Wivenhoe is a beautiful hotel in the countryside, with many rooms and an "
    "excellent restaurant. However, there is a big (19) ........... between Wivenhoe "
    "and other hotels. Firstly, Wivenhoe is part of a university, and secondly, its "
    "staff are all teenagers.\n\n"
    "In fact, Wivenhoe is a hotel school for young people who are (20) ........... to "
    "get jobs in the hotel or restaurant (21) ........... The students learn by "
    "helping staff in a real hotel, while their teachers (22) ........... them "
    "carefully. They do everything, from making beds and cleaning bathrooms to "
    "preparing menus and (23) ........... the telephone.\n\n"
    "Some British people may think that a hotel run by students is a rather strange "
    "idea, but many visitors say that Wivenhoe is the best hotel they have ever "
    "(24) ........... at."
)
_R4 = [
    (19, [("A", "change"), ("B", "variety"), ("C", "difference")], "C"),
    (20, [("A", "knowing"), ("B", "hoping"), ("C", "explaining")], "B"),
    (21, [("A", "business"), ("B", "work"), ("C", "career")], "A"),
    (22, [("A", "see"), ("B", "look"), ("C", "watch")], "C"),
    (23, [("A", "calling"), ("B", "answering"), ("C", "speaking")], "B"),
    (24, [("A", "entered"), ("B", "stayed"), ("C", "gone")], "B"),
]


def _reading_part4() -> Section:
    return Section(
        id="a2-2022-sec-r4",
        title="Reading Part 4 — Wivenhoe hotel",
        skill="reading",
        stimulus=GappedTextStimulus(text=_R4_TEXT),
        items=[
            _choice(f"a2-2022-r4-q{q}", f"Choose the correct word for gap {q}.", opts, c)
            for q, opts, c in _R4
        ],
    )


# ---- Reading Part 5: open cloze (write one word) -------------------------- #
_R5_TEXT = (
    "From: Anita\nTo: Sasha\n\n"
    "Thank you (0) for your email. Living in Canada sounds really great! I'm glad "
    "that you like (25) ........... new house. What's the weather like? "
    "(26) ........... it very cold in Canada? Does it snow every day?\n\n"
    "I heard that a (27) ........... of Canadians speak two languages — English and "
    "French. Are you having French lessons? Do you watch programmes (28) ........... "
    "TV in French too?\n\n"
    "How about the students in your new school? Are (29) ........... friendly? And "
    "send some photos too — I would like to know more about them.\n\n"
    "I've got (30) ........... go now, but I'll write again soon."
)
# Q25's key lists two acceptable words ("your / the").
_R5 = [
    (25, ["your", "the"]),
    (26, ["Is"]),
    (27, ["lot"]),
    (28, ["on"]),
    (29, ["they"]),
    (30, ["to"]),
]


def _reading_part5() -> Section:
    return Section(
        id="a2-2022-sec-r5",
        title="Reading Part 5 — Email (open cloze)",
        skill="reading",
        stimulus=GappedTextStimulus(text=_R5_TEXT),
        items=[
            GapFillItem(
                id=f"a2-2022-r5-q{q}",
                prompt=f"Write one word for gap {q}.",
                accepted=accepted,
            )
            for q, accepted in _R5
        ],
    )


# ---- Writing Parts 6 & 7 (open writing — no answer key, by design) -------- #
def _writing_part6() -> Section:
    return Section(
        id="a2-2022-sec-w6",
        title="Writing Part 6",
        skill="writing",
        stimulus=PassageTextStimulus(
            text=(
                "You are going shopping with your English friend Pat tomorrow.\n"
                "Write an email to Pat."
            )
        ),
        items=[
            OpenWritingItem(
                id="a2-2022-w6-q31",
                prompt="Write an email to Pat. Write 25 words or more.",
                word_min=25,
                bullet_points=[
                    "where you want to meet",
                    "what time you want to meet",
                    "what you want to buy",
                ],
                rubric=(
                    "A2 Key Writing Part 6 (max 5 marks). Award for clear "
                    "communication of all three content points (where to meet, what "
                    "time, what to buy) in a short email of 25+ words. Minor errors "
                    "that do not impede communication are acceptable at A2."
                ),
                grade_mode="llm",
            )
        ],
    )


def _writing_part7() -> Section:
    return Section(
        id="a2-2022-sec-w7",
        title="Writing Part 7 — Picture story",
        skill="writing",
        stimulus=ImageSetStimulus(
            asset_ids=["a2-2022-w7-pic1.png", "a2-2022-w7-pic2.png", "a2-2022-w7-pic3.png"]
        ),
        items=[
            OpenWritingItem(
                id="a2-2022-w7-q32",
                prompt=(
                    "Look at the three pictures. Write the story shown in the "
                    "pictures. Write 35 words or more."
                ),
                word_min=35,
                rubric=(
                    "A2 Key Writing Part 7 (max 5 marks). Award for a connected "
                    "story of 35+ words that follows the three pictures (friends "
                    "preparing food / a picnic by a lake / swimming). Reward range "
                    "of language and coherence; tolerate A2-level errors that do not "
                    "impede the story."
                ),
                grade_mode="llm",
            )
        ],
    )


# ---- Listening Part 1: 5 questions, 3 picture options each ---------------- #
_L1 = [
    (1, "What's Julia going to do tonight?", "C"),
    (2, "What time does the art lesson start?", "A"),
    (3, "What will Chloe do on Saturday?", "C"),
    (4, "How much will the girl pay for her cinema ticket?", "A"),
    (5, "Who will meet Peter at the airport?", "A"),
]


def _listening_part1() -> Section:
    items: list[Item] = []
    for q, prompt, correct in _L1:
        items.append(
            SingleChoiceItem(
                id=f"a2-2022-l1-q{q}",
                prompt=prompt,
                options=[
                    ImageOption(key="A", asset_id=f"a2-2022-l1-q{q}-a.png", alt="Picture A"),
                    ImageOption(key="B", asset_id=f"a2-2022-l1-q{q}-b.png", alt="Picture B"),
                    ImageOption(key="C", asset_id=f"a2-2022-l1-q{q}-c.png", alt="Picture C"),
                ],
                correct=correct,
            )
        )
    return Section(
        id="a2-2022-sec-l1",
        title="Listening Part 1 — Choose the correct picture",
        skill="listening",
        # First section of the listening block: it anchors the shared player and
        # carries the free intro preview before the (single-play) test track.
        stimulus=AudioAssetStimulus(
            asset_id=_AUDIO_ID, plays=1, preview_asset_id=_PREVIEW_ID
        ),
        items=items,
    )


# ---- Listening Part 2: note completion (gap fill) ------------------------- #
# Prompts carry the printed note labels; answers from the listening answer key.
_L2 = [
    (6, "School Camping Trip — Give money to: Mrs …", ["Fairford"]),
    (7, "School Camping Trip — Day of return: …", ["Friday"]),
    (8, "School Camping Trip — Time to arrive at school: … a.m.", ["7.30"]),
    (9, "School Camping Trip — Travel by: …", ["train"]),
    (10, "School Camping Trip — Bring: …", ["boots"]),
]


def _listening_part2() -> Section:
    return Section(
        id="a2-2022-sec-l2",
        title="Listening Part 2 — School Camping Trip (note completion)",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1),
        items=[
            GapFillItem(id=f"a2-2022-l2-q{q}", prompt=prompt, accepted=accepted)
            for q, prompt, accepted in _L2
        ],
    )


# ---- Listening Parts 3 & 4: 3-option multiple choice ---------------------- #
_L3 = [
    (
        11,
        "Annie saw a film at",
        [("A", "two o'clock."), ("B", "quarter past three."), ("C", "half past five.")],
        "B",
    ),
    (
        12,
        "The film was about",
        [("A", "a sports star."), ("B", "some animals."), ("C", "history.")],
        "A",
    ),
    (
        13,
        "Annie thought the film",
        [("A", "was too long."), ("B", "wasn't very interesting."), ("C", "needed better actors.")],
        "C",
    ),
    (
        14,
        "Annie's favourite film",
        [("A", "makes her laugh."), ("B", "is a true story."), ("C", "is very exciting.")],
        "B",
    ),
    (
        15,
        "Annie prefers to watch films",
        [("A", "at a cinema."), ("B", "on her laptop."), ("C", "on TV.")],
        "A",
    ),
]
_L4 = [
    (
        16,
        "You will hear a teacher talking to her class. What does the teacher want her class to do?",
        [("A", "work more quickly"), ("B", "make less noise"), ("C", "help each other more")],
        "B",
    ),
    (
        17,
        "You will hear two friends talking about their day. What have they just done?",
        [
            ("A", "They've been to a concert."),
            ("B", "They've had a meal."),
            ("C", "They've played a sport."),
        ],
        "C",
    ),
    (
        18,
        "You will hear a teacher talking to one of his students called Sarah. "
        "Why must Sarah do her homework again?",
        [
            ("A", "She made too many mistakes."),
            ("B", "She did the wrong work."),
            ("C", "She forgot to do some of it."),
        ],
        "B",
    ),
    (
        19,
        "You will hear a girl, Lara, talking about shopping. Why did Lara buy the bag?",
        [
            ("A", "The size was right."),
            ("B", "The price was right."),
            ("C", "The colour was right."),
        ],
        "A",
    ),
    (
        20,
        "You will hear a man talking to his daughter before she goes out. "
        "What's the weather like today?",
        [("A", "It's cold."), ("B", "It's wet."), ("C", "It's sunny.")],
        "B",
    ),
]


def _listening_part3() -> Section:
    return Section(
        id="a2-2022-sec-l3",
        title="Listening Part 3 — Annie and Tony talk about a film",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1),
        items=[_choice(f"a2-2022-l3-q{q}", p, o, c) for q, p, o, c in _L3],
    )


def _listening_part4() -> Section:
    return Section(
        id="a2-2022-sec-l4",
        title="Listening Part 4 — Short extracts",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1),
        items=[_choice(f"a2-2022-l4-q{q}", p, o, c) for q, p, o, c in _L4],
    )


# ---- Listening Part 5: matching (pool A-H) -------------------------------- #
# A matching section's stimulus is the A-H pool; the contract has no slot for
# audio here (one stimulus per section), so this part carries NO audio_asset.
# Flagged in the runbook/report — splitting the mp3 would let it keep the audio.
_L5_POOL = [
    ("A", "clothes"),
    ("B", "food"),
    ("C", "lights"),
    ("D", "make-up"),
    ("E", "music"),
    ("F", "photographs"),
    ("G", "posters"),
    ("H", "tickets"),
]
_L5 = [
    (21, "Anton", "E"),
    (22, "Emma", "F"),
    (23, "Karl", "G"),
    (24, "Sarah", "A"),
    (25, "George", "B"),
]


def _listening_part5() -> Section:
    return Section(
        id="a2-2022-sec-l5",
        title="Listening Part 5 — School fashion show (audio plays in Part 1's player)",
        skill="listening",
        stimulus=MatchingPoolStimulus(options=[PoolOption(key=k, text=t) for k, t in _L5_POOL]),
        items=[
            MatchingItem(
                id=f"a2-2022-l5-q{q}",
                prompt=f"What will {person} help with?",
                correct=correct,
            )
            for q, person, correct in _L5
        ],
    )


# --------------------------------------------------------------------------- #
# The whole test.
# --------------------------------------------------------------------------- #
def build_test() -> Test:
    """Assemble the A2 Key 2022 sample as a single draft test (R&W + Listening)."""
    return Test(
        id=TEST_ID,
        title="A2 Key for Schools 2022 — Sample Test",
        level="A2_KEY",
        status="draft",
        duration_minutes=95,  # R&W 60' + Listening ~35'; real exam runs them apart
        sections=[
            *_reading_part1(),
            _reading_part2(),
            _reading_part3(),
            _reading_part4(),
            _reading_part5(),
            _writing_part6(),
            _writing_part7(),
            _listening_part1(),
            _listening_part2(),
            _listening_part3(),
            _listening_part4(),
            _listening_part5(),
        ],
    )


def main() -> None:
    configure_logging(settings.log_level)
    Base.metadata.create_all(engine)
    storage = FilesystemStorage(settings.assets_dir)
    session = SessionLocal()
    try:
        test_id = load_test(session, storage, build_test(), assets())  # draft, no roster
        session.commit()
    except Exception:
        session.rollback()
        log.exception("ingest_a2_2022 failed")
        raise
    finally:
        session.close()
    log.info("ingest_a2_2022 complete: test=%s loaded as DRAFT (awaiting review)", test_id)


if __name__ == "__main__":
    main()
