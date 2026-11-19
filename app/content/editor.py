"""Content editing without code: validation, the publish-safety guard, re-id, assets.

Pure helpers behind `ContentService`'s update / import / duplicate methods. Every
edit is expressed as "the whole new `Test`" and goes through ONE gate:

1. **Contract validation** (`validate_test_data`) — the Pydantic authoring models
   (golden rule #4: `contracts/` is the source of truth) plus the semantic checks
   Pydantic cannot express (a `single_choice` `correct` must be one of its option
   keys, a `matching` item needs the section's pool, ids unique, ...). Failures
   raise `ContentValidationError` with a **Russian** message — it is shown to the
   teacher verbatim.
2. **The safety guard** (`check_edit_allowed`) — a draft WITHOUT student attempts
   is edited freely. A test that is published OR already has attempts (a draft
   after unpublish included) only takes *cosmetic* text fixes (`is_cosmetic_change`:
   titles, prompts, stimulus text, bullets — never option / matching-pool texts,
   which carry the key) and only with an explicit `force` (logged at WARNING by
   the caller). Anything touching the answer key or the item / section set is 409:
   a published test without attempts may be unpublished first; with attempts the
   only way is a draft copy (`duplicate_test` / `reid_test`) — otherwise a later
   regrade would re-score finished work against the new key.

Nothing here publishes (golden rule #5) and nothing reorders items inside a
section that shares a stimulus (golden rule #7, `can_reorder_items`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from app.content.storage import _validate_asset_id
from app.core.logging import get_logger
from app.grading import item_max_points
from app.grading.kwt import answer_word_count, count_words, has_key_word, kwt_tokens
from app.grading.writing_scales import has_scale
from contracts import (
    AudioAssetStimulus,
    ColourTaskItem,
    GappedTextStimulus,
    ImageOption,
    ImageSetStimulus,
    KeyWordTransformationItem,
    MatchingItem,
    MatchingPoolStimulus,
    PassageTextStimulus,
    Section,
    SingleChoiceItem,
    Test,
    TextOption,
)
from contracts.content import KWT_GAP_RE, kwt_word_limits

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors (status = the HTTP code the admin layer maps them to).
# --------------------------------------------------------------------------- #
class ContentEditError(Exception):
    """Base for a refused edit. The message is Russian (shown to the teacher)."""

    status: int = 400


class ContentValidationError(ContentEditError):
    """The new content does not satisfy the contracts -> 400."""

    status = 400


class ContentNotFoundError(ContentEditError):
    """Unknown test / section / item -> 404."""

    status = 404


class ContentConflictError(ContentEditError):
    """The target is in the wrong state for this change (published, id taken) -> 409."""

    status = 409


# --------------------------------------------------------------------------- #
# Russian rendering of Pydantic validation errors.
# --------------------------------------------------------------------------- #
_FIELD_RU = {
    "id": "идентификатор",
    "title": "название",
    "level": "уровень",
    "status": "статус",
    "duration_minutes": "длительность (мин)",
    "sections": "разделы",
    "skill": "навык",
    "reported_skill": "навык в отчёте",
    "stimulus": "материал раздела",
    "items": "вопросы",
    "item_type": "тип вопроса",
    "kind": "тип",
    "prompt": "текст вопроса",
    "image": "картинка к вопросу",
    "options": "варианты ответа",
    "key": "ключ",
    "text": "текст",
    "alt": "подпись картинки",
    "asset_id": "файл",
    "asset_ids": "картинки",
    "images": "картинки",
    "correct": "правильный ответ",
    "accepted": "принимаемые ответы",
    "accepted_variants": "допустимые варианты написания",
    "rejected": "неверные формы",
    "word_min": "минимум слов",
    "bullet_points": "пункты задания",
    "rubric": "критерии оценивания",
    "grade_mode": "способ оценивания",
    "optional": "задание на выбор",
    "palette": "палитра",
    "points": "баллы",
    "stem_word": "исходное слово",
    "max_words": "максимум слов",
    "min_words": "минимум слов",
    "key_word": "ключевое слово",
    "gapped_sentence": "второе предложение с пропуском",
    "answers": "варианты ответа (части A | B)",
    "part_a": "часть A",
    "part_b": "часть B",
    "each_option_once": "каждый вариант один раз",
    "audio": "аудиозапись",
    "plays": "число прослушиваний",
    "look_through_seconds": "время на просмотр (с)",
    "preview_asset_id": "вступительная запись",
    "locked": "запрет перемотки",
    "opens_at": "открытие окна экзамена",
    "closes_at": "закрытие окна экзамена",
    "late_start_grace_minutes": "запас на поздний старт (мин)",
}
# Discriminator tags Pydantic puts into the error location; not user-facing.
_TAGS = {
    "single_choice",
    "gap_fill",
    "matching",
    "open_writing",
    "colour_task",
    "key_word_transformation",
    "passage_text",
    "audio_asset",
    "image_set",
    "gapped_text",
    "matching_pool",
    "text",
    "image",
}
_INDEX_RU = {"sections": "раздел", "items": "вопрос", "options": "вариант"}


def _loc_ru(loc: Iterable[Any]) -> str:
    """('sections', 0, 'items', 1, 'single_choice', 'prompt') -> 'раздел 1 → вопрос 2 → …'."""
    seq = list(loc)
    parts: list[str] = []
    for idx, part in enumerate(seq):
        prev = seq[idx - 1] if idx else None
        if isinstance(part, int):
            word = _INDEX_RU.get(prev) if isinstance(prev, str) else None
            if word and parts:
                parts[-1] = f"{word} {part + 1}"
            else:
                parts.append(f"№{part + 1}")
            continue
        # A discriminator tag follows a list index or `stimulus` and is never last.
        is_tag = (
            part in _TAGS
            and idx < len(seq) - 1
            and (isinstance(prev, int) or prev == "stimulus")
        )
        if not is_tag:
            parts.append(_FIELD_RU.get(str(part), str(part)))
    return " → ".join(parts)


def _msg_ru(err: Mapping[str, Any]) -> str:
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "missing":
        return "обязательное поле не заполнено"
    if kind == "too_short":
        return f"нужно не меньше {ctx.get('min_length', 1)} элемент(ов)"
    if kind == "greater_than":
        return f"должно быть больше {ctx.get('gt')}"
    if kind == "greater_than_equal":
        return f"должно быть не меньше {ctx.get('ge')}"
    if kind == "less_than_equal":
        return f"должно быть не больше {ctx.get('le')}"
    if kind in ("int_parsing", "int_type", "int_from_float"):
        return "должно быть целым числом"
    if kind == "string_type":
        return "должно быть текстом"
    if kind in ("bool_parsing", "bool_type"):
        return "должно быть да/нет"
    if kind == "list_type":
        return "должно быть списком"
    if kind in ("dict_type", "model_type", "model_attributes_type"):
        return "неверная структура"
    if kind == "literal_error":
        expected = str(ctx.get("expected", "")).replace(" or ", " или ")
        return f"недопустимое значение (допустимо: {expected})"
    if kind == "union_tag_invalid":
        return f"неизвестный тип «{ctx.get('tag', '')}»"
    if kind == "union_tag_not_found":
        return "не указан тип"
    if kind == "extra_forbidden":
        return "лишнее поле"
    return str(err.get("msg", "неверное значение"))


def ru_validation_message(exc: ValidationError, *, limit: int = 5) -> str:
    """A compact Russian summary of a Pydantic `ValidationError` (first `limit` errors)."""
    lines = []
    errors = exc.errors()
    for err in errors[:limit]:
        where = _loc_ru(err.get("loc", ()))
        where = where[:1].upper() + where[1:]
        lines.append(f"{where}: {_msg_ru(err)}" if where else _msg_ru(err))
    more = len(errors) - limit
    if more > 0:
        lines.append(f"…и ещё ошибок: {more}")
    return "Ошибка в данных теста. " + "; ".join(lines) + "."


# --------------------------------------------------------------------------- #
# Validation (contracts + semantic checks).
# --------------------------------------------------------------------------- #
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


def _check_id(value: str, what: str, errors: list[str]) -> None:
    if not ID_RE.fullmatch(value or ""):
        errors.append(
            f"{what} «{value}»: идентификатор может содержать только латинские буквы, "
            "цифры, точку, дефис и подчёркивание (до 120 символов)"
        )


def _check_asset(value: str | None, what: str, errors: list[str]) -> None:
    if value is None:
        return
    try:
        _validate_asset_id(value)
    except ValueError:
        errors.append(f"{what}: недопустимое имя файла «{value}»")


def semantic_errors(test: Test) -> list[str]:
    """Rules the Pydantic models cannot express. Empty list = valid (Russian text)."""
    errors: list[str] = []
    _check_id(test.id, "Тест", errors)
    if not test.title.strip():
        errors.append("Название теста не может быть пустым")
    seen_sections: set[str] = set()
    seen_items: set[str] = set()
    for s_no, section in enumerate(test.sections, start=1):
        where_s = f"Раздел {s_no}"
        _check_id(section.id, where_s, errors)
        if section.id in seen_sections:
            errors.append(f"{where_s}: идентификатор «{section.id}» повторяется")
        seen_sections.add(section.id)
        stim = section.stimulus
        for asset in getattr(stim, "images", []):
            _check_asset(asset, f"{where_s} → картинки", errors)
        for attr in ("asset_id", "preview_asset_id"):
            _check_asset(getattr(stim, attr, None), f"{where_s} → файл", errors)
        pool_audio = stim.audio if isinstance(stim, MatchingPoolStimulus) else None
        if pool_audio is not None:
            _check_asset(pool_audio.asset_id, f"{where_s} → аудиозапись", errors)
            _check_asset(pool_audio.preview_asset_id, f"{where_s} → аудиозапись", errors)
        for asset in getattr(stim, "asset_ids", []):
            _check_asset(asset, f"{where_s} → картинки", errors)
        pool_keys: list[str] = []
        if isinstance(stim, MatchingPoolStimulus):
            pool_keys = [o.key for o in stim.options]
            if any(not k.strip() for k in pool_keys):
                errors.append(f"{where_s}: у варианта списка пустая буква")
            if len(set(pool_keys)) != len(pool_keys):
                errors.append(f"{where_s}: буквы вариантов списка повторяются")
            if any(not o.text.strip() for o in stim.options):
                errors.append(f"{where_s}: у варианта списка пустой текст")
        for i_no, item in enumerate(section.items, start=1):
            where = f"{where_s} → вопрос {i_no}"
            _check_id(item.id, where, errors)
            if item.id in seen_items:
                errors.append(f"{where}: идентификатор «{item.id}» повторяется")
            seen_items.add(item.id)
            if isinstance(item, SingleChoiceItem):
                keys = [o.key for o in item.options]
                if any(not k.strip() for k in keys):
                    errors.append(f"{where}: у варианта пустая буква")
                if len(set(keys)) != len(keys):
                    errors.append(f"{where}: буквы вариантов повторяются")
                if item.correct not in keys:
                    errors.append(
                        f"{where}: правильный ответ «{item.correct}» не совпадает ни с "
                        "одним вариантом"
                    )
                for o_no, opt in enumerate(item.options, start=1):
                    if isinstance(opt, TextOption) and not opt.text.strip():
                        errors.append(f"{where} → вариант {o_no}: пустой текст")
                    if isinstance(opt, ImageOption):
                        _check_asset(opt.asset_id, f"{where} → вариант {o_no}", errors)
                _check_asset(item.image, f"{where} → картинка", errors)
            elif item.item_type == "gap_fill":
                if any(not a.strip() for a in item.accepted):
                    errors.append(f"{where}: пустой принимаемый ответ")
                if any(not a.strip() for a in item.accepted_variants):
                    errors.append(f"{where}: пустой вариант написания")
                if any(not a.strip() for a in getattr(item, "rejected", [])):
                    errors.append(f"{where}: пустая неверная форма")
                if item.stem_word is not None and not item.stem_word.strip():
                    errors.append(f"{where}: пустое исходное слово")
                if item.max_words is not None:
                    for ans in (*item.accepted, *item.accepted_variants):
                        n = answer_word_count(ans, test.level)
                        if n > item.max_words:
                            errors.append(
                                f"{where}: принимаемый ответ «{ans}» длиннее "
                                f"разрешённого ({n} > {item.max_words} сл.)"
                            )
            elif isinstance(item, KeyWordTransformationItem):
                _kwt_errors(item, where, test.level, errors)
            elif isinstance(item, MatchingItem):
                if not isinstance(stim, MatchingPoolStimulus):
                    errors.append(
                        f"{where}: вопрос на сопоставление требует, чтобы материал "
                        "раздела был списком вариантов (A–H)"
                    )
                elif item.correct not in pool_keys:
                    errors.append(
                        f"{where}: правильный ответ «{item.correct}» не найден в списке "
                        "вариантов раздела"
                    )
            elif item.item_type == "open_writing":
                if any(not b.strip() for b in item.bullet_points):
                    errors.append(f"{where}: пустой пункт задания")
            elif item.item_type == "colour_task":
                _check_asset(item.asset_id, f"{where} → картинка", errors)
        _check_choose_one_points(section, where_s, errors, test.level)
    _check_audio_tracks(test, errors)
    return errors


def _kwt_errors(
    item: KeyWordTransformationItem, where: str, level: str, errors: list[str]
) -> None:
    """A key word transformation the grader can actually mark: one gap in the
    second sentence, a non-blank key word, non-blank halves, and every full answer
    (part A + part B) inside the word limits and containing the key word."""
    if not item.key_word.strip():
        errors.append(f"{where}: пустое ключевое слово")
    gaps = len(KWT_GAP_RE.findall(item.gapped_sentence))
    if gaps != 1:
        errors.append(
            f"{where}: во втором предложении должен быть ровно один пропуск «____» "
            f"(найдено: {gaps})"
        )
    lo, hi = kwt_word_limits(item, level)
    if lo > hi:
        errors.append(f"{where}: минимум слов больше максимума ({lo} > {hi})")
    for a_no, ans in enumerate(item.answers, start=1):
        halves = [*ans.part_a, *ans.part_b]
        if any(not kwt_tokens(h) for h in halves):
            errors.append(f"{where} → вариант ответа {a_no}: пустая часть")
            continue
        for a in ans.part_a:
            for b in ans.part_b:
                tokens = kwt_tokens(f"{a} {b}")
                n = count_words(tokens, level)
                if not lo <= n <= hi:
                    errors.append(
                        f"{where} → вариант ответа {a_no}: «{a} | {b}» — {n} сл., "
                        f"допустимо {lo}–{hi}"
                    )
                elif item.key_word.strip() and not has_key_word(tokens, item.key_word):
                    errors.append(
                        f"{where} → вариант ответа {a_no}: «{a} | {b}» не содержит "
                        f"ключевое слово {item.key_word.upper()}"
                    )


def _check_choose_one_points(
    section: Section, where_s: str, errors: list[str], level: str | None = None
) -> None:
    """The «задание на выбор» tasks of one section are alternatives: exactly ONE
    counts, so they must be worth the same (else the student's choice would move
    the maximum). Compared as what they are WORTH (`item_max_points`), so an
    unset `points` (the default 5) equals an explicit 5."""
    weights = {
        item_max_points(item, level)
        for item in section.items
        if item.item_type == "open_writing" and item.optional
    }
    if len(weights) > 1:
        errors.append(
            f"{where_s}: у заданий на выбор должны быть одинаковые баллы "
            "(засчитывается только одно из них)"
        )


def _check_audio_tracks(test: Test, errors: list[str]) -> None:
    """Per-part listening audio: one recording = one run of consecutive sections.

    A section whose audio file differs from the previous audio section's starts a
    new track, the same file continues it (app/delivery/audio.py). Using a file
    again after another one (A, B, A) would make two tracks share one stored
    progress — refused. The sound-check clip must not be the track itself."""
    seen: set[str] = set()
    current: str | None = None
    for s_no, section in enumerate(test.sections, start=1):
        stim = section.stimulus
        if section.skill != "listening":
            current = None  # the runner opens a new block: no track continues across it
        if isinstance(stim, MatchingPoolStimulus) and stim.audio is not None:
            stim = stim.audio  # a matching part with its own recording (C1/C2 Part 4)
        if not isinstance(stim, AudioAssetStimulus):
            continue
        if stim.preview_asset_id and stim.preview_asset_id == stim.asset_id:
            errors.append(
                f"Раздел {s_no}: вступление / проверка звука не может быть той же "
                "записью, что и сама запись части"
            )
        if section.skill != "listening":
            continue
        if stim.asset_id != current:
            if stim.asset_id in seen:
                errors.append(
                    f"Раздел {s_no}: запись «{stim.asset_id}» уже звучала в одной из "
                    "предыдущих частей — одна запись может идти только в соседних "
                    "разделах подряд"
                )
            seen.add(stim.asset_id)
            current = stim.asset_id


def semantic_warnings(test: Test) -> list[str]:
    """Advisories that do NOT refuse the edit (Russian; logged at WARNING by
    `validate_test_data`): a B2-C2 writing task without `points` (graded out of
    the Cambridge 20 by default — set it explicitly for clarity)."""
    out: list[str] = []
    if not has_scale(test.level):
        return out
    for s_no, section in enumerate(test.sections, start=1):
        for i_no, item in enumerate(section.items, start=1):
            if item.item_type == "open_writing" and item.points is None:
                out.append(
                    f"Раздел {s_no} → вопрос {i_no}: у письменного задания уровня "
                    "B2–C2 не указаны баллы — засчитывается из 20 (4 критерия × 5)"
                )
    return out


def validate_test_data(data: Mapping[str, Any] | Test) -> Test:
    """Validate a whole authoring test (Pydantic contract + semantics) or raise 400."""
    try:
        test = data if isinstance(data, Test) else Test.model_validate(dict(data))
        # Re-validate a model too: in-place edits on dumps can bypass field checks.
        test = Test.model_validate(test.model_dump(mode="json"))
    except ValidationError as exc:
        raise ContentValidationError(ru_validation_message(exc)) from exc
    errors = semantic_errors(test)
    if errors:
        shown = "; ".join(errors[:5])
        if len(errors) > 5:
            shown += f"; …и ещё ошибок: {len(errors) - 5}"
        raise ContentValidationError(f"Ошибка в данных теста. {shown}.")
    for warning in semantic_warnings(test):
        log.warning("content validate test=%s: %s", test.id, warning)
    return test


# --------------------------------------------------------------------------- #
# Asset references.
# --------------------------------------------------------------------------- #
def referenced_asset_ids(test: Test) -> set[str]:
    """Every `asset_id` the test's stimuli and image options point at.

    Used to validate that an authored test does not reference a blob we are not
    about to store (which would 404 in the runner).
    """
    ids: set[str] = set()
    for section in test.sections:
        stimulus = section.stimulus
        # Context images can sit on any text/audio stimulus.
        ids.update(getattr(stimulus, "images", []))
        audio = stimulus.audio if isinstance(stimulus, MatchingPoolStimulus) else stimulus
        if isinstance(audio, AudioAssetStimulus):
            ids.add(audio.asset_id)
            ids.update(audio.images)
            if audio.preview_asset_id is not None:
                ids.add(audio.preview_asset_id)
        elif isinstance(stimulus, ImageSetStimulus):
            ids.update(stimulus.asset_ids)
        for item in section.items:
            if isinstance(item, SingleChoiceItem):
                if item.image is not None:
                    ids.add(item.image)
                ids.update(o.asset_id for o in item.options if isinstance(o, ImageOption))
            elif isinstance(item, ColourTaskItem):
                ids.add(item.asset_id)
    return ids


# --------------------------------------------------------------------------- #
# Cosmetic vs structural change (the published-test guard).
# --------------------------------------------------------------------------- #
# Free-text fields a typo fix may touch on a locked (published / attempted) test.
# Everything else — the answer key (`correct` / `accepted` / `accepted_variants` /
# `rejected` / `rubric` / colour `key`), ids and order, level and duration, audio
# settings — is the "key signature" and needs a draft.
# `gapped_sentence` (KWT) and `stem_word` (word formation) are display text like a
# prompt: grading never reads them.
COSMETIC_FIELDS = frozenset(
    {"title", "text", "prompt", "alt", "bullet_points", "status", "gapped_sentence", "stem_word"}
)
# Containers whose WHOLE content is key-bearing, `text` / `alt` included: the
# options of a single_choice item and the letters + texts of a matching pool.
# Swapping the texts of A and B changes which answer is right without touching
# `correct`, so an option text is never a "typo fix" on a locked test.
KEY_BEARING_CONTAINERS = frozenset({"options"})
# The exam window (top-level `Test` fields). Schedule meta, not content: it never
# touches grading or the attempt layout, so a locked test takes it WITHOUT `force`
# (`check_edit_allowed`), and it is not part of the key signature.
SCHEDULE_FIELDS = frozenset({"opens_at", "closes_at", "late_start_grace_minutes"})


def _strip_cosmetic(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in KEY_BEARING_CONTAINERS:
                out[k] = v  # kept verbatim: every option field is part of the key
            elif k not in COSMETIC_FIELDS:
                out[k] = _strip_cosmetic(v)
        return out
    if isinstance(value, list):
        return [_strip_cosmetic(v) for v in value]
    return value


def key_signature(test: Test) -> Any:
    """Everything about a test that grading or the item set depends on."""
    return _strip_cosmetic(test.model_dump(mode="json", exclude=set(SCHEDULE_FIELDS)))


def is_schedule_only_change(old: Test, new: Test) -> bool:
    """True when the two tests differ at most in the exam window (`SCHEDULE_FIELDS`)."""
    keep = set(SCHEDULE_FIELDS)
    return old.model_dump(mode="json", exclude=keep) == new.model_dump(
        mode="json", exclude=keep
    )


def is_cosmetic_change(old: Test, new: Test) -> bool:
    """True when only free text changed (no key / option / item-set / structure change)."""
    return key_signature(old) == key_signature(new)


def is_locked(test: Test, attempts: int) -> bool:
    """A published test, or any test students have already written, is locked."""
    return test.status == "published" or attempts > 0


def check_edit_allowed(old: Test, new: Test, *, attempts: int, force: bool) -> bool:
    """Raise `ContentConflictError` unless this edit may be saved; return `cosmetic`.

    * draft without attempts -> anything goes (the review queue approves it before
      it goes live);
    * published OR with attempts (draft after unpublish too) -> key / option /
      structure changes are refused: without attempts "unpublish or make a draft
      copy"; with attempts ONLY a draft copy («Создать черновик-копию»), because a
      regrade would re-score finished work against the new key and the attempt
      layout is rebuilt from the edited sections. Cosmetic text fixes need an
      explicit `force` (the caller logs them at WARNING);
    * a change of the exam window ONLY (`SCHEDULE_FIELDS`) is always allowed, no
      `force`: it is not key-bearing and a running session must stay re-schedulable.
    """
    if is_schedule_only_change(old, new):
        return True
    cosmetic = is_cosmetic_change(old, new)
    published = old.status == "published"
    if not is_locked(old, attempts):
        return cosmetic
    if not cosmetic:
        if attempts:
            state = " и тест опубликован" if published else ""
            raise ContentConflictError(
                f"По тесту уже есть работы учеников ({attempts}){state}. Ключ ответов, "
                "варианты ответа, состав вопросов и разделов в нём менять нельзя — иначе "
                "уже сданные работы при переоценке будут проверены по новому ключу. "
                "Нажмите «Создать черновик-копию» и правьте копию (снятие с публикации "
                "здесь не поможет)."
            )
        raise ContentConflictError(
            "Тест опубликован. Менять ключ ответов, варианты ответа, состав вопросов или "
            "разделов можно только в черновике: снимите тест с публикации или создайте "
            "черновик-копию."
        )
    if not force:
        if attempts:
            what = "Тест опубликован и по нему" if published else "По тесту"
            raise ContentConflictError(
                f"{what} уже есть работы учеников ({attempts}). Исправление текста нужно "
                "подтвердить (в редакторе — галочка «исправить только опечатки»)."
            )
        raise ContentConflictError(
            "Тест опубликован. Исправление текста в опубликованном тесте нужно "
            "подтвердить (в редакторе — галочка «исправить только опечатки»)."
        )
    return cosmetic


# --------------------------------------------------------------------------- #
# Item order (golden rule #7).
# --------------------------------------------------------------------------- #
def can_reorder_items(section: Section) -> bool:
    """Items may be reordered only in a section with NO shared stimulus content.

    A passage, gapped text, recording, picture set or matching pool binds the
    items to its order (rule #7). Only an empty text stimulus (a section of
    standalone questions) leaves the order free.
    """
    stim = section.stimulus
    if isinstance(stim, PassageTextStimulus | GappedTextStimulus):
        return not stim.text.strip() and not stim.images
    return False


# --------------------------------------------------------------------------- #
# Re-id (draft copy / import under a new id).
# --------------------------------------------------------------------------- #
_VERSION_SUFFIX = re.compile(r"-v(\d+)$")


def _rebase(value: str, old_prefix: str, new_prefix: str) -> str:
    if value == old_prefix:
        return new_prefix
    if value.startswith(old_prefix + "-"):
        return new_prefix + value[len(old_prefix) :]
    return f"{new_prefix}-{value}"


def reid_test(test: Test, new_id: str, *, status: str = "draft") -> Test:
    """A copy of `test` under `new_id`: section / item ids rebased onto it.

    Section and item ids are primary keys across the whole bank, so a copy needs
    its own. Ids that start with the old test id keep their suffix (`…-q12` stays
    `…-q12`, so question numbering survives); others get `new_id-` prepended.
    Assets are shared by reference (blobs are immutable stimulus content).
    """
    data = test.model_dump(mode="json")
    data["id"] = new_id
    data["status"] = status
    for section in data["sections"]:
        section["id"] = _rebase(section["id"], test.id, new_id)
        for item in section["items"]:
            item["id"] = _rebase(item["id"], test.id, new_id)
    return validate_test_data(data)


def next_copy_id(test_id: str, exists: Callable[[str], bool]) -> str:
    """`<base>-v2`, `-v3`, ... — the first id not taken (a `-vN` suffix is reused)."""
    m = _VERSION_SUFFIX.search(test_id)
    base = test_id[: m.start()] if m else test_id
    n = int(m.group(1)) + 1 if m else 2
    while exists(f"{base}-v{n}"):
        n += 1
    return f"{base}-v{n}"


def all_ids(test: Test) -> tuple[set[str], set[str]]:
    """(section ids, item ids) of a test."""
    return (
        {s.id for s in test.sections},
        {i.id for s in test.sections for i in s.items},
    )


# --------------------------------------------------------------------------- #
# Asset upload validation.
# --------------------------------------------------------------------------- #
MAX_ASSET_BYTES = 50 * 1024 * 1024
ALLOWED_ASSET_TYPES = {
    ".mp3": "audio/mpeg",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_FAMILY_RU = {"audio/mpeg": "mp3", "image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP"}


def is_mp3_frame(data: bytes) -> bool:
    """An MPEG-1/2/2.5 **Layer III** frame header (mp3 without an ID3 tag).

    The 11-bit frame sync alone (`FFE`) also matches AAC/ADTS (layer bits `00`),
    MP1/MP2 and random bytes, so the version (not reserved), layer (== III),
    bitrate (not "bad") and sample-rate (not reserved) fields are checked too."""
    if len(data) < 3 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        return False
    version = (data[1] >> 3) & 0b11  # 01 = reserved
    layer = (data[1] >> 1) & 0b11  # 01 = Layer III (00 = ADTS / reserved)
    bitrate = data[2] >> 4  # 1111 = bad
    sample_rate = (data[2] >> 2) & 0b11  # 11 = reserved
    return version != 0b01 and layer == 0b01 and bitrate != 0b1111 and sample_rate != 0b11


def sniff_media_type(data: bytes) -> str | None:
    """The real type by magic bytes (only the allowed upload types), else None.

    AAC (ADTS) is deliberately NOT accepted as mp3 (see `is_mp3_frame`)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if is_mp3_frame(data):
        return "audio/mpeg"
    return None


