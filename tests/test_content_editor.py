"""Content editor (no code): validated updates, the published-test guard, copies,
JSON import/export, asset upload and the CLI wrappers (`app/content/editor.py`,
`ContentService`, `app/content/transfer.py`).

Golden rules exercised: #4 (every write validates against the contracts — Russian
400 otherwise), #5 (imports / copies are drafts; nothing here publishes), #7 (items
inside a shared-stimulus section are never reordered).
"""

from __future__ import annotations

import io
import json
import logging
import threading
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.content import (
    ContentService,
    FilesystemStorage,
    ingest_a2_2022,
    ingest_b1_2022,
    ingest_movers_2022,
)
from app.content.editor import (
    ContentConflictError,
    ContentNotFoundError,
    ContentValidationError,
    asset_id_from_filename,
    can_reorder_items,
    is_cosmetic_change,
    is_mp3_frame,
    next_copy_id,
    reid_test,
    sniff_media_type,
    validate_test_data,
)
from app.content.seed import SEED_TEST_ID, build_sample_test, sample_assets
from app.content.storage import AssetExistsError
from app.content.transfer import main as transfer_main
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import Attempt, RosterEntry, Section
from contracts import Test as ExamTest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
MP3 = b"ID3\x04\x00" + b"\x00" * 64


# --------------------------------------------------------------------------- #
# Fixtures / helpers.
# --------------------------------------------------------------------------- #
@pytest.fixture
def storage(tmp_path: Path) -> FilesystemStorage:
    return FilesystemStorage(tmp_path / "assets")


@pytest.fixture
def svc(session: Session, storage: FilesystemStorage) -> ContentService:
    service = ContentService(ContentRepository(session), storage)
    for asset_id, (data, ct) in sample_assets().items():
        storage.put(asset_id, data, content_type=ct)
    service.create_test(build_sample_test())
    return service


def _item(svc: ContentService, item_id: str, test_id: str = SEED_TEST_ID) -> dict:
    test = svc.get_test(test_id)
    assert test is not None
    for section in test.sections:
        for item in section.items:
            if item.id == item_id:
                return item.model_dump(mode="json")
    raise AssertionError(item_id)


def _get(svc: ContentService, test_id: str = SEED_TEST_ID) -> ExamTest:
    test = svc.get_test(test_id)
    assert test is not None
    return test


def _add_attempt(session: Session, test_id: str = SEED_TEST_ID, n: int = 1) -> None:
    repo = AttemptRepository(session)
    repo.add_roster_entry(RosterEntry(id=f"r{n}", test_id=test_id, display_name=f"S{n}"))
    repo.add_attempt(
        Attempt(id=f"a{n}", test_id=test_id, roster_entry_id=f"r{n}", status="submitted", seed=n)
    )


def _strip_ids(test: ExamTest) -> dict:
    data = test.model_dump(mode="json")
    data.pop("id")
    data.pop("status")
    for s in data["sections"]:
        s.pop("id")
        for i in s["items"]:
            i.pop("id")
    return data


# --------------------------------------------------------------------------- #
# Validation helpers.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "build",
    [
        build_sample_test,
        ingest_a2_2022.build_test,
        ingest_b1_2022.build_test,
        ingest_movers_2022.build_test,
    ],
)
def test_every_bundled_test_passes_editor_validation(build) -> None:  # noqa: ANN001
    """The semantic checks never reject real, already-published content."""
    test = build()
    assert validate_test_data(test.model_dump(mode="json")) == test


def test_pydantic_errors_are_russian() -> None:
    data = build_sample_test().model_dump(mode="json")
    data["duration_minutes"] = 0
    data["sections"][0]["items"][0]["options"] = data["sections"][0]["items"][0]["options"][:1]
    with pytest.raises(ContentValidationError) as exc:
        validate_test_data(data)
    msg = str(exc.value)
    assert "Длительность (мин): должно быть больше 0" in msg
    assert "Раздел 1 → вопрос 1 → варианты ответа: нужно не меньше 2" in msg
    assert "Input should" not in msg  # no raw English Pydantic text


def test_unknown_item_type_is_russian() -> None:
    data = build_sample_test().model_dump(mode="json")
    data["sections"][0]["items"][0]["item_type"] = "essay"
    with pytest.raises(ContentValidationError, match="неизвестный тип «essay»"):
        validate_test_data(data)


