"""Phase 9: ingestion pipeline (PDF -> draft items).

Hermetic: the heavy backends (PyMuPDF / Anthropic / WhisperX) are replaced by the
in-package mocks, so these exercise the deterministic core — extraction shape,
LLM-draft structuring, answer-key parse + merge, ASR alignment, the never-publish
invariant, and an import guard that the public surface pulls no optional dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.content import ContentService, FilesystemStorage
from app.ingestion import (
    AudioSpan,
    DraftGapFill,
    DraftMatching,
    DraftOpenWriting,
    DraftSection,
    DraftSingleChoice,
    DraftTest,
    ExtractedDocument,
    ExtractedPage,
    ImageCrop,
    IngestionPipeline,
    IngestionRequest,
    IngestionService,
    MockAsr,
    MockLLMStructurer,
    MockPdfExtractor,
    Transcript,
    Word,
    align_items_to_spans,
    merge_key,
    parse_answer_key,
)
from app.persistence.repository import ContentRepository
from contracts import (
    GapFillItem,
    MatchingItem,
    OpenWritingItem,
    SingleChoiceItem,
)
from contracts.content import (
    AudioAssetStimulus,
    MatchingPoolStimulus,
    PassageTextStimulus,
    PoolOption,
    TextOption,
)

# Cambridge-style answer key: choices, a gap-fill with alternates + a misspelling
# variant in parens, and matching letters. q7 (writing) is intentionally absent.
KEY_TEXT = """\
Answer key
1 B
2 station / railway station (statoin)
3 C
4 A
5 A
6 B
"""


def _choice(number: int) -> DraftSingleChoice:
    return DraftSingleChoice(
        number=number,
        prompt=f"Question {number}?",
        options=[
            TextOption(key="A", text="alpha"),
            TextOption(key="B", text="bravo"),
            TextOption(key="C", text="charlie"),
        ],
    )


def _golden_draft() -> DraftTest:
    return DraftTest(
        id="placeholder",  # overwritten by the structurer from the request
        title="A2 Key — Golden Sample",
        level="A2_KEY",
        duration_minutes=60,
        sections=[
            DraftSection(
                id="sec-reading",
                skill="reading",
                stimulus=PassageTextStimulus(text="A short passage."),
                items=[_choice(1), DraftGapFill(number=2, prompt="The ___ is open.")],
            ),
            DraftSection(
                id="sec-matching",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    options=[
                        PoolOption(key="A", text="opt a"),
                        PoolOption(key="B", text="opt b"),
                        PoolOption(key="C", text="opt c"),
                        PoolOption(key="D", text="opt d"),
                    ]
                ),
                items=[
                    DraftMatching(number=3, prompt="Match 3"),
                    DraftMatching(number=4, prompt="Match 4"),
                ],
            ),
            DraftSection(
                id="sec-listening",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id="audio-1", plays=2),
                items=[_choice(5), _choice(6)],
            ),
            DraftSection(
                id="sec-writing",
                skill="writing",
                stimulus=PassageTextStimulus(text="Write a note to your friend."),
                items=[
                    DraftOpenWriting(
                        number=7,
                        prompt="Write 25 words.",
                        word_min=25,
                        bullet_points=["where", "when"],
                        rubric="Award marks for task achievement.",
                    )
                ],
            ),
        ],
    )


def _golden_document() -> ExtractedDocument:
    return ExtractedDocument(
        pages=(ExtractedPage(number=1, text="extracted question paper text"),),
        crops=(
            ImageCrop(asset_id="img-p1-x1", data=b"PNG1", page=1, bbox=(0, 0, 1, 1)),
            ImageCrop(asset_id="img-p1-x2", data=b"PNG2", page=1, bbox=(1, 1, 2, 2)),
        ),
    )


def _key_document() -> ExtractedDocument:
    # The answer-key PDF extracts to its text (no crops).
    return ExtractedDocument(pages=(ExtractedPage(number=1, text=KEY_TEXT),))


def _golden_transcript() -> Transcript:
    return Transcript(
        words=(
            Word("Hello", 0.0, 1.0, speaker="SPEAKER_00"),
            Word("there", 1.0, 2.0, speaker="SPEAKER_00"),
            Word("welcome", 6.0, 12.0, speaker="SPEAKER_01"),
        )
    )


def _pipeline(*, with_asr: bool = True) -> IngestionPipeline:
    extractor = MockPdfExtractor(
        _golden_document(), by_input={KEY_TEXT.encode(): _key_document()}
    )
    structurer = MockLLMStructurer(_golden_draft())
    asr = MockAsr(_golden_transcript()) if with_asr else None
    return IngestionPipeline(extractor, structurer, asr=asr)


def _request() -> IngestionRequest:
    return IngestionRequest(
        test_id="a2-golden",
        level="A2_KEY",
        questions_pdf=b"%PDF-questions",
        answer_key_pdf=KEY_TEXT.encode(),
        audio=b"ID3-audio",
    )


# --------------------------------------------------------------------------- #
# Answer-key parsing.
# --------------------------------------------------------------------------- #
def test_parse_answer_key_choices_and_gap_variants() -> None:
    key = parse_answer_key(KEY_TEXT)

    assert set(key) == {1, 2, 3, 4, 5, 6}
    assert key[1].answers == ("B",)
    # Gap-fill: alternates split on '/', the parenthesised misspelling is a variant.
    assert key[2].answers == ("station", "railway station")
    assert key[2].variants == ("statoin",)


def test_parse_answer_key_ignores_noise_lines() -> None:
    key = parse_answer_key("Answer key\n\nPart 1\n1 A\nnonsense without a number\n")
    assert set(key) == {1}


# --------------------------------------------------------------------------- #
# Golden pipeline: counts, types, and an authoritative key merge.
# --------------------------------------------------------------------------- #
def test_pipeline_golden_counts_and_types() -> None:
    result = _pipeline().run(_request())
    test = result.test

    assert test.id == "a2-golden"
    assert len(test.sections) == 4
    items = [it for s in test.sections for it in s.items]
    assert len(items) == 7

    by_type: dict[str, int] = {}
    for it in items:
        by_type[it.item_type] = by_type.get(it.item_type, 0) + 1
    assert by_type == {"single_choice": 3, "gap_fill": 1, "matching": 2, "open_writing": 1}


def test_pipeline_key_merge_fills_correct() -> None:
    items = {it.id: it for s in _pipeline().run(_request()).test.sections for it in s.items}

    q1 = items["q1"]
    assert isinstance(q1, SingleChoiceItem) and q1.correct == "B"
    q3 = items["q3"]
    assert isinstance(q3, MatchingItem) and q3.correct == "C"

    q2 = items["q2"]
    assert isinstance(q2, GapFillItem)
    assert q2.accepted == ["station", "railway station"]
    assert q2.accepted_variants == ["statoin"]

    # Writing carries no answer key, only its rubric.
    q7 = items["q7"]
    assert isinstance(q7, OpenWritingItem) and q7.rubric


def test_pipeline_always_yields_a_draft() -> None:
    # Golden rule #5: ingestion NEVER auto-publishes.
    assert _pipeline().run(_request()).test.status == "draft"


# --------------------------------------------------------------------------- #
# Rejection of malformed extraction.
# --------------------------------------------------------------------------- #
def test_merge_rejects_missing_key() -> None:
    draft = _golden_draft()
    key = parse_answer_key(KEY_TEXT)
    del key[1]  # drop the key for a keyed item
    with pytest.raises(ValueError, match="no answer key for question 1"):
        merge_key(draft, key)


def test_merge_rejects_correct_not_in_options() -> None:
    draft = _golden_draft()
    key = parse_answer_key(KEY_TEXT)
    key[1] = key[1].__class__(number=1, answers=("Z",))  # not an option key
    with pytest.raises(ValueError, match="not in options"):
        merge_key(draft, key)


def test_merge_rejects_matching_correct_not_in_pool() -> None:
    draft = _golden_draft()
    key = parse_answer_key(KEY_TEXT)
    key[3] = key[3].__class__(number=3, answers=("Z",))  # not a pool key (A-D)
    with pytest.raises(ValueError, match="not in matching pool"):
        merge_key(draft, key)


def test_merge_rejects_matching_multi_answer() -> None:
    draft = _golden_draft()
    key = parse_answer_key(KEY_TEXT)
    key[3] = key[3].__class__(number=3, answers=("C", "D"))  # matching takes one key
    with pytest.raises(ValueError, match="matching expects a single key"):
        merge_key(draft, key)


def test_draft_single_choice_has_no_answer_key_field() -> None:
    # The LLM draft is structurally incapable of carrying `correct` (golden rule #5).
    assert "correct" not in DraftSingleChoice.model_fields


# --------------------------------------------------------------------------- #
# ASR alignment.
# --------------------------------------------------------------------------- #
def test_audio_alignment_deterministic_and_in_range() -> None:
    draft = _golden_draft()
    transcript = _golden_transcript()

    spans = align_items_to_spans(draft.sections, transcript)
    # Only the two listening items are aligned, in order, partitioning [0, 12].
    assert set(spans) == {"q5", "q6"}
    assert spans["q5"] == AudioSpan(start_s=0.0, end_s=6.0)
    assert spans["q6"] == AudioSpan(start_s=6.0, end_s=12.0)
    for span in spans.values():
        assert 0.0 <= span.start_s < span.end_s <= transcript.duration_s

    assert align_items_to_spans(draft.sections, transcript) == spans  # deterministic


def test_pipeline_without_audio_skips_asr() -> None:
    request = IngestionRequest(
        test_id="a2-noaudio",
        level="A2_KEY",
        questions_pdf=b"%PDF",
        answer_key_pdf=KEY_TEXT.encode(),
        audio=None,
    )
    result = _pipeline(with_asr=False).run(request)
    assert result.transcript is None
    assert result.audio_spans == {}


# --------------------------------------------------------------------------- #
# Service: store crops + queue the draft (never publish).
# --------------------------------------------------------------------------- #
def test_service_queues_draft_with_assets(session: Session, tmp_path: Path) -> None:
    repo = ContentRepository(session)
    content = ContentService(repo, FilesystemStorage(tmp_path))
    service = IngestionService(content, _pipeline())

    result = service.ingest(_request())

    stored = repo.get_test("a2-golden")
    assert stored is not None
    assert stored.status == "draft"  # queued for review, not published
    # Image crops were stored as assets under their stable ids.
    for crop in result.crops:
        assert content.has_asset(crop.asset_id)


# --------------------------------------------------------------------------- #
# Import guard: the public surface must not pull a heavy/optional backend.
# --------------------------------------------------------------------------- #
def test_public_surface_imports_no_heavy_backend() -> None:
    heavy = {"anthropic", "fitz", "whisperx", "arq", "pymupdf"}
    pkg = Path(__file__).resolve().parent.parent / "app" / "ingestion"
    public_modules = [
        "__init__.py",
        "models.py",
        "extract.py",
        "structure.py",
        "answer_key.py",
        "asr.py",
        "pipeline.py",
        "service.py",
        "jobs.py",
    ]
    for name in public_modules:
        tree = ast.parse((pkg / name).read_text(encoding="utf-8"))
        for node in tree.body:  # module-level imports only (lazy ones live in functions)
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not (roots & heavy), f"{name} imports a heavy backend at module level"
