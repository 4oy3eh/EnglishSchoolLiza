"""Content engine facade: item-bank CRUD + assets (Phase 3.1) + editing (Phase 13).

`ContentService` ties the persistence `ContentRepository` (tests/sections/items)
together with a `StorageBackend` (asset blobs) so the rest of the platform has a
single entry point for managing the item bank. It deliberately stays thin: row
<-> contract translation lives in the repository, blob handling in the storage
backend. Publishing only flips `draft -> published`; it never invents content
(human approval gates publish, golden rule #5).

Editing without code: `update_test_meta` / `update_section` / `reorder_sections` /
`reorder_items` / `update_item` / `add_item` / `remove_item` / `add_section` /
`remove_section`, plus `duplicate_test`, `export_test`, `import_test` and
`upload_asset`. Every write builds the WHOLE new `Test`, then `_commit` validates it
against the contracts (`editor.validate_test_data`), checks asset references and
global id clashes, applies the locked-test guard (`editor.check_edit_allowed`: a
published test or one with attempts takes only forced cosmetic fixes) and rewrites
it through the repository. Errors are `editor.ContentEditError`
subclasses with Russian messages. Imports and copies always land as **drafts**
(the review queue); nothing here publishes. Uploaded asset files are handled as
seekable streams (validated by their header + size, compared by sha256, copied to
storage in chunks) so an import never holds every blob in memory.
"""

from __future__ import annotations

import copy
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, BinaryIO

from app.content.editor import (
    MAX_ASSET_BYTES,
    SNIFF_BYTES,
    ContentConflictError,
    ContentNotFoundError,
    ContentValidationError,
    all_ids,
    asset_id_from_filename,
    can_reorder_items,
    check_edit_allowed,
    next_copy_id,
    referenced_asset_ids,
    reid_test,
    validate_asset,
    validate_test_data,
)
from app.content.storage import AssetExistsError, StorageBackend, stream_digest
from app.core.logging import get_logger
from app.persistence.repository import ContentRepository
from contracts import Section, Test

log = get_logger(__name__)

META_FIELDS = ("title", "level", "duration_minutes")
SECTION_FIELDS = ("title", "skill", "reported_skill", "stimulus")

# An uploaded asset: in-memory bytes (CLI, tests) or a seekable binary stream
# (the HTTP upload's spooled temp file — never read whole into memory).
AssetData = bytes | BinaryIO


@dataclass
class ImportOutcome:
    """What `import_test` did: the stored draft + which uploaded files were used."""

    test: Test
    replaced: bool
    # `replace=True` over a PUBLISHED test (no attempts): it is now a draft again
    # and waits in the review queue — the UI tells the teacher.
    unpublished: bool = False
    stored_assets: list[str] = field(default_factory=list)
    ignored_assets: list[str] = field(default_factory=list)


def _find_section(data: dict[str, Any], section_id: str) -> dict[str, Any]:
    for section in data["sections"]:
        if section["id"] == section_id:
            return section
    raise ContentNotFoundError(f"Раздел {section_id!r} не найден.")


def _find_item(data: dict[str, Any], item_id: str) -> tuple[dict[str, Any], int]:
    for section in data["sections"]:
        for pos, item in enumerate(section["items"]):
            if item["id"] == item_id:
                return section, pos
    raise ContentNotFoundError(f"Вопрос {item_id!r} не найден.")


def _check_patch(patch: Any, allowed: tuple[str, ...]) -> None:
    if not isinstance(patch, Mapping):
        raise ContentValidationError("Ошибка в данных: ожидался объект с полями.")
    unknown = sorted(set(patch) - set(allowed))
    if unknown:
        raise ContentValidationError(
            f"Неизвестные поля: {', '.join(unknown)}. Можно менять: {', '.join(allowed)}."
        )


def _check_order(order: Any, expected: list[str], what: str) -> list[str]:
    if (
        not isinstance(order, list)
        or not all(isinstance(x, str) for x in order)
        or sorted(order) != sorted(expected)
        or len(set(order)) != len(order)
    ):
        raise ContentValidationError(
            f"Новый порядок должен перечислять каждый {what} ровно один раз."
        )
    return order


def _as_stream(data: AssetData) -> BinaryIO:
    return io.BytesIO(data) if isinstance(data, bytes | bytearray) else data


