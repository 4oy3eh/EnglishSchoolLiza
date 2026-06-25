"""Builder for the Cambridge YLE Movers sample test (Volume 2, manual `pdf` ingest).

Third real content ingest (Option B, see `docs/INGEST_VIA_CLAUDE_CODE.md`), built
like `ingest_a2_2022.py` / `ingest_b1_2022.py` but made child-friendly: YLE is a
picture test, so most parts carry illustrations and several are answered by
*picking a picture* rather than typing. The all-in-one PDF holds Listening +
Reading & Writing + Speaking **and** the marking keys; every `correct`/`accepted`
comes only from that PDF's Marking Key. The listening mp3 had no transcript, so it
was transcribed with `faster-whisper` (model=small); the transcript is saved next
to the audio and used for the intro/test split and to corroborate keys/pictures.

Interactive YLE tasks (decisions confirmed with the owner):
  * Listening Part 1 (draw lines names↔people) -> picture-choice "Which child is X?"
    over the owner's hand-cut pictures 1–8 (1 = John, the worked example).
  * Listening Part 3 (match days↔pictures) -> each picture is shown with a day to
    choose; the Sunday picture is the worked example.
  * Listening Part 5 (listen and colour) -> a `colour_task` (paint canvas, teacher
    reviewed) + the one writable answer ("write MAP").
  * Reading & Writing Part 1 -> pick the matching picture (no typing).
Speaking is omitted (face-to-face). Several worked-example cards / scenes are the
owner's own crops in ``assets/yle/`` (`1.jpg`..`8.jpg`, `example 5.jpg`,
`example writing 2.jpg`, `example writing 5_1..3.jpg`).

Run ``python -m app.content.ingest_movers_2022`` to (re)load the draft (no publish,
no roster — those are the post-review steps, golden rule #5).
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
    ColourTaskItem,
    GapFillItem,
    GappedTextStimulus,
    ImageOption,
    ImageSetStimulus,
    Item,
    Option,
    PassageTextStimulus,
    Section,
    SingleChoiceItem,
    Test,
    TextOption,
)

log = get_logger(__name__)

TEST_ID = "movers-vol2"

# --- source asset bytes (local, gitignored) -------------------------------- #
_CROPS = Path("var/extract/yle/crops")
_USER_PICS = Path("assets/yle")  # owner's hand-cut pictures + example scenes
_TEST_MP3 = Path("var/extract/yle/movers-listening.mp3")
_PREVIEW_MP3 = Path("var/extract/yle/movers-listening-preview.mp3")

_AUDIO_ID = "movers-listening.mp3"
_PREVIEW_ID = "movers-listening-preview.mp3"

_WORDS = ["whale", "coffee", "shoulder", "elephant", "soup", "stomach", "milk", "bat"]


# --------------------------------------------------------------------------- #
# Assets.
# --------------------------------------------------------------------------- #
_IMAGE_SOURCES: dict[str, str] = {
    # Listening Part 1: full scene + worked-example card (children come from assets/yle).
    "movers-l1-scene.png": "l1_scene.png",
    "movers-l1-example.png": "movers-l1-example.png",
    # Listening Part 2: the zoo note form.
    "movers-l2-zoo.png": "l2_zoo.png",
    # Listening Part 3: 6 activity pictures + the Sunday worked-example card.
    **{f"movers-l3-p{n}.png": f"l3_p{n}.png" for n in range(1, 7)},
    "movers-l3-example.png": "movers-l3-example.png",
    # Listening Part 4: 15 option pictures (the example is the owner's image below).
    **{f"movers-l4-q{q}-{c}.png": f"l4_q{q}_{c}.png" for q in range(1, 6) for c in ("a", "b", "c")},
    # Listening Part 5: line-art scene to colour (transparent background).
    "movers-l5-colour.png": "l5_colour_line.png",
    # Reading & Writing Part 1: 8 labelled word-pictures (the answer options).
    **{f"movers-rw1-{w}.png": f"rw1_{w}.png" for w in _WORDS},
    # Reading & Writing Part 4: the word-box pictures.
    "movers-rw4-wordbox.png": "rw4_wordbox.png",
}

# Owner-provided files in assets/yle (kept verbatim).
_USER_FILES: dict[str, str] = {
    "movers-l4-example.jpg": "example 5.jpg",  # tick-box example with C clearly ✔
    "movers-rw2-scene.jpg": "example writing 2.jpg",  # the bathroom scene
    "movers-rw5-story1.jpg": "example writing 5_1.jpg",  # cinema / sharks
    "movers-rw5-story2.jpg": "example writing 5_2.jpg",  # beach, ball
    "movers-rw5-story3.jpg": "example writing 5_3.jpg",  # swimming
}


def assets() -> dict[str, tuple[bytes, str | None]]:
    """All blobs the test references, keyed by asset_id."""
    out: dict[str, tuple[bytes, str | None]] = {}
    for asset_id, fname in _IMAGE_SOURCES.items():
        path = _CROPS / fname
        if not path.is_file():
            raise FileNotFoundError(f"missing crop {path} (re-run the ingest crop step)")
        out[asset_id] = (path.read_bytes(), "image/png")
    # Listening Part 1 children: the owner's hand-cut pictures 1.jpg .. 8.jpg.
    for n in range(1, 9):
        pic = _USER_PICS / f"{n}.jpg"
        if not pic.is_file():
            raise FileNotFoundError(f"missing L1 child picture {pic}")
        out[f"movers-l1-p{n}.jpg"] = (pic.read_bytes(), "image/jpeg")
    # Owner-provided example scenes.
    for asset_id, fname in _USER_FILES.items():
        pic = _USER_PICS / fname
        if not pic.is_file():
            raise FileNotFoundError(f"missing owner image {pic}")
        out[asset_id] = (pic.read_bytes(), "image/jpeg")
    for asset_id, path in ((_PREVIEW_ID, _PREVIEW_MP3), (_AUDIO_ID, _TEST_MP3)):
        if not path.is_file():
            raise FileNotFoundError(f"missing audio {path} (re-run the mp3 split step)")
        out[asset_id] = (path.read_bytes(), "audio/mpeg")
    return out


# --------------------------------------------------------------------------- #
# Helpers. Item order within a section is the printed order (rule #7).
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


_YESNO = [("yes", "yes"), ("no", "no")]


# --------------------------------------------------------------------------- #
# LISTENING Part 1 — "Which child is X?" over the owner's pictures 1-8.
# Picture 1 = John (worked example). 3=Daisy, 4=Sally, 5=Peter, 7=Jim, 8=Jane;
# 2 & 6 are distractors.
# --------------------------------------------------------------------------- #
_L1_OPTS = [(chr(ord("A") + i), f"movers-l1-p{i + 1}.jpg") for i in range(8)]
_L1 = [
    (1, "Sally", "D"),  # picture 4
    (2, "Peter", "E"),  # picture 5
    (3, "Daisy", "C"),  # picture 3
    (4, "Jim", "G"),  # picture 7
    (5, "Jane", "H"),  # picture 8
]


def _listening_part1() -> Section:
    kid_options: list[Option] = [ImageOption(key=k, asset_id=a, alt="a child") for k, a in _L1_OPTS]
    items: list[Item] = [
        SingleChoiceItem(
            id=f"movers-l1-q{q}", prompt=f"Which child is {name}?", options=kid_options, correct=c
        )
        for q, name, c in _L1
    ]
    return Section(
        id="movers-sec-l1",
        title="Listening Part 1 — Who is who? (find each child)",
        skill="listening",
        stimulus=AudioAssetStimulus(
            asset_id=_AUDIO_ID,
            plays=1,
            preview_asset_id=_PREVIEW_ID,
            images=["movers-l1-scene.png", "movers-l1-example.png"],
        ),
        items=items,
    )


# --------------------------------------------------------------------------- #
# LISTENING Part 2 — note completion ("The Zoo"); the form shown as a picture.
# --------------------------------------------------------------------------- #
_L2 = [
    (
        1,
        "THE ZOO — How many kinds of animals:",
        ["30", "thirty", "30 kinds of animals", "thirty kinds of animals"],
    ),
    (2, "THE ZOO — Biggest animal:", ["elephant", "elephants", "the elephant", "the elephants"]),
    (3, "THE ZOO — Favourite animal:", ["parrot", "parrots", "the parrot", "the parrots"]),
    (4, "THE ZOO — Favourite animal's food:", ["fruit", "all kinds of fruit", "some fruit"]),
    (5, "THE ZOO — Name of zoo: ____ Zoo", ["wild"]),
]


def _listening_part2() -> Section:
    return Section(
        id="movers-sec-l2",
        title="Listening Part 2 — The Zoo (listen and write)",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1, images=["movers-l2-zoo.png"]),
        items=[
            GapFillItem(id=f"movers-l2-q{q}", prompt=prompt, accepted=acc) for q, prompt, acc in _L2
        ],
    )


# --------------------------------------------------------------------------- #
# LISTENING Part 3 — each activity picture is shown; choose the day.
# Sunday (the kite picture) is the worked example, shown above as a card.
# day->picture from the transcript+key (Tuesday is an unused distractor).
# --------------------------------------------------------------------------- #
_L3_DAYS = [
    ("A", "Monday"),
    ("B", "Tuesday"),
    ("C", "Wednesday"),
    ("D", "Thursday"),
    ("E", "Friday"),
    ("F", "Saturday"),
]
_L3 = [
    (1, "movers-l3-p1.png", "D"),  # bikes -> Thursday
    (2, "movers-l3-p2.png", "A"),  # car (drove to town) -> Monday
    (3, "movers-l3-p3.png", "F"),  # family walk -> Saturday
    (4, "movers-l3-p4.png", "E"),  # dogs in the park -> Friday
    (5, "movers-l3-p6.png", "C"),  # walked to the village shop -> Wednesday
]


def _listening_part3() -> Section:
    items: list[Item] = [
        SingleChoiceItem(
            id=f"movers-l3-q{q}",
            prompt="When did Sally do this? Choose the day.",
            image=pic,
            options=[TextOption(key=k, text=t) for k, t in _L3_DAYS],
            correct=c,
        )
        for q, pic, c in _L3
    ]
    return Section(
        id="movers-sec-l3",
        title="Listening Part 3 — Sally's week (choose the day for each picture)",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1, images=["movers-l3-example.png"]),
        items=items,
    )


# --------------------------------------------------------------------------- #
# LISTENING Part 4 — listen and tick the box (3 picture options). Key 1B2B3A4C5C.
# --------------------------------------------------------------------------- #
_L4 = [
    (1, "Where did the rabbits in the film go?", "B"),
    (2, "Where did the children have their lunch?", "B"),
    (3, "What did the children eat?", "A"),
    (4, "What did the children do after lunch?", "C"),
    (5, "What did Jim's friends give him?", "C"),
]


def _listening_part4() -> Section:
    items: list[Item] = [
        SingleChoiceItem(
            id=f"movers-l4-q{q}",
            prompt=prompt,
            options=[
                ImageOption(key="A", asset_id=f"movers-l4-q{q}-a.png", alt="Picture A"),
                ImageOption(key="B", asset_id=f"movers-l4-q{q}-b.png", alt="Picture B"),
                ImageOption(key="C", asset_id=f"movers-l4-q{q}-c.png", alt="Picture C"),
            ],
            correct=correct,
        )
        for q, prompt, correct in _L4
    ]
    return Section(
        id="movers-sec-l4",
        title="Listening Part 4 — Listen and tick the box",
        skill="listening",
        # Worked example (Where did Jim see the film? -> C ✔) — the owner's image.
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1, images=["movers-l4-example.jpg"]),
        items=items,
    )


# --------------------------------------------------------------------------- #
# LISTENING Part 5 — listen and colour (+ one written answer).
# --------------------------------------------------------------------------- #
def _listening_part5() -> Section:
    return Section(
        id="movers-sec-l5",
        title="Listening Part 5 — Listen and colour",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1),
        items=[
            ColourTaskItem(
                id="movers-l5-q1",
                prompt="Listen and colour the picture. Use the colours the teacher says.",
                asset_id="movers-l5-colour.png",
                palette=["blue", "green", "red", "brown"],
                key=(
                    "Colour the clock blue; the star on the boy's sweater green; the "
                    "comic on the desk red; the eraser on the desk brown."
                ),
            ),
            GapFillItem(
                id="movers-l5-q2",
                prompt="Write the word the teacher says under the map on the wall.",
                accepted=["map"],
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# READING & WRITING Part 1 — read a definition, PICK the matching picture.
# Options A-H = the 8 labelled word-pictures (coffee + bat are distractors).
# --------------------------------------------------------------------------- #
_RW1_OPTS = [(chr(ord("A") + i), f"movers-rw1-{w}.png") for i, w in enumerate(_WORDS)]
# whale=A coffee=B shoulder=C elephant=D soup=E stomach=F milk=G bat=H
_RW1 = [
    (1, "You can eat this from a bowl. Sometimes there are vegetables in it.", "E"),  # soup
    (2, "This is the biggest animal in the world. It lives in the sea.", "A"),  # whale
    (3, "This is part of your body. All your food and drink goes here first.", "F"),  # stomach
    (4, "This big animal lives in hot countries and eats leaves and grass.", "D"),  # elephant
    (5, "This is between your neck and your arm.", "C"),  # shoulder
    (6, "Mothers give this white drink to their babies.", "G"),  # milk
]


def _reading_part1() -> Section:
    word_options: list[Option] = [
        ImageOption(key=k, asset_id=a, alt="a picture") for k, a in _RW1_OPTS
    ]
    items: list[Item] = [
        SingleChoiceItem(id=f"movers-rw1-q{q}", prompt=prompt, options=word_options, correct=c)
        for q, prompt, c in _RW1
    ]
    return Section(
        id="movers-sec-rw1",
        title="Reading & Writing Part 1 — Read and choose the picture",
        skill="reading",
        stimulus=PassageTextStimulus(
            text=(
                "Look and read. Choose the correct picture for each one.\n"
                "Example: This animal can fly and it comes out at night. → a bat"
            )
        ),
        items=items,
    )


# --------------------------------------------------------------------------- #
# READING & WRITING Part 2 — look at the picture, write yes/no.
# --------------------------------------------------------------------------- #
_RW2 = [
    (1, "A big brown bear is having a shower.", "yes"),
    (2, "There are some glasses below the mirror.", "yes"),
    (3, "The yellow bear is fatter than the blue bear.", "no"),
    (4, "There are four toys in the bath.", "yes"),
    (5, "There are lots of boxes in the cupboard.", "no"),
    (6, "The floor is wet and there is a toothbrush on it.", "yes"),
]


def _reading_part2() -> Section:
    return Section(
        id="movers-sec-rw2",
        title="Reading & Writing Part 2 — Write yes or no",
        skill="reading",
        stimulus=ImageSetStimulus(asset_ids=["movers-rw2-scene.jpg"]),
        items=[_choice(f"movers-rw2-q{q}", stmt, _YESNO, c) for q, stmt, c in _RW2],
    )


# --------------------------------------------------------------------------- #
# READING & WRITING Part 3 — dialogue, choose the best response (example shown).
# --------------------------------------------------------------------------- #
_RW3_INTRO = (
    "Read the text and choose the best answer. Peter is talking to his friend Jane.\n\n"
    "Example (0):\n"
    "Jane: Hello, Peter. How are you?\n"
    "Peter:  (A) I'm not very well.  ✔   (B) I'm John's cousin.   (C) I'm going outside."
)
_RW3 = [
    (
        1,
        "What's the matter? Have you got a headache?",
        [
            ("A", "No, thank you. I don't want one."),
            ("B", "No, I've got toothache."),
            ("C", "No, I haven't got it."),
        ],
        "B",
    ),
    (
        2,
        "Would you like to come to my house?",
        [
            ("A", "Yes, I went home quickly."),
            ("B", "No, thanks. I want to go home."),
            ("C", "Well, I like my house a lot."),
        ],
        "B",
    ),
    (
        3,
        "Have you got a coat?",
        [("A", "Yes, it does."), ("B", "OK, he's here."), ("C", "No, I haven't.")],
        "C",
    ),
    (
        4,
        "Do you want a drink of water?",
        [("A", "Yes, please."), ("B", "Yes, it is."), ("C", "Yes, I had.")],
        "A",
    ),
    (
        5,
        "Shall I walk home with you?",
        [
            ("A", "He can walk there."),
            ("B", "I'd like that, thanks."),
            ("C", "I can go with her this evening."),
        ],
        "B",
    ),
    (
        6,
        "Is your mum at home?",
        [
            ("A", "It's his new home."),
            ("B", "Next to the bus station."),
            ("C", "Only my dad's there today."),
        ],
        "C",
    ),
]


def _reading_part3() -> Section:
    return Section(
        id="movers-sec-rw3",
        title="Reading & Writing Part 3 — Peter and Jane",
        skill="reading",
        stimulus=PassageTextStimulus(text=_RW3_INTRO),
        items=[_choice(f"movers-rw3-q{q}", f"Jane: {p}", o, c) for q, p, o, c in _RW3],
    )


# --------------------------------------------------------------------------- #
# READING & WRITING Part 4 — story cloze from a word box (pictures) + the title.
# --------------------------------------------------------------------------- #
_RW4_TEXT = (
    "Read the story. Choose a word from the box (shown in the pictures) and write "
    "the correct word next to numbers 1–6.\n\n"
    "My name is Daisy. I like toys, but I like books and comics best. I love stories "
    "about men on the moon and about (1) ......... who live in different countries.\n\n"
    "I read a good story yesterday. In this story, a boy climbed a (2) ......... . At "
    "the top, there was a lot of snow. It was evening, but the boy could see the "
    "forest below him.\n\n"
    "He (3) ......... down on a rock to have a drink and to look up at all the "
    "(4) ......... .\n\n"
    "But then he (5) ......... something that he didn't understand. Something very big "
    "and round flew quietly and quickly behind a cloud. What was it? The boy didn't "
    "know and he didn't wait to see it again. He (6) ......... home to his village "
    "because he was very afraid.\n\n"
    "I wasn't afraid! I enjoyed the story a lot!"
)
_RW4 = [
    (1, ["children"]),
    (2, ["mountain"]),
    (3, ["sat"]),
    (4, ["stars"]),
    (5, ["saw"]),
    (6, ["ran"]),
]


def _reading_part4() -> Section:
    items: list[Item] = [
        GapFillItem(id=f"movers-rw4-q{q}", prompt=f"Write the word for gap {q}.", accepted=acc)
        for q, acc in _RW4
    ]
    items.append(
        SingleChoiceItem(
            id="movers-rw4-q7",
            prompt="Now choose the best name for the story.",
            options=[
                TextOption(key="A", text="A boy that Daisy knows"),
                TextOption(key="B", text="A film that Daisy watched"),
                TextOption(key="C", text="A story that Daisy liked"),
            ],
            correct="C",
        )
    )
    return Section(
        id="movers-sec-rw4",
        title="Reading & Writing Part 4 — Daisy's story",
        skill="reading",
        stimulus=GappedTextStimulus(text=_RW4_TEXT, images=["movers-rw4-wordbox.png"]),
        items=items,
    )


# --------------------------------------------------------------------------- #
# READING & WRITING Part 5 — complete the story (1–3 words). Split into the three
# paragraphs so each illustration sits with its own questions.
# --------------------------------------------------------------------------- #
_RW5_PARA1 = (
    "A family holiday\n\n"
    "Vicky lives with her parents and her two brothers, Sam and Paul, in the city. "
    "Last week, they had a holiday by the sea. Sam is ten, Vicky is eight but Paul is "
    "only five. They went to the cinema on Wednesday because it rained all day. They "
    "saw a film about sharks. The sharks had very big teeth. Paul didn't like watching "
    "them and he closed his eyes.\n\n"
    "Examples: Vicky's family went on holiday last (week). Vicky has two (brothers) "
    "who are called Sam and Paul."
)
_RW5_PARA2 = (
    "On Thursday, Paul thought about the film. He didn't want to swim in the sea. He "
    "sat on the beach and watched Sam and Vicky. They played in the water. Mum gave "
    "Paul an ice cream but he didn't want it. Then Dad said, \"Come on Paul! Let's go "
    "for a swim.\" But Paul didn't want to."
)
_RW5_PARA3 = (
    "On Friday, the family ate breakfast in the garden because it was very sunny but "
    "Paul didn't want any. Then they all went to the beach again. The sea was very "
    "blue. Paul looked. There were three beautiful dolphins in the water! He ran to "
    "the sea and swam to them. Then Paul's dad threw a ball in the sea and the "
    "dolphins played with it. It was great and Paul stopped thinking about the sharks "
    "in the film. That evening, all the family went to the cinema again. This time the "
    "film was about a funny dolphin and they all enjoyed it."
)
_RW5 = {
    1: ("The family had a holiday by ____.", ["the sea", "the seaside"]),
    2: ("It ____ all day on Wednesday and the family went to the cinema.", ["rained"]),
    3: ("Paul didn't enjoy seeing ____ in the film.", ["the sharks", "the sharks' teeth"]),
    4: ("Sam and Vicky ____ in the sea.", ["played"]),
    5: ("Paul didn't want the ice cream that his ____ gave him.", ["mum", "mother", "mummy"]),
    6: ("Dad wanted to go for ____ with Paul.", ["a swim"]),
    7: ("The family had breakfast in ____ on Friday.", ["the garden", "their garden"]),
    8: (
        "Paul saw ____ in the water.",
        [
            "dolphins",
            "three dolphins",
            "some dolphins",
            "three beautiful dolphins",
            "some beautiful dolphins",
        ],
    ),
    9: ("Paul's dad ____ into the water.", ["threw a ball"]),
    # Key prints "the/Peter's family" but no "Peter" is in this story (apparent typo).
    10: ("All ____ enjoyed another film at the cinema on Friday evening.", ["the family"]),
}


def _reading_part5_sections() -> list[Section]:
    plan = [
        ("a", _RW5_PARA1, "movers-rw5-story1.jpg", [1, 2, 3]),
        ("b", _RW5_PARA2, "movers-rw5-story2.jpg", [4, 5, 6]),
        ("c", _RW5_PARA3, "movers-rw5-story3.jpg", [7, 8, 9, 10]),
    ]
    sections = []
    for suffix, text, img, qs in plan:
        sections.append(
            Section(
                id=f"movers-sec-rw5{suffix}",
                title="Reading & Writing Part 5 — A family holiday",
                skill="reading",
                stimulus=PassageTextStimulus(text=text, images=[img]),
                items=[
                    GapFillItem(id=f"movers-rw5-q{q}", prompt=_RW5[q][0], accepted=_RW5[q][1])
                    for q in qs
                ],
            )
        )
    return sections


# --------------------------------------------------------------------------- #
# READING & WRITING Part 6 — cloze, choose the right word.
# --------------------------------------------------------------------------- #
_RW6_TEXT = (
    "Cats\n\n"
    "Cats (example: have) good eyes. They can see very well at night. (1) ......... "
    "cats climb trees and eat meat. They can move very quietly and catch animals. "
    "Then they eat them. They have strong teeth. There (2) ......... small cats and "
    "big cats like lions and tigers. Only tigers live (3) ......... the jungle. Lions "
    "don't. Some people go and see lions and tigers at the zoo. A lot of people have "
    "small cats in (4) ......... homes. These cats are pets. People (5) ......... them "
    "because they are beautiful."
)
_RW6 = [
    (1, [("A", "All"), ("B", "Every"), ("C", "Any")], "A"),
    (2, [("A", "am"), ("B", "are"), ("C", "is")], "B"),
    (3, [("A", "at"), ("B", "on"), ("C", "in")], "C"),
    (4, [("A", "your"), ("B", "their"), ("C", "our")], "B"),
    (5, [("A", "like"), ("B", "liking"), ("C", "likes")], "A"),
]


def _reading_part6() -> Section:
    return Section(
        id="movers-sec-rw6",
        title="Reading & Writing Part 6 — Cats",
        skill="reading",
        stimulus=GappedTextStimulus(text=_RW6_TEXT),
        items=[
            _choice(f"movers-rw6-q{q}", f"Choose the word for gap {q}.", o, c) for q, o, c in _RW6
        ],
    )


# --------------------------------------------------------------------------- #
# The whole test.
# --------------------------------------------------------------------------- #
def build_test() -> Test:
    """Assemble the Movers parts as one draft test (Listening + Reading & Writing)."""
    return Test(
        id=TEST_ID,
        title="YLE Movers 2022 — Sample Test (Vol. 2)",
        level="A1_MOVERS",
        status="draft",
        duration_minutes=55,  # Listening ~25' + Reading & Writing 30'; run apart
        sections=[
            _listening_part1(),
            _listening_part2(),
            _listening_part3(),
            _listening_part4(),
            _listening_part5(),
            _reading_part1(),
            _reading_part2(),
            _reading_part3(),
            _reading_part4(),
            *_reading_part5_sections(),
            _reading_part6(),
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
        log.exception("ingest_movers_2022 failed")
        raise
    finally:
        session.close()
    log.info("ingest_movers_2022 complete: test=%s loaded as DRAFT (awaiting review)", test_id)


if __name__ == "__main__":
    main()