SNIFF_BYTES = 16  # enough header for `sniff_media_type`


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def validate_asset(
    asset_id: str, head: bytes, size: int, *, max_bytes: int = MAX_ASSET_BYTES
) -> str:
    """Check type (extension AND magic bytes agree) + size; return the media type.

    Takes only the first `SNIFF_BYTES` and the total size, so a large upload is
    validated without being loaded into memory."""
    ext = _extension(asset_id)
    expected = ALLOWED_ASSET_TYPES.get(ext)
    if expected is None:
        raise ContentValidationError(
            f"Файл «{asset_id}»: допустимы только mp3, png, jpg, webp."
        )
    if size <= 0:
        raise ContentValidationError(f"Файл «{asset_id}» пустой.")
    if size > max_bytes:
        raise ContentValidationError(
            f"Файл «{asset_id}» слишком большой: максимум {max_bytes // (1024 * 1024)} МБ."
        )
    actual = sniff_media_type(head)
    if actual != expected:
        raise ContentValidationError(
            f"Файл «{asset_id}» не похож на {_FAMILY_RU[expected]} — содержимое не "
            "совпадает с расширением."
        )
    return expected


def validate_asset_bytes(asset_id: str, data: bytes, *, max_bytes: int = MAX_ASSET_BYTES) -> str:
    """`validate_asset` over an in-memory blob."""
    return validate_asset(asset_id, data[:SNIFF_BYTES], len(data), max_bytes=max_bytes)


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def asset_id_from_filename(filename: str) -> str:
    """A safe, flat asset id from an uploaded file name (no directories, no `..`)."""
    base = re.split(r"[\\/]", filename or "")[-1].strip()
    ext = _extension(base)
    if ext not in ALLOWED_ASSET_TYPES:
        raise ContentValidationError(
            f"Файл «{base or filename}»: допустимы только mp3, png, jpg, webp."
        )
    stem = base[: -len(ext)]
    stem = _UNSAFE_NAME.sub("-", stem).strip("-.")[:80] or "file"
    return f"{stem}{'.jpg' if ext == '.jpeg' else ext}"
