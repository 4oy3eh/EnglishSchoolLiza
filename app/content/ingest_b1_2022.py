"""Builder for the B1 Preliminary for Schools 2022 sample test (manual `pdf` ingest).

Second real content ingest (Option B, see `docs/INGEST_VIA_CLAUDE_CODE.md`),
built exactly like `app/content/ingest_a2_2022.py`. Claude Code read the Cambridge
sample PDFs with the `pdf` skill, transcribed the question paper, cropped the
sign/picture images, and took every `correct`/`accepted` value **only** from the
answer-key PDF, then loads the result as a **draft** (golden rule #5).

Year-mismatch note (runbook §1): the QPs are dated 2022 but the answer keys 2020.
Before trusting them, every answer was verified against the 2022 QP content (and,
for listening, the transcript) — they share component **D243** and align 1:1, so
the "2020 keys" are the correct keys for these 2022 papers. See the ingest report.

Provenance of the source bytes (local, gitignored — runbook §1):

* sign / picture crops: rendered + cropped from the QP PDFs into
  ``var/extract/b1/crops/`` during the ingest; read back here by filename.
* listening audio: ``assets/b1/Preliminary for Schools PB Sample Test.mp3``, split
  (lossless) into an intro ``preview`` + the test track under ``var/extract/b1/``.

Run ``python -m app.content.ingest_b1_2022`` to (re)load the draft. It does NOT
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

TEST_ID = "b1-2022"

# --- where the source asset bytes live (local, gitignored) ----------------- #
_CROPS = Path("var/extract/b1/crops")
_TEST_MP3 = Path("var/extract/b1/b1-2022-listening.mp3")
_PREVIEW_MP3 = Path("var/extract/b1/b1-2022-listening-preview.mp3")

_AUDIO_ID = "b1-2022-listening.mp3"
_PREVIEW_ID = "b1-2022-listening-preview.mp3"


# --------------------------------------------------------------------------- #
# Assets.
# --------------------------------------------------------------------------- #
def _png(path: Path) -> tuple[bytes, str]:
    if not path.is_file():
        raise FileNotFoundError(f"missing crop {path} (re-run the ingest crop step)")
    return path.read_bytes(), "image/png"


def assets() -> dict[str, tuple[bytes, str | None]]:
    """All blobs the test references, keyed by the asset_id used in the contract."""
    out: dict[str, tuple[bytes, str | None]] = {}
    # Reading Part 1 signs (one per question).
    for q in range(1, 6):
        out[f"b1-2022-r1-q{q}-sign.png"] = _png(_CROPS / f"r_p1_q{q}.png")
    # Listening Part 1 picture options (3 per question, 7 questions).
    for q in range(1, 8):
        for c in ("a", "b", "c"):
            out[f"b1-2022-l1-q{q}-{c}.png"] = _png(_CROPS / f"l_p1_q{q}_{c}.png")
    # Listening audio: a free intro preview + the test track (runbook §8/§10).
    for asset_id, path in ((_PREVIEW_ID, _PREVIEW_MP3), (_AUDIO_ID, _TEST_MP3)):
        if not path.is_file():
            raise FileNotFoundError(f"missing audio {path} (re-run the mp3 split step)")
        out[asset_id] = (path.read_bytes(), "audio/mpeg")
    return out


# --------------------------------------------------------------------------- #
# Section builders. Item order within a section is the printed order (rule #7);
# every `correct`/`accepted` comes from the answer-key PDF.
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


# ---- Reading Part 1: five signs/notices, one single-item section each ------ #
_R1 = [
    (
        1,
        [
            ("A", "All campers must reserve a place in advance."),
            ("B", "Groups bigger than four are not allowed on this site."),
            ("C", "Groups of more than three should contact the campsite before arriving."),
        ],
        "C",
    ),
    (
        2,
        [
            ("A", "Those who don't pay punctually won't be able to go to Oxford."),
            ("B", "There are very few places left on the Oxford trip."),
            ("C", "This is the last chance for students to register for the Oxford trip."),
        ],
        "A",
    ),
    (
        3,
        [
            ("A", "You must have signed permission to take part in sports day."),
            ("B", "You have to limit the number of sports day races you take part in."),
            ("C", "You need to write your name here to get more information about sports day."),
        ],
        "B",
    ),
    (
        4,
        [
            ("A", "It is essential to have more actors even if they haven't acted before."),
            (
                "B",
                "It is important for all actors to have training before being "
                "involved in the play.",
            ),
            ("C", "It is necessary to find a new director to train the actors."),
        ],
        "A",
    ),
    (
        5,
        [
            ("A", "Students must write detailed notes on this week's experiment."),
            ("B", "Students should check that their work last term was done accurately."),
            ("C", "Students need to look at previous work while doing an experiment."),
        ],
        "C",
    ),
]


def _reading_part1() -> list[Section]:
    return [
        Section(
            id=f"b1-2022-sec-r1-q{q}",
            title=f"Reading Part 1 — Question {q}",
            skill="reading",
            stimulus=ImageSetStimulus(asset_ids=[f"b1-2022-r1-q{q}-sign.png"]),
            items=[_choice(f"b1-2022-r1-q{q}", "Choose the correct answer.", opts, c)],
        )
        for q, opts, c in _R1
    ]


# ---- Reading Part 2: match 5 people to 8 cycling courses (pool A-H) -------- #
# True matching: the 8 self-contained course descriptions are the pool options,
# the 5 people are the matching items (runbook §8 — fits MatchingPoolStimulus).
_R2_COURSES = [
    (
        "A",
        "Two Wheels Good! Mountains! Rivers! Forests! Our 'off-road' course offers you "
        "the chance to get out of the city. You'll need very good cycling skills and "
        "confidence. You will be with others of the same ability. Expert advice on "
        "keeping your bike in good condition also included. Mondays 2.00 pm–6.00 pm or "
        "Fridays 3.00 pm–7.00 pm.",
    ),
    (
        "B",
        "On Your Bike! Can't ride a bike yet, but really want to? Don't worry. Our "
        "beginners-only group (4–10 pupils per group) is just what you're looking for. "
        "Excellent teaching in safe surroundings. Makes learning to cycle fun, exciting "
        "and easy. Mondays 9.00 am–11.00 am and Thursdays 2.00 pm–4.00 pm.",
    ),
    (
        "C",
        "Fun and Games. Do you want some adventure? Find out how to do 'wheelies' "
        "(riding on one wheel), 'rampers' (cycling off low walls), 'spins' and much "
        "more... We offer a secure practice ground, excellent trainers and loads of fun "
        "equipment. Wear suitable clothes. Only for advanced cyclists. (Age 11–12) "
        "Saturdays 1.00 pm–4.00 pm.",
    ),
    (
        "D",
        "Pedal Power. A course for able cyclists. We specialise in teaching riders of "
        "all ages how to manage difficult situations in heavy traffic in towns and "
        "cities. We guarantee that by the end of the course, no roundabout or crossroads "
        "will worry you! Saturdays 2.00 pm–4.00 pm.",
    ),
    (
        "E",
        "Cycling 4 U. Not a beginner, but need plenty of practice? This course offers "
        "practical help with the basics of balancing and using your brakes safely. "
        "You'll be in a group of pupils of the same level. Improve your cycling skills "
        "and enjoy yourself at the same time! Open to all children up to the age of ten. "
        "Sundays 10.00 am–12.00 pm.",
    ),
    (
        "F",
        "Bike Doctors. Have you been doing too many tricks on your bike? Taken it up "
        "mountains and through rivers? Then it probably needs some tender loving care. "
        "Bike Doctors teach you to maintain and repair your bike. (Some basic equipment "
        "required.) Ages 11–19 Tuesdays 9.00 am–12.00 pm or Wednesdays 3.00 pm–6.00 pm.",
    ),
    (
        "G",
        "Safety First. We teach cycling safety for the city centre and country lane "
        "biker. We'll teach you the skills you need to deal with all the vehicles using "
        "our busy roads. All ages welcome from 10+. Thursdays 9.00 am–11.00 am.",
    ),
    (
        "H",
        "Setting Out. A course for absolute beginners needing one-to-one instruction to "
        "get off to a perfect start. We also give advice on helmets, lights, what to "
        "wear and much more. A fantastic introduction to cycling! Mondays and Tuesdays "
        "9.00 am–11.00 am.",
    ),
]
_R2 = [
    (
        6,
        "Nancy is fourteen and cycles quite well. She needs to learn how to cycle safely "
        "from her home to school on busy city roads. She's only free at the weekends.",
        "D",
    ),
    (
        7,
        "Markus is an excellent cyclist and he wants the excitement of riding on "
        "countryside and woodland tracks. He'd also like to learn more about looking "
        "after his bike. He can't attend a morning course.",
        "A",
    ),
    (
        8,
        "Ellie is nine and knows how to ride her bike, but isn't confident about starting "
        "and stopping. She'd love to meet other cyclists with a similar ability and have "
        "fun with them.",
        "E",
    ),
    (
        9,
        "Leo can't cycle yet, and wants to learn on his own with the teacher. He'd prefer "
        "a course with sessions twice a week. He'd also like some practical information "
        "about cycling clothes and equipment.",
        "H",
    ),
    (
        10,
        "Josh is eleven and a skilled cyclist. He's keen to learn to do exciting cycling "
        "tricks in a safe environment. He'd like to be with people of a similar age.",
        "C",
    ),
]


def _reading_part2() -> Section:
    return Section(
        id="b1-2022-sec-r2",
        title="Reading Part 2 — Cycling courses",
        skill="reading",
        stimulus=MatchingPoolStimulus(options=[PoolOption(key=k, text=t) for k, t in _R2_COURSES]),
        items=[
            MatchingItem(id=f"b1-2022-r2-q{q}", prompt=person, correct=c) for q, person, c in _R2
        ],
    )


# ---- Reading Part 3: long text, 4-option questions ------------------------ #
_R3_PASSAGE = (
    "Play to win\n"
    "16-year-old Harry Moore writes about his hobby, tennis.\n\n"
    "My parents have always loved tennis and they're members of a tennis club. My "
    "older brother was really good at it and they supported him — taking him to "
    "lessons all the time. So I guess when I announced that I wanted to be a tennis "
    "champion when I grew up I just intended for them to notice me. My mother laughed. "
    "She knew I couldn't possibly be serious, I was just a 4-year-old kid!\n\n"
    "Later, I joined the club's junior coaching group and eventually took part in my "
    "first proper contest, confident that my team would do well. We won, which was "
    "fantastic, but I wasn't so successful. I didn't even want to be in the team photo "
    "because I didn't feel I deserved to be. When my coach asked what happened in my "
    "final match, I didn't know what to say. I couldn't believe I'd lost — I knew I "
    "was the better player. But every time I attacked, the other player defended "
    "brilliantly. I couldn't explain the result.\n\n"
    "After that, I decided to listen more carefully to my coach because he had lots of "
    "tips. I realised that you need the right attitude to be a winner. On court I have "
    "a plan but sometimes the other guy will do something unexpected so I'll change it. "
    "If I lose a point, I do my best to forget it and find a way to win the next one.\n\n"
    "At tournaments, it's impossible to avoid players who explode in anger. Lots of "
    "players can be negative — including myself sometimes. Once I got so angry that I "
    "nearly broke my racket! But my coach has helped me develop ways to control those "
    "feelings. After all, the judges have a hard job and you just have to accept their "
    "decisions.\n\n"
    "My coach demands that I train in the gym to make sure I'm strong right to the end "
    "of a tournament. I'm getting good results: my shots are more accurate and I'm "
    "beginning to realise that with hard work there's a chance that I could be a "
    "champion one day."
)
_R3 = [
    (
        11,
        "Harry thinks he said that he was going to be a tennis champion in order to",
        [
            ("A", "please his parents."),
            ("B", "get some attention."),
            ("C", "annoy his older brother."),
            ("D", "persuade people that he was serious."),
        ],
        "B",
    ),
    (
        12,
        "How did Harry feel after his first important competition?",
        [
            ("A", "confused about his defeat."),
            ("B", "proud to be a member of the winning team."),
            ("C", "ashamed of the way he treated another player."),
            ("D", "amazed that he had got so far in the tournament."),
        ],
        "A",
    ),
    (
        13,
        "What does Harry try to remember when he's on the court?",
        [
            ("A", "Don't let the other player surprise you."),
            ("B", "Follow your game plan."),
            ("C", "Respect the other player."),
            ("D", "Don't keep thinking about your mistakes."),
        ],
        "D",
    ),
    (
        14,
        "What does Harry say about his behaviour in tournaments?",
        [
            ("A", "He broke his racket once when he was angry."),
            ("B", "He stays away from players who behave badly."),
            ("C", "He tries to keep calm during the game."),
            ("D", "He found it difficult to deal with one judge's decisions."),
        ],
        "C",
    ),
    (
        15,
        "What might a sports journalist write about Harry now?",
        [
            (
                "A",
                "Harry needs to believe in his own abilities and stop depending on "
                "good luck when he plays.",
            ),
            (
                "B",
                "Harry has really grown up since his first tournament and discovered "
                "that tennis is a battle of minds not just rackets.",
            ),
            (
                "C",
                "Harry looked exhausted when he finished his last match so maybe he "
                "should think about working out.",
            ),
            (
                "D",
                "Harry could be a great player but he needs to find a coach to take "
                "him all the way to the big competitions.",
            ),
        ],
        "B",
    ),
]


def _reading_part3() -> Section:
    return Section(
        id="b1-2022-sec-r3",
        title="Reading Part 3 — Play to win",
        skill="reading",
        stimulus=PassageTextStimulus(text=_R3_PASSAGE),
        items=[_choice(f"b1-2022-r3-q{q}", p, o, c) for q, p, o, c in _R3],
    )


# ---- Reading Part 4: gapped text, choose the missing sentence (A-H) -------- #
_R4_TEXT = (
    "Planting trees\n"
    "by Mark Rotheram, aged 13\n\n"
    "This spring, our teacher suggested we should get involved in a green project and "
    "plant some trees around the school. Everyone thought it was a great idea, so we "
    "started looking online for the best trees to buy. (16) ......... If we wanted them "
    "to grow properly, they had to be the right type — but there were so many different "
    "ones available! So our teacher suggested that we should look for trees that grew "
    "naturally in our part of the world. (17) ......... They'd also be more suitable for "
    "the wildlife here.\n\n"
    "Then we had to think about the best place for planting the trees. We learnt that "
    "trees are happiest where they have room to grow, with plenty of space for their "
    "branches. The trees might get damaged close to the school playgrounds, for "
    "example. (18) ......... Finally, we found a quiet corner close to the school garden "
    "— perfect!\n\n"
    "Once we'd planted the trees, we knew we had to look after them carefully. We all "
    "took turns to check the leaves regularly and make sure they had no strange spots "
    "or marks on them. (19) ......... And we decided to check the following spring in "
    "case the leaves turned yellow too soon, as that could also mean the tree was "
    "sick.\n\n"
    "We all knew that we wouldn't be at the school anymore by the time the trees grew "
    "tall, and that was a bit sad. But we'd planted the trees to benefit not only the "
    "environment, but also future students at the school. (20) ......... And that "
    "thought really cheered us up!"
)
# The shared A-H option list (three are extra / unused).
_R4_OPTIONS = [
    ("A", "So we tried to avoid areas where students were very active."),
    ("B", "However, our parents did offer to help with the digging!"),
    ("C", "That could mean the tree had a disease."),
    ("D", "But we soon found that choosing trees was quite complicated."),
    ("E", "It can be quite good for young trees, though."),
    ("F", "We knew they'd get as much pleasure from them as we had."),
    ("G", "But at least we were doing it in the right season."),
    ("H", "That way, the trees would be used to local conditions."),
]
_R4 = [(16, "D"), (17, "H"), (18, "A"), (19, "C"), (20, "F")]


def _reading_part4() -> Section:
    # The gapped passage + the A-H sentence list are shown ONCE (the pool); each
    # gap is answered by picking a letter (no per-gap re-reading, fixed A-H order).
    return Section(
        id="b1-2022-sec-r4",
        title="Reading Part 4 — Planting trees",
        skill="reading",
        stimulus=MatchingPoolStimulus(
            text=_R4_TEXT,
            options=[PoolOption(key=k, text=t) for k, t in _R4_OPTIONS],
        ),
        items=[MatchingItem(id=f"b1-2022-r4-q{q}", prompt=f"Gap {q}", correct=c) for q, c in _R4],
    )


# ---- Reading Part 5: cloze, 4-option questions ---------------------------- #
_R5_TEXT = (
    "This car runs on chocolate!\n\n"
    "Scientists have built a 300kph racing car that uses chocolate as a fuel! The "
    "project is (21) ......... to show how car-making could (22) ......... "
    "environmentally friendly. The car meets all racing car (23) ......... apart from "
    "its fuel. This is a mixture of waste chocolate and vegetable oil, and such "
    "'biofuels' are not (24) ......... in the sport yet. It has to be mixed with normal "
    "fuel so that all parts of the car keep working.\n\n"
    "Carrots and other root vegetables were used to make some parts inside and outside "
    "the car. Even the mirrors are made from potatoes! The sides of the car "
    "(25) ......... a mixture of natural materials from plants as well as other "
    "recycled materials.\n\n"
    "The project is still young, so the scientists have not yet found out how 'green' "
    "the car is. They are planning many experiments to compare its (26) ......... "
    "against that of normal racing cars."
)
_R5 = [
    (21, [("A", "intended"), ("B", "wished"), ("C", "decided"), ("D", "insisted")], "A"),
    (22, [("A", "develop"), ("B", "move"), ("C", "become"), ("D", "arrive")], "C"),
    (23, [("A", "levels"), ("B", "standards"), ("C", "grades"), ("D", "orders")], "B"),
    (24, [("A", "allowed"), ("B", "let"), ("C", "ruled"), ("D", "agreed")], "A"),
    (25, [("A", "store"), ("B", "involve"), ("C", "collect"), ("D", "contain")], "D"),
    (26, [("A", "operation"), ("B", "performance"), ("C", "display"), ("D", "technique")], "B"),
]


def _reading_part5() -> Section:
    return Section(
        id="b1-2022-sec-r5",
        title="Reading Part 5 — This car runs on chocolate!",
        skill="reading",
        stimulus=GappedTextStimulus(text=_R5_TEXT),
        items=[
            _choice(f"b1-2022-r5-q{q}", f"Choose the word for gap {q}.", o, c) for q, o, c in _R5
        ],
    )


# ---- Reading Part 6: open cloze (write one word) -------------------------- #
_R6_TEXT = (
    "Our new skatepark!\n"
    "by Jack Fletcher\n\n"
    "Is there a great skatepark in your town? We've now got the (27) ......... "
    "fantastic skatepark ever, and it's all because of my friends and me!\n\n"
    "Our old skatepark was full of broken equipment, so none of us ever went there. "
    "But we all agreed that (28) ......... we had a better skatepark in our town, we'd "
    "use it. And teenagers might come (29) ......... other towns to join us, too.\n\n"
    "So I set up an online questionnaire to find out (30) ......... local people "
    "wanted. I asked them whether we should improve our old skatepark (31) ......... "
    "build a completely new one. People voted to build a new one.\n\n"
    "Then we held some events to get money to pay for it. In the end we collected half "
    "the cost, and the local council paid the rest. It (32) ......... finally finished "
    "last month. So come and try it — you'll have a great time!"
)
# Q28's key lists two acceptable words ("if / when").
_R6 = [
    (27, ["most"]),
    (28, ["if", "when"]),
    (29, ["from"]),
    (30, ["what"]),
    (31, ["or"]),
    (32, ["was"]),
]


def _reading_part6() -> Section:
    return Section(
        id="b1-2022-sec-r6",
        title="Reading Part 6 — Our new skatepark! (open cloze)",
        skill="reading",
        stimulus=GappedTextStimulus(text=_R6_TEXT),
        items=[
            GapFillItem(id=f"b1-2022-r6-q{q}", prompt=f"Write one word for gap {q}.", accepted=a)
            for q, a in _R6
        ],
    )


# ---- Writing Parts 1 & 2 (open writing — no answer key, by design) -------- #
def _writing_part1() -> Section:
    return Section(
        id="b1-2022-sec-w1",
        title="Writing Part 1",
        skill="writing",
        stimulus=PassageTextStimulus(
            text=(
                "Read this email from your English teacher Mrs Lake and the notes you "
                "have made.\n\n"
                "EMAIL\n"
                "From: Mrs Lake\n"
                "Subject: End of year party\n\n"
                "Dear Class,\n\n"
                "I'd like our class to have a party to celebrate the end of the school "
                "year.\n\n"
                "We could either have a party in the classroom or we could go to the "
                "park. Which would you prefer to do?  [your note: Great! Explain]\n\n"
                "What sort of activities or games should we do during the party?  "
                "[your note: Suggest ...]\n\n"
                "What food do you think we should have at the party?  [your note: Tell "
                "Mrs Lake]\n\n"
                "Reply soon!\n\nAnna Lake"
            )
        ),
        items=[
            OpenWritingItem(
                id="b1-2022-w1-q1",
                prompt="Write your email to Mrs Lake using all the notes. Write about 100 words.",
                word_min=100,
                bullet_points=[
                    "say which you'd prefer (classroom or park) and explain",
                    "suggest activities or games for the party",
                    "tell Mrs Lake what food you should have",
                ],
                rubric=(
                    "B1 Preliminary Writing Part 1 (max 20 marks). A reply email of "
                    "around 100 words covering all three notes (preference + reason, "
                    "activities, food). Reward content coverage, organisation, language "
                    "range and accuracy at B1; the ~100-word count is a target, not a "
                    "hard cut-off — do not over-penalise 80–120 words."
                ),
                grade_mode="llm",
            )
        ],
    )


def _writing_part2() -> Section:
    # Part 2 is "choose ONE of Q2 / Q3"; both are offered, the student answers one
    # (the other is left blank). Flagged for the reviewer/runner (see report).
    return Section(
        id="b1-2022-sec-w2",
        title="Writing Part 2 — choose ONE (article or story)",
        skill="writing",
        stimulus=PassageTextStimulus(
            text=(
                "Choose ONE of these questions. Write your answer in about 100 words.\n\n"
                "Question 2 — You see this announcement in your school English-language "
                "magazine:\n"
                "'Articles wanted! WHAT MAKES YOU LAUGH? Write an article telling us "
                "what you find funny and who you enjoy laughing with. Do you think it's "
                "good to laugh a lot? Why? The best articles will be published next "
                "month.'\n\n"
                "Question 3 — Your English teacher has asked you to write a story. Your "
                "story must begin with this sentence: 'Jo looked at the map and decided "
                "to go left.'"
            )
        ),
        items=[
            OpenWritingItem(
                id="b1-2022-w2-q2",
                prompt=(
                    "Question 2 (article): Write an article about what makes you laugh, "
                    "who you laugh with, and whether laughing a lot is good. About 100 "
                    "words. (Answer this OR Question 3.)"
                ),
                word_min=100,
                rubric=(
                    "B1 Writing Part 2 article (max 20 marks). ~100 words answering all "
                    "prompts (what's funny, who with, is laughing good + why). Reward "
                    "register, organisation, range and accuracy at B1. Count is a target "
                    "(80–120 acceptable). Left blank if the student chose the story."
                ),
                grade_mode="llm",
            ),
            OpenWritingItem(
                id="b1-2022-w2-q3",
                prompt=(
                    "Question 3 (story): Write a story that begins 'Jo looked at the map "
                    "and decided to go left.' About 100 words. (Answer this OR Question "
                    "2.)"
                ),
                word_min=100,
                rubric=(
                    "B1 Writing Part 2 story (max 20 marks). ~100 words, must open with "
                    "the given sentence and tell a coherent story. Reward narrative "
                    "control, range and accuracy at B1. Count is a target (80–120 "
                    "acceptable). Left blank if the student chose the article."
                ),
                grade_mode="llm",
            ),
        ],
    )


# ---- Listening Part 1: 7 questions, 3 picture options each ---------------- #
_L1 = [
    (1, "What will the boy bring for the barbecue?", "A"),
    (2, "Which part of the boy's body hurts now?", "B"),
    (3, "What will the visitors see last?", "C"),
    (4, "Where did the police catch the zebra?", "C"),
    (5, "What did the girl do yesterday?", "C"),
    (6, "Which computer game does the girl like most?", "A"),
    (7, "Which sport did the boy do for the first time on holiday?", "C"),
]


def _listening_part1() -> Section:
    items: list[Item] = []
    for q, prompt, correct in _L1:
        items.append(
            SingleChoiceItem(
                id=f"b1-2022-l1-q{q}",
                prompt=prompt,
                options=[
                    ImageOption(key="A", asset_id=f"b1-2022-l1-q{q}-a.png", alt="Picture A"),
                    ImageOption(key="B", asset_id=f"b1-2022-l1-q{q}-b.png", alt="Picture B"),
                    ImageOption(key="C", asset_id=f"b1-2022-l1-q{q}-c.png", alt="Picture C"),
                ],
                correct=correct,
            )
        )
    return Section(
        id="b1-2022-sec-l1",
        title="Listening Part 1 — Choose the correct picture",
        skill="listening",
        # First section of the listening block: anchors the shared player and carries
        # the free intro preview before the (single-play) test track.
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1, preview_asset_id=_PREVIEW_ID),
        items=items,
    )


# ---- Listening Part 2: 3-option multiple choice --------------------------- #
_L2 = [
    (
        8,
        "Two friends talk about a campsite. What did the boy like best about it?",
        [
            ("A", "It was very close to the beach."),
            ("B", "There were lots of people his age."),
            ("C", "The activities were free."),
        ],
        "B",
    ),
    (
        9,
        "Two friends talk about homework. The girl thinks that doing homework with friends",
        [
            ("A", "is fun."),
            ("B", "helps concentration."),
            ("C", "takes longer than doing it alone."),
        ],
        "A",
    ),
    (
        10,
        "A boy tells his friend about a rock-climbing trip. How did he feel about it?",
        [
            ("A", "grateful for the help he got"),
            ("B", "satisfied with his climbing"),
            ("C", "hopeful of going again"),
        ],
        "C",
    ),
    (
        11,
        "Two friends talk about learning to play the guitar. The girl advises the boy to",
        [
            ("A", "practise more often."),
            ("B", "play in a variety of styles."),
            ("C", "listen to the best guitarists."),
        ],
        "C",
    ),
    (
        12,
        "Two friends talk about a book they've read. They agree that it has",
        [("A", "lots of action."), ("B", "realistic characters."), ("C", "an unexpected ending.")],
        "B",
    ),
    (
        13,
        "Two friends talk about a concert they have been to. They agree that",
        [
            ("A", "the organisation was poor."),
            ("B", "the performance was good."),
            ("C", "the tickets were expensive."),
        ],
        "C",
    ),
]


def _listening_part2() -> Section:
    return Section(
        id="b1-2022-sec-l2",
        title="Listening Part 2 — Multiple choice",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1),
        items=[_choice(f"b1-2022-l2-q{q}", p, o, c) for q, p, o, c in _L2],
    )


# ---- Listening Part 3: note completion (gap fill) ------------------------- #
# Phil Lamb, TV news presenter. Accepted forms / misspellings from the key.
_L3 = [
    (14, "Phil's first job after university was on local …", ["radio"], []),
    (
        15,
        "Before presenting the news, Phil looks through the day's …",
        ["newspaper", "newspapers", "paper", "papers"],
        [],
    ),
    (16, "Phil is very careful about which … he wears.", ["jacket", "jackets"], []),
    (17, "Phil sometimes finds the names of some … difficult to say correctly.", ["people"], []),
    (
        18,
        "Phil enjoys presenting news on the topic of …",
        ["business"],
        ["busines", "bussines", "bussiness"],
    ),
    (19, "Phil would like to be a … in the future.", ["producer"], []),
]


def _listening_part3() -> Section:
    return Section(
        id="b1-2022-sec-l3",
        title="Listening Part 3 — TV news presenter (note completion)",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1),
        items=[
            GapFillItem(
                id=f"b1-2022-l3-q{q}",
                prompt=prompt,
                accepted=accepted,
                accepted_variants=variants,
            )
            for q, prompt, accepted, variants in _L3
        ],
    )


# ---- Listening Part 4: 3-option multiple choice --------------------------- #
_L4 = [
    (
        20,
        "Mandy started working as a DJ",
        [
            ("A", "once she could afford the equipment."),
            ("B", "after she lost her job as a nurse."),
            ("C", "when she first left school."),
        ],
        "A",
    ),
    (
        21,
        "What does Mandy say about her singing career?",
        [
            ("A", "It started by chance."),
            ("B", "It took years of practice."),
            ("C", "It began with a song that she wrote."),
        ],
        "A",
    ),
    (
        22,
        "What is Mandy's new song about?",
        [("A", "making new friends"), ("B", "changing your mind"), ("C", "finding life difficult")],
        "C",
    ),
    (
        23,
        "How does Mandy feel about her new CD?",
        [
            ("A", "sure that people will like it"),
            ("B", "pleased with what she's achieved"),
            ("C", "sorry that it wasn't ready on time"),
        ],
        "B",
    ),
    (
        24,
        "Mandy's favourite songs are those which",
        [
            ("A", "are easy to dance to."),
            ("B", "other women have written."),
            ("C", "have interesting words."),
        ],
        "C",
    ),
    (
        25,
        "In the future, Mandy plans to",
        [
            ("A", "learn another instrument."),
            ("B", "run her own business."),
            ("C", "work in television."),
        ],
        "B",
    ),
]


def _listening_part4() -> Section:
    return Section(
        id="b1-2022-sec-l4",
        title="Listening Part 4 — Interview with a young singer",
        skill="listening",
        stimulus=AudioAssetStimulus(asset_id=_AUDIO_ID, plays=1),
        items=[_choice(f"b1-2022-l4-q{q}", p, o, c) for q, p, o, c in _L4],
    )


# --------------------------------------------------------------------------- #
# The whole test.
# --------------------------------------------------------------------------- #
def build_test() -> Test:
    """Assemble the B1 Preliminary 2022 sample as one draft test (R + W + L)."""
    return Test(
        id=TEST_ID,
        title="B1 Preliminary for Schools 2022 — Sample Test",
        level="B1_PRELIMINARY",
        status="draft",
        duration_minutes=120,  # Reading 45' + Writing 45' + Listening ~30'; run apart
        sections=[
            *_reading_part1(),
            _reading_part2(),
            _reading_part3(),
            _reading_part4(),
            _reading_part5(),
            _reading_part6(),
            _writing_part1(),
            _writing_part2(),
            _listening_part1(),
            _listening_part2(),
            _listening_part3(),
            _listening_part4(),
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
        log.exception("ingest_b1_2022 failed")
        raise
    finally:
        session.close()
    log.info("ingest_b1_2022 complete: test=%s loaded as DRAFT (awaiting review)", test_id)


if __name__ == "__main__":
    main()