def _stream_size(src: BinaryIO) -> int:
    src.seek(0, io.SEEK_END)
    size = src.tell()
    src.seek(0)
    return size


def _stream_head(src: BinaryIO) -> bytes:
    src.seek(0)
    head = src.read(SNIFF_BYTES)
    src.seek(0)
    return head


def _validate_stream(asset_id: str, src: BinaryIO, max_bytes: int) -> str:
    return validate_asset(asset_id, _stream_head(src), _stream_size(src), max_bytes=max_bytes)


def _clamp(position: int | None, length: int) -> int:
    if position is None:
        return length
    return max(0, min(int(position), length))


class ContentService:
    """CRUD over the item bank plus asset storage, and validated editing."""

    def __init__(self, repo: ContentRepository, storage: StorageBackend) -> None:
        self.repo = repo
        self.storage = storage

    # -- tests / sections / items ------------------------------------------ #
    def create_test(self, test: Test) -> str:
        return self.repo.add_test(test)

    def get_test(self, test_id: str) -> Test | None:
        return self.repo.get_test(test_id)

    def list_tests(self) -> list[Test]:
        return self.repo.list_tests()

    def publish(self, test_id: str) -> bool:
        """Flip a draft to published (human-approved, golden rule #5)."""
        return self.repo.set_status(test_id, "published")

    def unpublish(self, test_id: str) -> bool:
        return self.repo.set_status(test_id, "draft")

    def delete_test(self, test_id: str) -> bool:
        return self.repo.delete_test(test_id)

    # -- assets ------------------------------------------------------------- #
    def add_asset(
        self, asset_id: str, data: bytes, *, content_type: str | None = None
    ) -> str:
        return self.storage.put(asset_id, data, content_type=content_type)

    def get_asset(self, asset_id: str) -> bytes:
        return self.storage.get(asset_id)

    def has_asset(self, asset_id: str) -> bool:
        try:
            return self.storage.exists(asset_id)
        except ValueError:  # a traversal-unsafe id is never "stored"
            return False

    def delete_asset(self, asset_id: str) -> None:
        self.storage.delete(asset_id)

    def upload_asset(
        self, filename: str, data: AssetData, *, max_bytes: int = MAX_ASSET_BYTES
    ) -> str:
        """Store an uploaded mp3/png/jpg/webp; return its (new or reused) asset id.

        The id comes from the file's base name (directories and `..` dropped, unsafe
        characters replaced). Identical bytes under that id are reused; a different
        blob already stored under it gets `-2`, `-3`, ... — an upload never
        overwrites a stored asset another test may reference. The name is reserved
        with an atomic create-only write (`put_stream(exclusive=True)`), so two
        concurrent uploads of the same name can never overwrite each other: the
        loser re-checks and takes the next suffix (or reuses identical bytes).
        `data` may be bytes or a seekable stream (copied to storage in chunks)."""
        asset_id = asset_id_from_filename(filename)
        src = _as_stream(data)
        media_type = _validate_stream(asset_id, src, max_bytes)
        size = _stream_size(src)
        digest: str | None = None
        stem, dot, ext = asset_id.rpartition(".")
        candidate, n = asset_id, 1
        while True:
            if self.storage.exists(candidate):
                digest = digest or stream_digest(src)
                if self.storage.digest(candidate) == digest:
                    log.info(
                        "content upload_asset id=%s -> reused (identical bytes)", candidate
                    )
                    return candidate
            else:
                try:
                    self.storage.put_stream(
                        candidate, src, content_type=media_type, exclusive=True
                    )
                    break
                except AssetExistsError:
                    log.info(
                        "content upload_asset id=%s taken concurrently -> re-check", candidate
                    )
                    continue  # re-check the same name: identical bytes are reused
            n += 1
            candidate = f"{stem}-{n}{dot}{ext}"
        log.info(
            "content upload_asset name=%r -> id=%s bytes=%d type=%s",
            filename,
            candidate,
            size,
            media_type,
        )
        return candidate

    def missing_assets(self, test: Test, provided: set[str] | None = None) -> list[str]:
        """Referenced asset ids neither stored nor about to be stored (sorted)."""
        have = provided or set()
        return sorted(
            a for a in referenced_asset_ids(test) if a not in have and not self.has_asset(a)
        )

    # -- editing (validated, guarded) --------------------------------------- #
    def _require(self, test_id: str) -> Test:
        test = self.repo.get_test(test_id)
        if test is None:
            raise ContentNotFoundError(f"Тест {test_id!r} не найден.")
        return test

    def _check_ids_free(self, test: Test) -> None:
        sections, items = all_ids(test)
        taken = self.repo.ids_in_use_elsewhere(test.id, sections, items)
        if taken:
            shown = ", ".join(sorted(taken)[:10])
            raise ContentConflictError(
                f"Идентификаторы уже заняты другим тестом: {shown}. Дайте разделам и "
                "вопросам другие идентификаторы (или импортируйте под новым id теста)."
            )

    def _edit(self, test_id: str) -> tuple[Test, dict[str, Any]]:
        old = self._require(test_id)
        return old, copy.deepcopy(old.model_dump(mode="json"))

    def _commit(
        self,
        old: Test,
        data: dict[str, Any],
        *,
        force: bool,
        action: str,
        target: str,
    ) -> Test:
        """Validate -> assets -> id clashes -> published guard -> persist -> log."""
        data["id"] = old.id
        data["status"] = old.status  # editing never changes the publish state
        new = validate_test_data(data)
        missing = self.missing_assets(new)
        if missing:
            log.warning(
                "content edit REFUSED action=%s test=%s target=%s: missing assets %s",
                action,
                old.id,
                target,
                missing,
            )
            raise ContentValidationError(
                "Не найдены файлы: " + ", ".join(missing) + ". Сначала загрузите их."
            )
        if new == old:
            log.info(
                "content edit action=%s test=%s target=%s -> no change", action, old.id, target
            )
            return old
        self._check_ids_free(new)
        attempts = self.repo.count_attempts(old.id)
        try:
            cosmetic = check_edit_allowed(old, new, attempts=attempts, force=force)
        except ContentConflictError:
            log.warning(
                "content edit REFUSED action=%s test=%s target=%s status=%s attempts=%d "
                "force=%s",
                action,
                old.id,
                target,
                old.status,
                attempts,
                force,
            )
            raise
        self.repo.replace_test(new)
        if old.status == "published":
            log.warning(
                "content edit FORCED on PUBLISHED test action=%s test=%s target=%s "
                "attempts=%d (cosmetic text fix)",
                action,
                old.id,
                target,
                attempts,
            )
        elif attempts:
            log.warning(
                "content edit FORCED on a draft WITH %d attempt(s) action=%s test=%s "
                "target=%s (cosmetic text fix)",
                attempts,
                action,
                old.id,
                target,
            )
        log.info(
            "content edit action=%s test=%s target=%s cosmetic=%s status=%s",
            action,
            old.id,
            target,
            cosmetic,
            old.status,
        )
        return new

    def update_test_meta(
        self, test_id: str, patch: Mapping[str, Any], *, force: bool = False
    ) -> Test:
        """Change title / level / duration_minutes."""
        _check_patch(patch, META_FIELDS)
        old, data = self._edit(test_id)
        data.update(copy.deepcopy(dict(patch)))
        return self._commit(old, data, force=force, action="update_meta", target=test_id)

    def set_schedule(
        self,
        test_id: str,
        *,
        opens_at: datetime | None,
        closes_at: datetime | None,
        late_start_grace_minutes: int = 0,
    ) -> Test:
        """Set / clear the exam window (schedule meta — allowed on ANY test).

        Not key-bearing (`editor.SCHEDULE_FIELDS`): it never touches the sections,
        items or key, so it is written as a three-column UPDATE even on a published
        test with attempts (no `force`, no `replace_test`). Running attempts keep
        their deadline (fixed at start, golden rule #3); the window only decides who
        may START and the deadline cap of attempts started from now on."""
        old = self._require(test_id)
        for name, value in (("открытия", opens_at), ("закрытия", closes_at)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ContentValidationError(
                    f"Время {name} окна экзамена должно быть с часовым поясом."
                )
        if opens_at is not None and closes_at is not None and closes_at <= opens_at:
            raise ContentValidationError(
                "Окно экзамена: время закрытия должно быть позже времени открытия."
            )
        if not 0 <= late_start_grace_minutes <= 1440:
            raise ContentValidationError(
                "Запас на поздний старт: от 0 до 1440 минут."
            )
        # Only the window is validated (the checks above mirror the contract's): the
        # content is not touched, so an older test that a newer editor rule would
        # flag must never block a schedule change in the middle of a session.
        new = old.model_copy(
            update={
                "opens_at": opens_at.astimezone(UTC) if opens_at else None,
                "closes_at": closes_at.astimezone(UTC) if closes_at else None,
                "late_start_grace_minutes": late_start_grace_minutes,
            }
        )
        self.repo.set_schedule(
            test_id,
            opens_at=new.opens_at,
            closes_at=new.closes_at,
            late_start_grace_minutes=new.late_start_grace_minutes,
        )
        log.info(
            "content set_schedule test=%s status=%s opens=%s closes=%s grace=%d "
            "(was opens=%s closes=%s grace=%d)",
            test_id,
            old.status,
            new.opens_at.isoformat() if new.opens_at else None,
            new.closes_at.isoformat() if new.closes_at else None,
            new.late_start_grace_minutes,
            old.opens_at.isoformat() if old.opens_at else None,
            old.closes_at.isoformat() if old.closes_at else None,
            old.late_start_grace_minutes,
        )
        return new

    def update_section(
        self, test_id: str, section_id: str, patch: Mapping[str, Any], *, force: bool = False
    ) -> Test:
        """Change a section's title / skill / reported skill / stimulus (text, pool,
        audio, images)."""
        _check_patch(patch, SECTION_FIELDS)
        old, data = self._edit(test_id)
        _find_section(data, section_id).update(copy.deepcopy(dict(patch)))
        return self._commit(
            old, data, force=force, action="update_section", target=section_id
        )

    def reorder_sections(self, test_id: str, order: list[str]) -> Test:
        """Reorder sections (rule #7 constrains items within a section, not sections)."""
        old, data = self._edit(test_id)
        by_id = {s["id"]: s for s in data["sections"]}
        data["sections"] = [by_id[sid] for sid in _check_order(order, list(by_id), "раздел")]
        return self._commit(old, data, force=False, action="reorder_sections", target=test_id)

    def reorder_items(self, test_id: str, section_id: str, order: list[str]) -> Test:
        """Reorder items — ONLY in a section without a shared stimulus (rule #7)."""
        old, data = self._edit(test_id)
        section = _find_section(data, section_id)
        if not can_reorder_items(Section.model_validate(section)):
            log.warning(
                "content reorder_items REFUSED test=%s section=%s: shared stimulus (rule #7)",
                test_id,
                section_id,
            )
            raise ContentConflictError(
                "Порядок вопросов в этом разделе менять нельзя: они идут по общему тексту, "
                "записи или картинкам раздела."
            )
        by_id = {i["id"]: i for i in section["items"]}
        section["items"] = [
            by_id[iid] for iid in _check_order(order, list(by_id), "вопрос раздела")
        ]
        return self._commit(old, data, force=False, action="reorder_items", target=section_id)

    def update_item(
        self, test_id: str, item_id: str, item: Mapping[str, Any], *, force: bool = False
    ) -> Test:
        """Replace one item's fields (prompt, options, key, accepted, rubric, ...).

        The item keeps its id; changing its type is structural (draft only)."""
        if not isinstance(item, Mapping):
            raise ContentValidationError("Ошибка в данных: ожидался объект вопроса.")
        old, data = self._edit(test_id)
        section, pos = _find_item(data, item_id)
        section["items"][pos] = {**copy.deepcopy(dict(item)), "id": item_id}
        return self._commit(old, data, force=force, action="update_item", target=item_id)

    def _free_id(self, test_id: str, used: set[str], fmt: str, *, section: bool) -> str:
        n = len(used) + 1
        while True:
            candidate = fmt.format(test_id=test_id, n=n)
            clash = self.repo.ids_in_use_elsewhere(
                test_id, {candidate} if section else set(), set() if section else {candidate}
            )
            if candidate not in used and not clash:
                return candidate
            n += 1

    def _item_ids(self, data: dict[str, Any]) -> set[str]:
        return {
            str(i.get("id"))
            for s in data["sections"]
            for i in s.get("items", [])
            if isinstance(i, dict)
        }

    def add_item(
        self,
        test_id: str,
        section_id: str,
        item: Mapping[str, Any],
        *,
        position: int | None = None,
    ) -> Test:
        """Insert a new item (id `<test>-q<N>` generated when blank) — draft only."""
        if not isinstance(item, Mapping):
            raise ContentValidationError("Ошибка в данных: ожидался объект вопроса.")
        old, data = self._edit(test_id)
        section = _find_section(data, section_id)
        new_item = copy.deepcopy(dict(item))
        if not str(new_item.get("id") or "").strip():
            new_item["id"] = self._free_id(
                old.id, self._item_ids(data), "{test_id}-q{n}", section=False
            )
        section["items"].insert(_clamp(position, len(section["items"])), new_item)
        return self._commit(
            old, data, force=False, action="add_item", target=f"{section_id}/{new_item['id']}"
        )

    def remove_item(self, test_id: str, item_id: str) -> Test:
        old, data = self._edit(test_id)
        section, pos = _find_item(data, item_id)
        if len(section["items"]) == 1:
            raise ContentValidationError(
                "В разделе должен остаться хотя бы один вопрос — удалите раздел целиком."
            )
        del section["items"][pos]
        return self._commit(old, data, force=False, action="remove_item", target=item_id)

    def add_section(
        self, test_id: str, section: Mapping[str, Any], *, position: int | None = None
    ) -> Test:
        """Insert a new section (blank ids generated for it and its items) — draft only."""
        if not isinstance(section, Mapping):
            raise ContentValidationError("Ошибка в данных: ожидался объект раздела.")
        old, data = self._edit(test_id)
        new_section = copy.deepcopy(dict(section))
        if not str(new_section.get("id") or "").strip():
            new_section["id"] = self._free_id(
                old.id, {s["id"] for s in data["sections"]}, "{test_id}-sec{n}", section=True
            )
        data["sections"].insert(_clamp(position, len(data["sections"])), new_section)
        items = new_section.get("items")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and not str(it.get("id") or "").strip():
                    it["id"] = self._free_id(
                        old.id, self._item_ids(data), "{test_id}-q{n}", section=False
                    )
        return self._commit(
            old, data, force=False, action="add_section", target=str(new_section["id"])
        )

    def remove_section(self, test_id: str, section_id: str) -> Test:
        old, data = self._edit(test_id)
        section = _find_section(data, section_id)
        if len(data["sections"]) == 1:
            raise ContentValidationError("В тесте должен остаться хотя бы один раздел.")
        data["sections"].remove(section)
        return self._commit(old, data, force=False, action="remove_section", target=section_id)

    # -- copy / export / import --------------------------------------------- #
    def duplicate_test(self, test_id: str) -> Test:
        """A draft copy `<id>-v2` (or the next free `-vN`) with rebased ids."""
        old = self._require(test_id)
        new_id = next_copy_id(test_id, self.repo.test_exists)
        copy_ = reid_test(old, new_id, status="draft")
        self._check_ids_free(copy_)
        self.repo.add_test(copy_)
        log.info(
            "content duplicate test=%s -> %s (draft, %d sections, %d items) -> review queue",
            test_id,
            new_id,
            len(copy_.sections),
            sum(len(s.items) for s in copy_.sections),
        )
        return copy_

    def export_test(self, test_id: str) -> Test:
        """The authoring `Test` WITH its answer key (teacher-only download / CLI)."""
        test = self._require(test_id)
        log.info(
            "content export test=%s sections=%d items=%d",
            test_id,
            len(test.sections),
            sum(len(s.items) for s in test.sections),
        )
        return test

    def import_test(
        self,
        data: Mapping[str, Any] | str | bytes,
        assets: Mapping[str, AssetData] | None = None,
        *,
        replace: bool = False,
        new_id: str | None = None,
        max_asset_bytes: int = MAX_ASSET_BYTES,
    ) -> ImportOutcome:
        """Validate an authored Test JSON (+ its asset files) and store it as a DRAFT.

        * `new_id` re-ids the test (and its sections / items) before storing;
        * an id collision is refused unless `replace=True` AND the existing test has
          no attempts (its roster is kept);
        * every referenced asset must be uploaded (file name = asset id) or already
          stored — the missing ids are listed in the error;
        * an uploaded file that differs from a blob already stored under that id is
          refused unless `replace=True` and no other test references it;
        * `replace=True` over a PUBLISHED test drops it back to draft (WARNING log,
          `ImportOutcome.unpublished`) — it must be approved again.
        Asset values may be bytes or seekable streams (compared by sha256 and copied
        to storage in chunks). Never publishes (golden rule #5)."""
        try:
            test = self._parse_import(data)
            if new_id is not None and new_id.strip():
                test = reid_test(test, new_id.strip(), status="draft")
        except RecursionError as exc:  # absurdly deep JSON nesting
            log.warning("content import REFUSED: JSON nested too deeply")
            raise ContentValidationError(
                "Файл теста слишком глубоко вложен — это не похоже на тест (JSON)."
            ) from exc

        uploaded = {name: _as_stream(blob) for name, blob in (assets or {}).items()}
        referenced = referenced_asset_ids(test)
        ignored = sorted(a for a in uploaded if a not in referenced)
        used = {a: b for a, b in uploaded.items() if a in referenced}
        types = {a: _validate_stream(a, b, max_asset_bytes) for a, b in used.items()}
        missing = self.missing_assets(test, set(used))
        if missing:
            log.warning("content import test=%s REFUSED: missing assets %s", test.id, missing)
            raise ContentValidationError(
                "Не найдены файлы, на которые ссылается тест: "
                + ", ".join(missing)
                + ". Приложите их к импорту (имя файла = идентификатор файла в тесте) "
                "или загрузите заранее."
            )

        existing = self.repo.get_test(test.id)
        exists = existing is not None
        if exists:
            if not replace:
                log.warning("content import test=%s REFUSED: id already exists", test.id)
                raise ContentConflictError(
                    f"Тест с id {test.id!r} уже есть в банке. Включите «заменить» (если по "
                    "нему ещё нет работ учеников) или импортируйте под новым id."
                )
            attempts = self.repo.count_attempts(test.id)
            if attempts:
                log.warning(
                    "content import test=%s REFUSED replace: %d attempt(s)", test.id, attempts
                )
                raise ContentConflictError(
                    f"Тест {test.id!r} нельзя заменить: по нему уже есть работы учеников "
                    f"({attempts}). Импортируйте под новым id."
                )
        self._check_ids_free(test)

        shared: set[str] = set()
        for other in self.repo.list_tests():
            if other.id != test.id:
                shared |= referenced_asset_ids(other)
        for asset_id, blob in used.items():
            if self.has_asset(asset_id) and self.storage.digest(asset_id) != stream_digest(
                blob
            ):
                if not replace or asset_id in shared:
                    log.warning(
                        "content import test=%s REFUSED: asset %s differs from the stored "
                        "one (shared=%s)",
                        test.id,
                        asset_id,
                        asset_id in shared,
                    )
                    raise ContentConflictError(
                        f"Файл «{asset_id}» уже есть на сервере с другим содержимым"
                        + (" и используется другим тестом" if asset_id in shared else "")
                        + ". Переименуйте файл (и ссылку на него в тесте)."
                    )

        for asset_id, blob in used.items():
            self.storage.put_stream(asset_id, blob, content_type=types[asset_id])
        unpublished = existing is not None and existing.status == "published"
        if exists:
            self.repo.replace_test(test)
        else:
            self.repo.add_test(test)
        if unpublished:
            log.warning(
                "content import test=%s REPLACED a PUBLISHED test -> now draft, "
                "unpublished until approved again",
                test.id,
            )
        log.info(
            "content import test=%s replaced=%s sections=%d items=%d assets_stored=%d "
            "ignored=%d -> draft (review queue)",
            test.id,
            exists,
            len(test.sections),
            sum(len(s.items) for s in test.sections),
            len(used),
            len(ignored),
        )
        return ImportOutcome(
            test=test,
            replaced=exists,
            unpublished=unpublished,
            stored_assets=sorted(used),
            ignored_assets=ignored,
        )

    @staticmethod
    def _parse_import(data: Mapping[str, Any] | str | bytes) -> Test:
        if isinstance(data, str | bytes):
            try:
                data = json.loads(data)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ContentValidationError("Файл теста не является корректным JSON.") from exc
        if not isinstance(data, Mapping):
            raise ContentValidationError("Файл теста должен содержать объект Test (JSON).")
        return validate_test_data({**dict(data), "status": "draft"})