def test_cosmetic_vs_structural() -> None:
    old = build_sample_test()
    data = old.model_dump(mode="json")
    data["title"] = "New title"
    data["sections"][0]["stimulus"]["text"] = "Fixed typo"
    data["sections"][0]["items"][0]["prompt"] = "When is the pool closed??"
    data["sections"][2]["items"][0]["prompt"] = "I went out ____ it rained."
    data["sections"][3]["items"][1]["bullet_points"] = ["say when (fixed)", "b", "c"]
    assert is_cosmetic_change(old, ExamTest.model_validate(data))

    def swap_option_texts(d: dict) -> None:  # A <-> B: the key moves, `correct` doesn't
        opts = d["sections"][0]["items"][0]["options"]
        opts[0]["text"], opts[1]["text"] = opts[1]["text"], opts[0]["text"]

    def swap_pool_texts(d: dict) -> None:
        pool = d["sections"][2]["stimulus"]["options"]
        pool[0]["text"], pool[1]["text"] = pool[1]["text"], pool[0]["text"]

    for path_mut in (
        swap_option_texts,
        swap_pool_texts,
        lambda d: d["sections"][0]["items"][0]["options"][1].update(text="On weekends!"),
        lambda d: d["sections"][2]["stimulus"]["options"][0].update(text="becuase"),
        lambda d: d["sections"][1]["items"][0]["options"][0].update(alt="Sign B"),
        lambda d: d["sections"][0]["items"][0].update(correct="B"),
        lambda d: d["sections"][3]["items"][0].update(accepted=["since", "for"]),
        lambda d: d["sections"][3]["items"][1].update(word_min=30),
        lambda d: d["sections"][3]["items"][1].update(rubric="other"),
        lambda d: d.update(duration_minutes=45),
        lambda d: d["sections"].reverse(),
        lambda d: d["sections"][3]["items"][0].update(rejected=["sinc"]),
    ):
        changed = old.model_dump(mode="json")
        path_mut(changed)
        assert not is_cosmetic_change(old, ExamTest.model_validate(changed))


def test_next_copy_id_and_reid() -> None:
    taken = {"t", "t-v2"}
    assert next_copy_id("t", taken.__contains__) == "t-v3"
    assert next_copy_id("t-v2", taken.__contains__) == "t-v3"
    assert next_copy_id("x", taken.__contains__) == "x-v2"
    test = build_sample_test()
    copy_ = reid_test(test, "seed-a2-demo-v2")
    assert copy_.status == "draft"
    assert all(s.id.startswith("seed-a2-demo-v2-") for s in copy_.sections)
    assert _strip_ids(copy_) == _strip_ids(test)


def test_can_reorder_items_only_without_shared_stimulus() -> None:
    test = build_sample_test()
    assert not any(can_reorder_items(s) for s in test.sections)
    free = Section.model_validate(
        {
            **test.sections[3].model_dump(mode="json"),
            "stimulus": {"kind": "passage_text", "text": ""},
        }
    )
    assert can_reorder_items(free)


# --------------------------------------------------------------------------- #
# Updates on a draft (valid + invalid).
# --------------------------------------------------------------------------- #
def test_update_meta(svc: ContentService, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    t = svc.update_test_meta(SEED_TEST_ID, {"title": "Новый", "duration_minutes": 45})
    assert (t.title, t.duration_minutes) == ("Новый", 45)
    assert _get(svc).title == "Новый"
    assert "content edit action=update_meta test=seed-a2-demo" in caplog.text
    with pytest.raises(ContentValidationError, match="Уровень: недопустимое значение"):
        svc.update_test_meta(SEED_TEST_ID, {"level": "C2"})
    with pytest.raises(ContentValidationError, match="Неизвестные поля: status"):
        svc.update_test_meta(SEED_TEST_ID, {"status": "published"})
    with pytest.raises(ContentValidationError, match="Название теста не может быть пустым"):
        svc.update_test_meta(SEED_TEST_ID, {"title": "  "})
    assert _get(svc).status == "draft"


def test_update_section(svc: ContentService) -> None:
    t = svc.update_section(
        SEED_TEST_ID,
        "seed-sec-reading",
        {"title": "Part 1 (fixed)", "stimulus": {"kind": "passage_text", "text": "New notice"}},
    )
    assert t.sections[0].title == "Part 1 (fixed)"
    assert t.sections[0].stimulus.text == "New notice"  # type: ignore[union-attr]
    # Removing pool option B (the matching key) is rejected in Russian.
    with pytest.raises(ContentValidationError, match="не найден в списке"):
        svc.update_section(
            SEED_TEST_ID,
            "seed-sec-vocab",
            {"stimulus": {"kind": "matching_pool", "options": [
                {"key": "A", "text": "because"}, {"key": "C", "text": "however"}]}},
        )
    with pytest.raises(ContentValidationError, match="неизвестный тип"):
        svc.update_section(SEED_TEST_ID, "seed-sec-reading", {"stimulus": {"kind": "video"}})
    with pytest.raises(ContentNotFoundError):
        svc.update_section(SEED_TEST_ID, "nope", {"title": "x"})


def test_reorder_sections(svc: ContentService) -> None:
    ids = [s.id for s in _get(svc).sections]
    t = svc.reorder_sections(SEED_TEST_ID, list(reversed(ids)))
    assert [s.id for s in t.sections] == list(reversed(ids))
    with pytest.raises(ContentValidationError, match="ровно один раз"):
        svc.reorder_sections(SEED_TEST_ID, ids[:2])


def test_reorder_items_respects_rule_7(svc: ContentService) -> None:
    with pytest.raises(ContentConflictError, match="менять нельзя"):
        svc.reorder_items(SEED_TEST_ID, "seed-sec-writing", ["seed-q-write", "seed-q-gap"])
    # Same section without shared stimulus -> allowed.
    svc.update_section(
        SEED_TEST_ID, "seed-sec-writing", {"stimulus": {"kind": "passage_text", "text": ""}}
    )
    t = svc.reorder_items(SEED_TEST_ID, "seed-sec-writing", ["seed-q-write", "seed-q-gap"])
    assert [i.id for i in t.sections[3].items] == ["seed-q-write", "seed-q-gap"]


def test_update_single_choice(svc: ContentService) -> None:
    item = _item(svc, "seed-q-read")
    item["prompt"] = "When is it closed?"
    item["options"][2]["text"] = "Every single day"
    item["correct"] = "C"
    t = svc.update_item(SEED_TEST_ID, "seed-q-read", item)
    got = t.sections[0].items[0]
    assert (got.prompt, got.correct, got.options[2].text) == (  # type: ignore[union-attr]
        "When is it closed?", "C", "Every single day")
    item["correct"] = "D"
    with pytest.raises(ContentValidationError, match="не совпадает ни с одним вариантом"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item)
    item["correct"] = "A"
    item["options"][1]["key"] = "A"
    with pytest.raises(ContentValidationError, match="буквы вариантов повторяются"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item)


def test_update_image_option_requires_stored_asset(svc: ContentService) -> None:
    item = _item(svc, "seed-q-listen")
    item["options"][0]["asset_id"] = "nope.png"
    with pytest.raises(ContentValidationError, match="Не найдены файлы: nope.png"):
        svc.update_item(SEED_TEST_ID, "seed-q-listen", item)
    svc.add_asset("nope.png", PNG)
    t = svc.update_item(SEED_TEST_ID, "seed-q-listen", item)
    assert t.sections[1].items[0].options[0].asset_id == "nope.png"  # type: ignore[union-attr]


def test_update_gap_fill(svc: ContentService) -> None:
    item = _item(svc, "seed-q-gap")
    item["accepted"] = ["since", "from"]
    item["accepted_variants"] = ["sinse"]
    t = svc.update_item(SEED_TEST_ID, "seed-q-gap", item)
    gap = t.sections[3].items[0]
    assert gap.accepted == ["since", "from"]  # type: ignore[union-attr]
    assert gap.accepted_variants == ["sinse"]  # type: ignore[union-attr]
    item["rejected"] = ["sincé", "sins"]
    gap = svc.update_item(SEED_TEST_ID, "seed-q-gap", item).sections[3].items[0]
    assert gap.rejected == ["sincé", "sins"]  # type: ignore[union-attr]
    item["rejected"] = ["  "]
    with pytest.raises(ContentValidationError, match="пустая неверная форма"):
        svc.update_item(SEED_TEST_ID, "seed-q-gap", item)
    item["rejected"] = "sins"
    with pytest.raises(
        ContentValidationError, match="вопрос 1 → неверные формы: должно быть списком"
    ):
        svc.update_item(SEED_TEST_ID, "seed-q-gap", item)
    item["rejected"] = []
    item["accepted"] = []
    with pytest.raises(ContentValidationError, match="принимаемые ответы"):
        svc.update_item(SEED_TEST_ID, "seed-q-gap", item)
    item["accepted"] = ["since", " "]
    with pytest.raises(ContentValidationError, match="пустой принимаемый ответ"):
        svc.update_item(SEED_TEST_ID, "seed-q-gap", item)


def test_update_matching(svc: ContentService) -> None:
    item = _item(svc, "seed-q-match")
    item["correct"] = "C"
    assert svc.update_item(SEED_TEST_ID, "seed-q-match", item).sections[2].items[0].correct == "C"  # type: ignore[union-attr]
    item["correct"] = "Z"
    with pytest.raises(ContentValidationError, match="не найден в списке вариантов"):
        svc.update_item(SEED_TEST_ID, "seed-q-match", item)


def test_update_open_writing(svc: ContentService) -> None:
    item = _item(svc, "seed-q-write")
    item.update(prompt="Write to Sam", bullet_points=["a", "b"], word_min=35, rubric="R")
    w = svc.update_item(SEED_TEST_ID, "seed-q-write", item).sections[3].items[1]
    assert (w.prompt, w.bullet_points, w.word_min, w.rubric) == (  # type: ignore[union-attr]
        "Write to Sam", ["a", "b"], 35, "R")
    item["word_min"] = 0
    with pytest.raises(ContentValidationError, match="минимум слов: должно быть больше 0"):
        svc.update_item(SEED_TEST_ID, "seed-q-write", item)


def test_item_id_is_kept_and_type_may_change_on_draft(svc: ContentService) -> None:
    t = svc.update_item(
        SEED_TEST_ID,
        "seed-q-gap",
        {"id": "hijack", "item_type": "gap_fill", "prompt": "p", "accepted": ["x"]},
    )
    assert t.sections[3].items[0].id == "seed-q-gap"
    t = svc.update_item(
        SEED_TEST_ID,
        "seed-q-gap",
        {"item_type": "open_writing", "prompt": "p", "word_min": 5, "rubric": "r"},
    )
    assert t.sections[3].items[0].item_type == "open_writing"


def test_add_and_remove_items_and_sections(svc: ContentService) -> None:
    t = svc.add_item(
        SEED_TEST_ID,
        "seed-sec-reading",
        {"item_type": "gap_fill", "prompt": "new", "accepted": ["a"]},
    )
    new_id = t.sections[0].items[-1].id
    assert new_id == f"{SEED_TEST_ID}-q6"
    with pytest.raises(ContentValidationError):
        svc.add_item(SEED_TEST_ID, "seed-sec-reading", {"item_type": "gap_fill", "prompt": "x"})
    t = svc.remove_item(SEED_TEST_ID, new_id)
    assert [i.id for i in t.sections[0].items] == ["seed-q-read"]
    with pytest.raises(ContentValidationError, match="хотя бы один вопрос"):
        svc.remove_item(SEED_TEST_ID, "seed-q-read")
    t = svc.add_section(
        SEED_TEST_ID,
        {
            "title": "Extra",
            "skill": "reading",
            "stimulus": {"kind": "passage_text", "text": ""},
            "items": [{"item_type": "gap_fill", "prompt": "q", "accepted": ["a"]}],
        },
        position=0,
    )
    assert t.sections[0].id == f"{SEED_TEST_ID}-sec5"
    assert t.sections[0].items[0].id.startswith(f"{SEED_TEST_ID}-q")
    t = svc.remove_section(SEED_TEST_ID, t.sections[0].id)
    assert len(t.sections) == 4
    with pytest.raises(ContentNotFoundError):
        svc.remove_item(SEED_TEST_ID, "missing")
    with pytest.raises(ContentNotFoundError):
        svc.update_item("no-test", "x", {})


def test_remove_last_section_refused(svc: ContentService) -> None:
    for sid in ["seed-sec-listening", "seed-sec-vocab", "seed-sec-writing"]:
        svc.remove_section(SEED_TEST_ID, sid)
    with pytest.raises(ContentValidationError, match="хотя бы один раздел"):
        svc.remove_section(SEED_TEST_ID, "seed-sec-reading")


def test_ids_used_by_another_test_are_refused(svc: ContentService) -> None:
    other = reid_test(build_sample_test(), "other")
    svc.create_test(other)
    with pytest.raises(ContentConflictError, match="заняты другим тестом"):
        svc.add_item(
            SEED_TEST_ID,
            "seed-sec-reading",
            {"id": other.sections[0].items[0].id, "item_type": "gap_fill", "prompt": "x",
             "accepted": ["a"]},
        )


# --------------------------------------------------------------------------- #
# Published-test guards.
# --------------------------------------------------------------------------- #
def test_published_cosmetic_fix_needs_force(
    svc: ContentService, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    svc.publish(SEED_TEST_ID)
    _add_attempt(session)
    item = _item(svc, "seed-q-read")
    item["prompt"] = "Typo fixed"
    with pytest.raises(ContentConflictError, match="подтвердить"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item)
    caplog.set_level(logging.INFO)
    t = svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    assert t.sections[0].items[0].prompt == "Typo fixed"
    assert t.status == "published"
    warn = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("FORCED on PUBLISHED" in r.getMessage() for r in warn)


def test_published_key_or_item_set_change_refused_even_with_force(
    svc: ContentService, session: Session
) -> None:
    svc.publish(SEED_TEST_ID)
    _add_attempt(session)
    item = _item(svc, "seed-q-read")
    item["correct"] = "B"
    with pytest.raises(ContentConflictError, match="черновик-копию"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    gap = _item(svc, "seed-q-gap")
    gap["accepted"] = ["for"]
    with pytest.raises(ContentConflictError):
        svc.update_item(SEED_TEST_ID, "seed-q-gap", gap, force=True)
    gap = _item(svc, "seed-q-gap")
    gap["rejected"] = ["sinse"]  # the "never accept" list is part of the key
    with pytest.raises(ContentConflictError):
        svc.update_item(SEED_TEST_ID, "seed-q-gap", gap, force=True)
    with pytest.raises(ContentConflictError):
        svc.add_item(SEED_TEST_ID, "seed-sec-reading",
                     {"item_type": "gap_fill", "prompt": "n", "accepted": ["a"]})
    with pytest.raises(ContentConflictError):
        svc.remove_section(SEED_TEST_ID, "seed-sec-vocab")
    with pytest.raises(ContentConflictError):
        svc.update_test_meta(SEED_TEST_ID, {"duration_minutes": 99}, force=True)
    assert _get(svc) == build_sample_test().model_copy(update={"status": "published"})


def test_published_without_attempts_still_guarded(svc: ContentService) -> None:
    svc.publish(SEED_TEST_ID)
    item = _item(svc, "seed-q-read")
    item["correct"] = "B"
    with pytest.raises(ContentConflictError, match="снимите тест с публикации"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    svc.unpublish(SEED_TEST_ID)  # unpublish first -> editing a draft is free
    assert svc.update_item(SEED_TEST_ID, "seed-q-read", item).sections[0].items[0].correct == "B"  # type: ignore[union-attr]


def test_draft_with_attempts_refuses_key_and_structure_changes(
    svc: ContentService, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """A draft WITH attempts (e.g. after unpublish) is locked like a published test:
    a regrade would otherwise re-score finished work against the new key."""
    _add_attempt(session)
    item = _item(svc, "seed-q-read")
    item["correct"] = "B"
    with pytest.raises(ContentConflictError, match="Создать черновик-копию") as exc:
        svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    assert "работы учеников (1)" in str(exc.value)
    with pytest.raises(ContentConflictError, match="Создать черновик-копию"):
        svc.add_item(
            SEED_TEST_ID, "seed-sec-reading",
            {"item_type": "gap_fill", "prompt": "n", "accepted": ["a"]},
        )
    with pytest.raises(ContentConflictError):
        svc.remove_section(SEED_TEST_ID, "seed-sec-vocab")
    with pytest.raises(ContentConflictError):
        svc.reorder_sections(SEED_TEST_ID, [s.id for s in _get(svc).sections][::-1])
    with pytest.raises(ContentConflictError):
        svc.update_test_meta(SEED_TEST_ID, {"duration_minutes": 99}, force=True)
    assert _get(svc) == build_sample_test()  # nothing changed

    # Cosmetic text: needs force, then saved with a WARNING.
    item = _item(svc, "seed-q-read")
    item["prompt"] = "Typo fixed"
    with pytest.raises(ContentConflictError, match="подтвердить"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item)
    caplog.set_level(logging.INFO)
    svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    assert _get(svc).sections[0].items[0].prompt == "Typo fixed"
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("FORCED on a draft WITH 1 attempt(s)" in m for m in warn)

    # The way out: a draft copy (no attempts) is edited freely.
    copy_ = svc.duplicate_test(SEED_TEST_ID)
    c_item = _item(svc, copy_.sections[0].items[0].id, copy_.id)
    c_item["correct"] = "B"
    svc.update_item(copy_.id, c_item["id"], c_item)


def test_option_and_pool_texts_are_key_bearing(svc: ContentService, session: Session) -> None:
    """Swapping the texts of A and B changes the right answer without touching
    `correct`: refused on a published test even with force, and with attempts."""
    svc.publish(SEED_TEST_ID)
    item = _item(svc, "seed-q-read")
    opts = item["options"]
    opts[0]["text"], opts[1]["text"] = opts[1]["text"], opts[0]["text"]
    with pytest.raises(ContentConflictError, match="варианты ответа"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    item = _item(svc, "seed-q-read")
    item["options"][1]["text"] = "On weekend"  # even a "typo" in an option text
    with pytest.raises(ContentConflictError):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    sec = _get(svc).sections[2].model_dump(mode="json")
    pool = sec["stimulus"]["options"]
    pool[0]["text"], pool[1]["text"] = pool[1]["text"], pool[0]["text"]
    with pytest.raises(ContentConflictError):
        svc.update_section(
            SEED_TEST_ID, "seed-sec-vocab", {"stimulus": sec["stimulus"]}, force=True
        )
    # Stimulus / prompt / title text stays a cosmetic (forced) fix.
    reading = _get(svc).sections[0].model_dump(mode="json")
    reading["stimulus"]["text"] = "NOTICE: The pool is closed on Mondays."
    svc.update_section(
        SEED_TEST_ID, "seed-sec-reading", {"stimulus": reading["stimulus"]}, force=True
    )
    svc.update_section(SEED_TEST_ID, "seed-sec-vocab", {"title": "Words"}, force=True)
    # With attempts after unpublish: still key-bearing.
    svc.unpublish(SEED_TEST_ID)
    _add_attempt(session)
    with pytest.raises(ContentConflictError, match="Создать черновик-копию"):
        svc.update_item(SEED_TEST_ID, "seed-q-read", item, force=True)
    # A draft without attempts: option texts are free.
    copy_ = svc.duplicate_test(SEED_TEST_ID)
    c_item = _item(svc, copy_.sections[0].items[0].id, copy_.id)
    c_item["options"][0]["text"], c_item["options"][1]["text"] = "On weekends", "On Mondays"
    svc.update_item(copy_.id, c_item["id"], c_item)


# --------------------------------------------------------------------------- #
# Duplicate.
# --------------------------------------------------------------------------- #
def test_duplicate_creates_draft_copy(svc: ContentService, session: Session) -> None:
    svc.publish(SEED_TEST_ID)
    _add_attempt(session)
    copy_ = svc.duplicate_test(SEED_TEST_ID)
    assert copy_.id == f"{SEED_TEST_ID}-v2"
    assert copy_.status == "draft"
    assert _strip_ids(copy_) == _strip_ids(_get(svc))
    assert _get(svc, copy_.id) == copy_
    assert _get(svc).status == "published"  # original untouched
    # The copy is freely editable (key change OK on the draft).
    item = _item(svc, copy_.sections[0].items[0].id, copy_.id)
    assert item["id"] == f"{SEED_TEST_ID}-v2-seed-q-read"  # non-prefixed id gets new_id-
    item["correct"] = "B"
    svc.update_item(copy_.id, item["id"], item)
    assert svc.duplicate_test(SEED_TEST_ID).id == f"{SEED_TEST_ID}-v3"
    assert svc.duplicate_test(copy_.id).id == f"{SEED_TEST_ID}-v4"


# --------------------------------------------------------------------------- #
# Import / export.
# --------------------------------------------------------------------------- #
def test_export_import_round_trip(svc: ContentService) -> None:
    gap = _item(svc, "seed-q-gap")
    gap["rejected"] = ["sinse"]
    svc.update_item(SEED_TEST_ID, "seed-q-gap", gap)
    exported = svc.export_test(SEED_TEST_ID).model_dump_json()
    assert '"rejected":["sinse"]' in exported
    out = svc.import_test(exported, new_id="copy-1")
    assert out.test.status == "draft" and not out.replaced
    again = svc.export_test("copy-1")
    assert _strip_ids(again) == _strip_ids(_get(svc))
    assert again == reid_test(_get(svc), "copy-1")
    assert again.sections[3].items[0].rejected == ["sinse"]  # type: ignore[union-attr]


def test_import_never_publishes(svc: ContentService) -> None:
    data = json.loads(svc.export_test(SEED_TEST_ID).model_dump_json())
    data["status"] = "published"
    out = svc.import_test(data, new_id="pub-attempt")
    assert out.test.status == "draft"
    assert _get(svc, "pub-attempt").status == "draft"


def test_import_id_collision_and_replace(svc: ContentService, session: Session) -> None:
    data = json.loads(svc.export_test(SEED_TEST_ID).model_dump_json())
    with pytest.raises(ContentConflictError, match="уже есть в банке"):
        svc.import_test(data)
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id="keep", test_id=SEED_TEST_ID, display_name="Keep")
    )
    svc.publish(SEED_TEST_ID)
    data["title"] = "Replaced"
    out = svc.import_test(data, replace=True)
    assert out.replaced and out.test.title == "Replaced"
    assert out.unpublished  # the UI says: unpublished, waiting for approval
    assert _get(svc).status == "draft"  # back in the review queue
    assert not svc.import_test(data, replace=True).unpublished  # a draft stays a draft
    assert AttemptRepository(session).get_roster_entry("keep") is not None
    _add_attempt(session)
    with pytest.raises(ContentConflictError, match="уже есть работы"):
        svc.import_test(data, replace=True)


def test_import_reports_missing_assets(svc: ContentService) -> None:
    data = json.loads(svc.export_test(SEED_TEST_ID).model_dump_json())
    data["sections"][1]["stimulus"]["asset_id"] = "new-audio.mp3"
    data["sections"][1]["items"][0]["options"][0]["asset_id"] = "new-a.png"
    with pytest.raises(ContentValidationError) as exc:
        svc.import_test(data, new_id="needs-files")
    assert "new-a.png, new-audio.mp3" in str(exc.value)
    assert svc.get_test("needs-files") is None
    out = svc.import_test(
        data,
        {"new-audio.mp3": MP3, "new-a.png": PNG, "unused.png": PNG},
        new_id="needs-files",
    )
    assert out.stored_assets == ["new-a.png", "new-audio.mp3"]
    assert out.ignored_assets == ["unused.png"]
    assert svc.get_asset("new-a.png") == PNG
    assert not svc.has_asset("unused.png")


def test_import_rejects_bad_json_and_bad_assets(svc: ContentService) -> None:
    with pytest.raises(ContentValidationError, match="корректным JSON"):
        svc.import_test(b"{not json")
    with pytest.raises(ContentValidationError, match="объект Test"):
        svc.import_test("[1, 2]")
    data = json.loads(svc.export_test(SEED_TEST_ID).model_dump_json())
    data["sections"][1]["stimulus"]["asset_id"] = "x.mp3"
    with pytest.raises(ContentValidationError, match="не похож на mp3"):
        svc.import_test(data, {"x.mp3": PNG}, new_id="bad-asset")
    del data["sections"]
    with pytest.raises(ContentValidationError, match="Разделы: обязательное поле"):
        svc.import_test(data, new_id="no-sections")


def test_import_never_overwrites_a_shared_asset(svc: ContentService) -> None:
    data = json.loads(svc.export_test(SEED_TEST_ID).model_dump_json())
    shared_png = data["sections"][1]["items"][0]["options"][0]["asset_id"]
    with pytest.raises(ContentConflictError, match="другим содержимым"):
        svc.import_test(data, {shared_png: PNG}, new_id="clobber")
    with pytest.raises(ContentConflictError, match="используется другим тестом"):
        svc.import_test(data, {shared_png: PNG}, new_id="clobber", replace=True)


# --------------------------------------------------------------------------- #
# Asset upload.
# --------------------------------------------------------------------------- #
def test_upload_asset_types(svc: ContentService, storage: FilesystemStorage) -> None:
    assert svc.upload_asset("Sign 1.PNG", PNG) == "Sign-1.png"
    assert svc.upload_asset("photo.jpeg", JPG) == "photo.jpg"
    assert svc.upload_asset("pic.webp", WEBP) == "pic.webp"
    assert svc.upload_asset("track.mp3", MP3) == "track.mp3"
    assert svc.upload_asset("frames.mp3", b"\xff\xfb\x90\x00" + b"\x00" * 16) == "frames.mp3"
    assert storage.get("Sign-1.png") == PNG
    with pytest.raises(ContentValidationError, match="только mp3, png, jpg, webp"):
        svc.upload_asset("evil.svg", b"<svg/>")
    with pytest.raises(ContentValidationError, match="не похож на PNG"):
        svc.upload_asset("fake.png", b"<html>")
    with pytest.raises(ContentValidationError, match="пустой"):
        svc.upload_asset("empty.png", b"")
    with pytest.raises(ContentValidationError, match="слишком большой"):
        svc.upload_asset("big.png", PNG + b"\x00" * 100, max_bytes=64)


def test_upload_asset_traversal_and_collisions(
    svc: ContentService, storage: FilesystemStorage, tmp_path: Path
) -> None:
    assert svc.upload_asset("../../evil.png", PNG) == "evil.png"
    assert svc.upload_asset("..\\..\\win.png", PNG) == "win.png"
    with pytest.raises(ContentValidationError):
        svc.upload_asset("../.png", PNG)  # no name left -> refused, nothing written
    assert not (tmp_path / "evil.png").exists()
    assert (storage.root / "evil.png").is_file()
    assert asset_id_from_filename("a b/c d?.mp3") == "c-d.mp3"
    # Identical bytes reuse the id; different bytes never overwrite.
    assert svc.upload_asset("evil.png", PNG) == "evil.png"
    other = PNG + b"\x01"
    assert svc.upload_asset("evil.png", other) == "evil-2.png"
    assert storage.get("evil.png") == PNG
    assert storage.get("evil-2.png") == other


# --------------------------------------------------------------------------- #
# CLI wrappers (make export-test / make import-test).
# --------------------------------------------------------------------------- #
@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.content import transfer
    from app.core.db import Base

    engine = create_engine(f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    assets_root = tmp_path / "store"
    monkeypatch.setattr(transfer, "engine", engine)
    monkeypatch.setattr(transfer, "SessionLocal", factory)
    monkeypatch.setattr(transfer.settings, "assets_dir", str(assets_root))
    # Keep pytest's log capture: the CLI would otherwise replace the root handlers.
    monkeypatch.setattr(transfer, "configure_logging", lambda *_a, **_k: None)
    with factory() as s:
        storage = FilesystemStorage(assets_root)
        for asset_id, (data, ct) in sample_assets().items():
            storage.put(asset_id, data, content_type=ct)
        ContentService(ContentRepository(s), storage).create_test(build_sample_test())
        s.commit()
    yield factory, assets_root
    engine.dispose()


def test_cli_export_then_import(cli_env, tmp_path: Path) -> None:  # noqa: ANN001
    factory, _ = cli_env
    out = tmp_path / "exported.json"
    assert transfer_main(["export", "--id", SEED_TEST_ID, "--out", str(out)]) == 0
    exported = ExamTest.model_validate_json(out.read_text(encoding="utf-8"))
    assert exported == build_sample_test()
    assert transfer_main(["import", "--file", str(out), "--new-id", "cli-copy"]) == 0
    with factory() as s:
        got = ContentRepository(s).get_test("cli-copy")
    assert got is not None and got.status == "draft"
    assert _strip_ids(got) == _strip_ids(build_sample_test())
    # Collision without --replace -> non-zero exit, nothing changed.
    assert transfer_main(["import", "--file", str(out)]) == 1


def test_cli_import_reads_assets_dir_and_reports_missing(
    cli_env, tmp_path: Path, caplog: pytest.LogCaptureFixture  # noqa: ANN001
) -> None:
    factory, assets_root = cli_env
    data = build_sample_test().model_dump(mode="json")
    data["id"] = "cli-new"
    for s in data["sections"]:
        s["id"] = "cli-" + s["id"]
        for i in s["items"]:
            i["id"] = "cli-" + i["id"]
    data["sections"][1]["stimulus"]["asset_id"] = "cli-audio.mp3"
    path = tmp_path / "new.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    caplog.set_level(logging.INFO)
    assert transfer_main(["import", "--file", str(path)]) == 1
    assert "cli-audio.mp3" in caplog.text
    files = tmp_path / "files"
    files.mkdir()
    (files / "cli-audio.mp3").write_bytes(MP3)
    assert transfer_main(["import", "--file", str(path), "--assets-dir", str(files)]) == 0
    assert (assets_root / "cli-audio.mp3").read_bytes() == MP3
    with factory() as s:
        assert ContentRepository(s).get_test("cli-new") is not None


def test_unchanged_save_on_published_needs_no_force(svc: ContentService) -> None:
    svc.publish(SEED_TEST_ID)
    item = _item(svc, "seed-q-read")
    assert svc.update_item(SEED_TEST_ID, "seed-q-read", item).status == "published"


def test_import_replace_of_published_is_logged(
    svc: ContentService, caplog: pytest.LogCaptureFixture
) -> None:
    svc.publish(SEED_TEST_ID)
    caplog.set_level(logging.INFO)
    out = svc.import_test(svc.export_test(SEED_TEST_ID).model_dump_json(), replace=True)
    assert out.unpublished
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("REPLACED a PUBLISHED test" in m for m in warn)


def test_import_deeply_nested_json_is_a_russian_400(svc: ContentService) -> None:
    deep = b'{"id": "x", "title": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    with pytest.raises(ContentValidationError, match="слишком глубоко вложен"):
        svc.import_test(deep)


def test_import_streams_assets(svc: ContentService, storage: FilesystemStorage) -> None:
    """Asset values may be seekable streams (the HTTP spooled files): validated by
    header + size, compared by digest, copied to storage in chunks."""
    data = json.loads(svc.export_test(SEED_TEST_ID).model_dump_json())
    data["sections"][1]["items"][0]["options"][1]["asset_id"] = "stream-b.png"
    big = PNG + b"\x01" * (3 * 1024 * 1024)
    out = svc.import_test(data, {"stream-b.png": io.BytesIO(big)}, new_id="streamed")
    assert out.stored_assets == ["stream-b.png"]
    assert storage.get("stream-b.png") == big
    # Same id, same bytes -> fine; different bytes -> refused (shared by "streamed").
    svc.import_test(data, {"stream-b.png": io.BytesIO(big)}, new_id="streamed-2")
    with pytest.raises(ContentConflictError, match="другим содержимым"):
        svc.import_test(data, {"stream-b.png": io.BytesIO(PNG)}, new_id="streamed-3")
    with pytest.raises(ContentValidationError, match="слишком большой"):
        svc.import_test(
            data, {"stream-b.png": io.BytesIO(big)}, new_id="streamed-4", max_asset_bytes=1024
        )


def test_upload_asset_stream_and_atomic_name_reservation(
    svc: ContentService, storage: FilesystemStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert svc.upload_asset("s.png", io.BytesIO(PNG)) == "s.png"
    assert storage.get("s.png") == PNG
    with pytest.raises(AssetExistsError):
        storage.put_stream("s.png", io.BytesIO(JPG), exclusive=True)
    assert storage.get("s.png") == PNG  # never overwritten
    assert not list((storage.root / ".incoming").iterdir())  # temp files cleaned up

    # Race: `exists` says free, but another upload grabbed the name first.
    real_exists = storage.exists
    lied: list[str] = []

    def stale_exists(asset_id: str) -> bool:
        if asset_id == "s.png" and not lied:
            lied.append(asset_id)
            return False
        return real_exists(asset_id)

    monkeypatch.setattr(storage, "exists", stale_exists)
    other = PNG + b"\x02"
    assert svc.upload_asset("s.png", other) == "s-2.png"
    assert lied and storage.get("s.png") == PNG and storage.get("s-2.png") == other


def test_concurrent_same_name_uploads_never_clobber(
    svc: ContentService, storage: FilesystemStorage
) -> None:
    blobs = [PNG + bytes([n]) * 2048 for n in range(8)]
    ids: list[str] = []
    errors: list[BaseException] = []
    gate = threading.Barrier(len(blobs))

    def upload(blob: bytes) -> None:
        try:
            gate.wait()
            ids.append(svc.upload_asset("race.png", blob))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=upload, args=(b,)) for b in blobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == len(blobs)  # one distinct id per distinct blob
    assert sorted(storage.get(i) for i in ids) == sorted(blobs)


def test_storage_digest_and_version(storage: FilesystemStorage) -> None:
    storage.put("v.png", PNG)
    v1, d1 = storage.version("v.png"), storage.digest("v.png")
    storage.put_stream("v.png", io.BytesIO(PNG + b"\x00\x01"))
    assert storage.version("v.png") != v1 and storage.digest("v.png") != d1
    assert storage.version("missing.png") is None
    with pytest.raises(FileNotFoundError):
        storage.digest("missing.png")


def test_mp3_sniff_rejects_aac_and_other_layers() -> None:
    assert sniff_media_type(b"\xff\xfb\x90\x64" + b"\x00" * 8) == "audio/mpeg"  # MPEG-1 L3
    assert sniff_media_type(b"\xff\xf3\x90\x64" + b"\x00" * 8) == "audio/mpeg"  # MPEG-2 L3
    assert sniff_media_type(b"\xff\xf1\x50\x80" + b"\x00" * 8) is None  # AAC / ADTS
    assert sniff_media_type(b"\xff\xf9\x50\x80" + b"\x00" * 8) is None  # AAC / ADTS (MPEG-2)
    assert sniff_media_type(b"\xff\xfd\x90\x64" + b"\x00" * 8) is None  # MPEG-1 Layer II
    assert not is_mp3_frame(b"\xff\xfb\xf0\x64")  # bad bitrate index
    assert not is_mp3_frame(b"\xff\xfb\x9c\x64")  # reserved sample rate
    assert not is_mp3_frame(b"\xff\xeb\x90\x64")  # reserved MPEG version
    assert not is_mp3_frame(b"\xff\xfb")  # too short
