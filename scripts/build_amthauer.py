"""Build the Amthauer TSI (ТСИ, form A) import from PRIVATE sources (Phase 20).

Usage (repo root, or with ``--src`` pointing at the private folder):
    python -m scripts.build_amthauer [--src DIR] [--no-check] [--import-check]

Licensed content never enters git: this script holds NO items, words, keys,
pictures or coordinates. It reads only the private, gitignored folder ``<src>``:

* ``raw_utf8.txt``  — the antiword text of the .doc (instructions, items, key);
* ``curation.json`` — hand-curated, private: test id / title, timing (+ where it
  comes from), per-subtest description / worked examples / the generalization
  answers accepted for 2 points, transcription overrides, the checks done and the
  ``uncertain`` list (source typos, doubtful key entries — the key itself is never
  changed: golden rule 5);
* ``pics/t7_examples.jpg``, ``pics/t7_items.jpg``, ``pics/t8_examples.jpg``,
  ``pics/t8_items.jpg`` — the scans of the figure / cube subtests.

Writes (all private): ``source.json`` (the transcription: subtests, items, key,
study table, checks, uncertain), ``import/test.json`` (level IQ, status DRAFT,
``ui_language`` "ru"), ``import/assets/`` (one file per asset id, the import format
of ``app.content.transfer``) and ``import/PREVIEW.html`` (teacher-only review page
with the key — never served).

Pictures: the figure-assembly boxes and the cubes are found by image analysis
(connected components of the ink: frames = big thin rectangles, cubes = merged
line drawings), the printed item numbers are removed, and each item picture is the
matching reference row ("а"-"д", re-packed so it stays legible on a phone) above
the item's figure. Every count is checked (20 boxes, 2 reference rows of 5, 25
cubes ...) — a scan that does not fit stops the build.

Checks: editor validation, max raw 192, delivery timing, a self-grade (all-correct
= 192 / 192 without review; all-wrong = 0 with every generalization answer routed
to review), the raw IQ report (no norms: V / N / S / G + %), optional
``--import-check`` (a THROW-AWAY sqlite db + assets dir; never the dev db).
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger("scripts.build_amthauer")

DEFAULT_SRC = Path("private/iq/amthauer")
LETTERS = "абвгд"
# Subtest -> (first, last) item number (the form's numbering, 176 items).
RANGES = {
    1: (1, 20),
    2: (21, 40),
    3: (41, 60),
    4: (61, 76),
    5: (77, 96),
    6: (97, 116),
    7: (117, 136),
    8: (137, 156),
    9: (157, 176),
}
# Points per item (the source's scoring rule: 1 per key match, subtest 4: 2 / 1 / 0).
T4_POINTS = 2
MAX_RAW = 192

# This project's own neutral prompts for the picture subtests (not from the form).
T7_PROMPT = "Какая фигура получится из этих частей?"
T8_PROMPT = "Какой это куб?"
T2_PROMPT = "Какое слово лишнее?"


class BuildError(Exception):
    """The private sources do not have the expected shape."""


# --------------------------------------------------------------------------- #
# Transcription: raw antiword text -> structured source.
# --------------------------------------------------------------------------- #
_PART_RE = re.compile(r"^\s*\S+\s+часть\.?\s*$", re.IGNORECASE | re.MULTILINE)
_ITEM_START_RE = re.compile(r"^\s*(\d{1,3})\.\s", re.MULTILINE)
_OPTION_SPLIT_RE = re.compile(r"(?:^|(?<=\s))([абвгд])\)")
_DOTS_RE = re.compile(r"\s*[.…]{2,}\s*")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _clean_stem(text: str) -> str:
    """Collapse spaces; a run of dots / ellipses (the blank) becomes " … "."""
    out = _DOTS_RE.sub(" … ", _collapse(text)).strip()
    return out


def split_parts(raw: str) -> list[str]:
    """The 9 parts ("ПЕРВАЯ ЧАСТЬ" ... "ДЕВЯТАЯ ЧАСТЬ"), each up to the next one; the
    last one runs to the end (answer sheet + key included)."""
    starts = [m.start() for m in _PART_RE.finditer(raw)]
    if len(starts) != 9:
        raise BuildError(f"expected 9 parts, found {len(starts)}")
    return [raw[a:b] for a, b in zip(starts, [*starts[1:], len(raw)], strict=True)]


def _items_block(part: str, *, occurrence: int = 1, end: str = r"Конец|Это всё") -> str:
    """The text after the n-th "СУБТЕСТ" heading up to `end` (or the part's end)."""
    pos = -1
    for _ in range(occurrence):
        pos = part.find("СУБТЕСТ", pos + 1)
        if pos < 0:
            raise BuildError("no СУБТЕСТ heading")
    block = part[pos:]
    stop = re.search(end, block)
    return block[: stop.start()] if stop else block


def _numbered(block: str) -> list[tuple[int, str]]:
    """(number, text) chunks of a block whose items start a line with "N. "."""
    marks = list(_ITEM_START_RE.finditer(block))
    out = []
    for m, nxt in zip(marks, [*marks[1:], None], strict=True):
        out.append((int(m.group(1)), block[m.end() : nxt.start() if nxt else len(block)]))
    return out


def parse_choice_items(part: str, first: int, last: int) -> list[dict[str, Any]]:
    """single_choice items: optional stem, then exactly five options а) .. д)."""
    items = []
    for n, text in _numbered(_items_block(part)):
        if not first <= n <= last:
            continue
        flat = _collapse(text)
        pieces = _OPTION_SPLIT_RE.split(flat)
        stem, rest = pieces[0], pieces[1:]
        letters = rest[0::2]
        if "".join(letters) != LETTERS:
            raise BuildError(f"item {n}: options {letters} are not а..д")
        options = [o.strip(" ,.;") for o in rest[1::2]]
        if any(not o for o in options):
            raise BuildError(f"item {n}: an empty option")
        items.append({"n": n, "stem": _clean_stem(stem), "options": options})
    return items


def parse_pairs(part: str, first: int, last: int) -> list[dict[str, Any]]:
    """Generalization: "N. Word — word ....." -> the pair."""
    items = []
    for n, text in _numbered(_items_block(part)):
        if first <= n <= last:
            items.append({"n": n, "stem": _clean_stem(text).rstrip(" …").strip()})
    return items


def parse_problems(part: str, first: int, last: int) -> list[dict[str, Any]]:
    """Arithmetic word problems: the whole numbered paragraph."""
    return [
        {"n": n, "stem": _collapse(text)}
        for n, text in _numbered(_items_block(part))
        if first <= n <= last
    ]


def parse_series(part: str, first: int, last: int) -> list[dict[str, Any]]:
    """Number series: "N.  a  b  c ... ?" -> the numbers."""
    items = []
    for n, text in _numbered(_items_block(part)):
        if not first <= n <= last:
            continue
        line = text.strip().splitlines()[0]
        if not line.rstrip().endswith("?"):
            raise BuildError(f"series {n}: no '?' at the end")
        items.append({"n": n, "series": [int(x) for x in re.findall(r"\d+", line)]})
    return items


def _table_rows(block: str) -> list[list[str]]:
    return [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in block.splitlines()
        if line.strip().startswith("|")
    ]


def parse_study_table(part: str) -> list[dict[str, Any]]:
    """The memory table: rows "|CATEGORY|w1|..|w5|"; a row whose first cell
    continues the previous one (a wrapped cell) is merged cell by cell (a 1-2
    letter fragment is glued on, a longer one joins with a space)."""
    block = _items_block(part, occurrence=1, end=r"ЖДИТЕ")
    rows: list[list[str]] = []
    for cells in _table_rows(block):
        if len(cells) != 6:
            continue
        is_cont = bool(rows) and sum(1 for c in cells[1:] if c) < 5
        if is_cont:
            prev = rows[-1]
            for i, c in enumerate(cells):
                if c:
                    prev[i] = prev[i] + c if len(c) <= 2 else f"{prev[i]} {c}"
        else:
            rows.append(cells)
    if len(rows) != 5 or any(len(r) != 6 or not all(r) for r in rows):
        raise BuildError(f"study table: expected 5 full rows, got {rows}")
    return [{"category": r[0], "words": r[1:]} for r in rows]


def parse_memory_items(part: str, first: int, last: int) -> list[dict[str, Any]]:
    block = _items_block(part, occurrence=2)
    items = []
    for m in re.finditer(r"(\d{3})\.\s*«(.)»", block):
        n = int(m.group(1))
        if first <= n <= last:
            items.append({"n": n, "letter": m.group(2)})
    return items


def key_from_table(raw: str) -> dict[int, str]:
    """The key table after "Субтесты:": nine columns whose cells wrap across lines.
    Each column is read top to bottom as one text, then split at "N." marks
    (dash fillers dropped)."""
    pos = raw.find("Субтесты:")
    if pos < 0:
        raise BuildError("no key table")
    rows = _table_rows(raw[pos:].split("\n\n\n", 1)[0])
    rows = [r for r in rows if len(r) >= 9 and not r[0].startswith("№")]
    key: dict[int, str] = {}
    for col in range(9):
        text = " ".join(r[col] for r in rows)
        text = re.sub(r"-{3,}", " ", text)
        for m in re.finditer(r"(\d{1,3})\.\s*(.*?)(?=\s*\d{1,3}\.|$)", text):
            key[int(m.group(1))] = _collapse(m.group(2))
    return key


def parse_key(raw: str) -> dict[int, str]:
    """The whole key (items 1..176), every entry non-blank."""
    key = key_from_table(raw)
    missing = [n for n in range(1, 177) if not key.get(n)]
    if missing:
        raise BuildError(f"key: no answer for {missing}")
    return key


def apply_corrections(
    key: dict[int, str], corrections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Owner-approved key corrections from an authoritative published source
    (curation ``corrections``: item, from, to, source). Each must name the value the
    document's key actually prints (``from``) and cite its source; the document key
    is never changed silently. Returns the applied list (for source.json)."""
    applied = []
    for c in corrections:
        n = int(c["item"])
        if not str(c.get("source", "")).strip():
            raise BuildError(f"correction of item {n}: no source cited")
        if key.get(n) != c["from"]:
            raise BuildError(
                f"correction of item {n}: key prints {key.get(n)!r}, not {c['from']!r}"
            )
        key[n] = str(c["to"])
        applied.append(dict(c))
        log.warning("key correction item %d: %s -> %s (%s)", n, c["from"], c["to"], c["source"])
    return applied


def transcribe(src: Path) -> dict[str, Any]:
    """raw_utf8.txt + curation.json -> the source dict (written as source.json)."""
    raw = (src / "raw_utf8.txt").read_text("utf-8").replace("\r\n", "\n")
    cur = json.loads((src / "curation.json").read_text("utf-8"))
    parts = split_parts(raw)
    key = parse_key(raw)
    corrections = apply_corrections(key, cur.get("corrections", []))
    parsers = {
        1: parse_choice_items,
        2: parse_choice_items,
        3: parse_choice_items,
        4: parse_pairs,
        5: parse_problems,
        6: parse_series,
        9: parse_memory_items,
    }
    overrides = cur.get("overrides", {})
    subtests = []
    for n in range(1, 10):
        first, last = RANGES[n]
        if n in (7, 8):
            items = [{"n": q} for q in range(first, last + 1)]
        else:
            items = parsers[n](parts[n - 1], first, last)
        got = [i["n"] for i in items]
        if got != list(range(first, last + 1)):
            raise BuildError(f"subtest {n}: items {got} != {first}..{last}")
        for it in items:
            it.update(overrides.get(str(it["n"]), {}))
            it["answer"] = key[it["n"]]
            if n in (1, 2, 3, 7, 8, 9) and it["answer"] not in LETTERS:
                raise BuildError(f"item {it['n']}: key {it['answer']!r} is not а..д")
            if n in (5, 6) and not it["answer"].isdigit():
                raise BuildError(f"item {it['n']}: key {it['answer']!r} is not a number")
        meta = cur["subtests"][str(n)]
        subtests.append({"n": n, "first": first, "last": last, **meta, "items": items})
    study = parse_study_table(parts[8])
    letters = [w[0].lower() for row in study for w in row["words"]]
    for it in subtests[8]["items"]:
        hits = [i for i, c in enumerate(letters) if c == it["letter"]]
        if len(hits) != 1:
            raise BuildError(f"memory item {it['n']}: letter {it['letter']!r} hits {hits}")
        expect = LETTERS[hits[0] // 5]
        if expect != it["answer"]:
            raise BuildError(f"memory item {it['n']}: table says {expect}, key {it['answer']}")
    source = {
        "meta": {
            "test_id": cur["test_id"],
            "title": cur["title"],
            "source_file": "pdf/A-AMTHAUER.DOC (antiword -> raw_utf8.txt)",
            "items": sum(len(s["items"]) for s in subtests),
            "max_raw": MAX_RAW,
            "scoring": "1 point per key match; subtest 4: 2 = key, 1 = similar but "
            "incomplete meaning, 0 = otherwise. V = T1+T2+T3+T4, N = T5+T6, "
            "S = T7+T8, G = V+N+S+T9. No norm tables in the source.",
        },
        "general": cur["general"],
        "timing": cur["timing"],
        "subtests": subtests,
        "study": study,
        "key": {str(k): v for k, v in sorted(key.items())},
        "corrections": corrections,
        "checks": cur.get("checks", []),
        "uncertain": cur.get("uncertain", []),
    }
    log.info(
        "transcribed %d items in %d subtests; key %d entries; study table %d x %d",
        source["meta"]["items"],
        len(subtests),
        len(key),
        len(study),
        len(study[0]["words"]),
    )
    return source


# --------------------------------------------------------------------------- #
# Pictures: connected components of the ink.
# --------------------------------------------------------------------------- #
class Comp:
    __slots__ = ("x0", "y0", "x1", "y1", "n")

    def __init__(self, x0: int, y0: int, x1: int, y1: int, n: int) -> None:
        self.x0, self.y0, self.x1, self.y1, self.n = x0, y0, x1, y1, n

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def fill(self) -> float:
        return self.n / max(1, self.w * self.h)

    def box(self) -> tuple[int, int, int, int]:
        return self.x0, self.y0, self.x1, self.y1

    def __repr__(self) -> str:
        return f"Comp({self.x0},{self.y0} {self.w}x{self.h} n={self.n})"


def components(mask: np.ndarray) -> list[Comp]:
    """8-connected components of a boolean mask (row runs + union-find)."""
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    prev: list[tuple[int, int, int]] = []
    runs_all: list[tuple[int, int, int, int]] = []
    for y in range(mask.shape[0]):
        d = np.diff(np.concatenate(([0], mask[y].astype(np.int8), [0])))
        cur: list[tuple[int, int, int]] = []
        j = 0
        for s, e in zip(
            np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist(), strict=True
        ):
            idx = len(parent)
            parent.append(idx)
            while j < len(prev) and prev[j][1] < s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] <= e:
                ra, rb = find(prev[k][2]), find(idx)
                if ra != rb:
                    parent[rb] = ra
                k += 1
            cur.append((s, e, idx))
            runs_all.append((y, s, e, idx))
        prev = cur
    boxes: dict[int, list[int]] = {}
    for y, s, e, idx in runs_all:
        r = find(idx)
        b = boxes.get(r)
        if b is None:
            boxes[r] = [s, y, e, y + 1, e - s]
        else:
            b[0], b[1], b[2], b[3] = min(b[0], s), min(b[1], y), max(b[2], e), max(b[3], y + 1)
            b[4] += e - s
    return [Comp(*b) for b in boxes.values()]


def load_gray(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L"), dtype=np.uint8)


def ink_mask(a: np.ndarray) -> np.ndarray:
    return a < 140


def clean_array(a: np.ndarray) -> np.ndarray:
    """Grey paper -> white, ink -> black (linear levels 60..200)."""
    out = (a.astype(np.float32) - 60.0) / 140.0
    return (np.clip(out, 0.0, 1.0) * 255.0).round().astype(np.uint8)


def _inside(c: Comp, box: Sequence[float], margin: float = 0) -> bool:
    x0, y0, x1, y1 = box
    return x0 - margin <= c.cx <= x1 + margin and y0 - margin <= c.cy <= y1 + margin


def _union(cs: Sequence[Comp]) -> tuple[int, int, int, int]:
    return (
        min(c.x0 for c in cs),
        min(c.y0 for c in cs),
        max(c.x1 for c in cs),
        max(c.y1 for c in cs),
    )


def rows_of(cs: Sequence[Comp], gap: float) -> list[list[Comp]]:
    """Cluster by vertical centre (a new row when the centre jumps by > gap), each
    row sorted left to right."""
    rows: list[list[Comp]] = []
    for c in sorted(cs, key=lambda c: c.cy):
        if rows and c.cy - rows[-1][-1].cy <= gap:
            rows[-1].append(c)
        else:
            rows.append([c])
    return [sorted(r, key=lambda c: c.cx) for r in rows]


def is_letter(c: Comp) -> bool:
    return 150 <= c.n <= 3000 and 25 <= c.h <= 120 and c.w <= 120


def frames(comps: Sequence[Comp]) -> list[Comp]:
    """The figure-assembly boxes: big, thin rectangles."""
    return [c for c in comps if 450 <= c.w <= 900 and 280 <= c.h <= 600 and c.fill < 0.15]


def ref_cells(
    comps: Sequence[Comp], band: tuple[float, float], exclude: Sequence[Comp] = ()
) -> list[tuple[int, int, int, int]]:
    """The 5 reference options (shape + its letter) whose centres lie in the
    vertical band, left to right; `exclude` = boxes whose content is not part of it."""
    lo, hi = band
    inband = [
        c for c in comps if lo <= c.cy <= hi and not any(_inside(c, e.box()) for e in exclude)
    ]
    shapes = [c for c in inband if c.n >= 3000 and max(c.w, c.h) >= 120]
    shapes = _merge(shapes, 25)
    letters = [c for c in inband if is_letter(c)]
    if len(shapes) != 5:
        raise BuildError(f"reference row: {len(shapes)} figures, expected 5: {shapes}")
    shapes.sort(key=lambda c: c.cx)
    cells: list[list[Comp]] = [[s] for s in shapes]
    for let in letters:
        k = min(range(5), key=lambda i: abs(shapes[i].cx - let.cx))
        if abs(shapes[k].cx - let.cx) < max(shapes[k].w, 200):
            cells[k].append(let)
    for i, cell in enumerate(cells):
        if len(cell) != 2:
            raise BuildError(f"reference option {LETTERS[i]}: {len(cell) - 1} letters")
    return [_union(cell) for cell in cells]


def _merge(cs: Sequence[Comp], reach: int) -> list[Comp]:
    """Merge components whose boxes (grown by `reach`) overlap, until stable."""
    groups = [Comp(c.x0, c.y0, c.x1, c.y1, c.n) for c in cs]
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                if (
                    a.x0 - reach < b.x1
                    and b.x0 - reach < a.x1
                    and a.y0 - reach < b.y1
                    and b.y0 - reach < a.y1
                ):
                    groups[i] = Comp(
                        min(a.x0, b.x0),
                        min(a.y0, b.y0),
                        max(a.x1, b.x1),
                        max(a.y1, b.y1),
                        a.n + b.n,
                    )
                    del groups[j]
                    changed = True
                    break
            if changed:
                break
    return groups


def cubes(comps: Sequence[Comp]) -> list[Comp]:
    """Cube drawings: big line components merged with their overlapping pieces."""
    big = [c for c in comps if (c.w >= 150 or c.h >= 150) and c.n >= 800]
    return _merge(big, 20)


def crop_clean(a: np.ndarray, box: Sequence[float], pad: int = 12) -> Image.Image:
    x0, y0, x1, y1 = (round(v) for v in box)
    h, w = a.shape
    sub = a[max(0, y0 - pad) : min(h, y1 + pad), max(0, x0 - pad) : min(w, x1 + pad)]
    return Image.fromarray(clean_array(sub), mode="L")


def frame_figure(a: np.ndarray, frame: Comp, inset: int = 14) -> Image.Image:
    """The pieces inside one box, cropped to their ink. Removed: the printed item
    number (small glyphs in the top-left corner), what is left of a tilted frame
    (thin strokes touching the inner border) and the hairlines of the pasted
    paper's cut edges."""
    x0, y0, x1, y1 = frame.x0 + inset, frame.y0 + inset, frame.x1 - inset, frame.y1 - inset
    sub = a[y0:y1, x0:x1].copy()
    h, w = sub.shape
    drop, ink = [], []
    for c in components(ink_mask(sub)):
        touches = c.x0 <= 1 or c.y0 <= 1 or c.x1 >= w - 1 or c.y1 >= h - 1
        label = c.h <= 80 and c.n < 3000 and c.cx < 0.3 * w and c.cy < 0.3 * h
        thin = min(c.w, c.h) <= 16 and (touches or c.n < 2000 or max(c.w, c.h) >= 8 * min(c.w, c.h))
        if label or thin or c.n < 30:
            drop.append(c)
        else:
            ink.append(c)
    if not ink:
        raise BuildError(f"empty box at {frame}")
    for c in drop:
        if c.n >= 30:
            sub[max(0, c.y0 - 2) : c.y1 + 2, max(0, c.x0 - 2) : c.x1 + 2] = 255
    bx0, by0, bx1, by1 = _union(ink)
    pad = 12
    sub = sub[max(0, by0 - pad) : min(h, by1 + pad), max(0, bx0 - pad) : min(w, bx1 + pad)]
    return Image.fromarray(clean_array(sub), mode="L")


def _fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    s = min(max_w / img.width, max_h / img.height)
    return img.resize(
        (max(1, round(img.width * s)), max(1, round(img.height * s))), Image.Resampling.LANCZOS
    )


def compose(
    cells: Sequence[Image.Image], figure: Image.Image, *, per_row: int, width: int = 1200
) -> Image.Image:
    """The reference options re-packed in rows of `per_row` (equal cells), a thin
    rule, then the task figure centred below — one picture, legible on a phone."""
    gap, margin = 16, 16
    cell_w = (width - 2 * margin - (per_row - 1) * gap) // per_row
    cell_h = round(max(c.height * cell_w / c.width for c in cells))
    cell_h = min(cell_h, round(cell_w * 1.35))
    # One scale for every option: their relative sizes stay as printed.
    scale = min(min(cell_w / c.width, cell_h / c.height) for c in cells)
    fitted = [
        c.resize(
            (max(1, round(c.width * scale)), max(1, round(c.height * scale))),
            Image.Resampling.LANCZOS,
        )
        for c in cells
    ]
    n_rows = math.ceil(len(cells) / per_row)
    ref_h = n_rows * cell_h + (n_rows - 1) * gap
    fig = _fit(figure, round(width * 0.62), round(width * 0.42))
    height = margin + ref_h + 2 * gap + 3 + fig.height + margin
    out = Image.new("L", (width, height), 255)
    for i, c in enumerate(fitted):
        r, k = divmod(i, per_row)
        in_row = min(per_row, len(cells) - r * per_row)
        row_w = in_row * cell_w + (in_row - 1) * gap
        x = (width - row_w) // 2 + k * (cell_w + gap) + (cell_w - c.width) // 2
        y = margin + r * (cell_h + gap) + (cell_h - c.height) // 2
        out.paste(c, (x, y))
    rule_y = margin + ref_h + gap
    ImageDraw.Draw(out).rectangle((margin, rule_y, width - margin, rule_y + 2), fill=170)
    out.paste(fig, ((width - fig.width) // 2, rule_y + 3 + gap))
    return out


class Scans:
    """The four scans analysed: reference rows, task figures in reading order."""

    def __init__(self, pics: Path) -> None:
        self.pics = pics

    def t7(
        self, name: str, expect: int
    ) -> tuple[list[list[Image.Image]], list[Image.Image], list[int]]:
        """(reference rows as option cells, task figures, reference row per task)."""
        a = load_gray(self.pics / f"{name}.jpg")
        comps = components(ink_mask(a))
        boxes = frames(comps)
        if len(boxes) != expect:
            raise BuildError(f"{name}: {len(boxes)} boxes, expected {expect}")
        rows = rows_of(boxes, gap=150)
        order = [b for r in rows for b in r]
        # Reference rows sit in the gaps above a box row that are taller than a row.
        refs: list[list[Image.Image]] = []
        ref_of_row: list[int] = []
        top = 0.0
        for r in rows:
            row_top = min(b.y0 for b in r)
            if row_top - top > 0.6 * r[0].h:
                cells = ref_cells(comps, (top, row_top), exclude=boxes)
                refs.append([crop_clean(a, box, pad=10) for box in cells])
            ref_of_row.append(len(refs) - 1)
            top = max(b.y1 for b in r)
        if not refs or ref_of_row[0] != 0:
            raise BuildError(f"{name}: no reference row above the first boxes")
        figures = [frame_figure(a, b) for b in order]
        ref_idx = [ref_of_row[i] for i, r in enumerate(rows) for _ in r]
        log.info(
            "%s: %d boxes in rows %s, %d reference row(s)",
            name,
            len(order),
            [len(r) for r in rows],
            len(refs),
        )
        return refs, figures, ref_idx

    def t8(self, name: str, expect: int) -> tuple[list[Image.Image], list[Image.Image]]:
        """(reference cubes а..д with their letters, task cubes in reading order)."""
        a = load_gray(self.pics / f"{name}.jpg")
        comps = components(ink_mask(a))
        found = cubes(comps)
        if len(found) != 5 + expect:
            raise BuildError(f"{name}: {len(found)} cubes, expected {5 + expect}: {found}")
        rows = rows_of(found, gap=150)
        if len(rows[0]) != 5 or any(len(r) != 5 for r in rows):
            raise BuildError(f"{name}: cube rows {[len(r) for r in rows]}")
        ref, tasks = rows[0], [c for r in rows[1:] for c in r]
        letters = [c for c in comps if is_letter(c)]
        cells = []
        for k, cube in enumerate(ref):
            near = [
                c
                for c in letters
                if cube.x0 - 40 <= c.cx <= cube.x1 + 40
                and (0 < cube.y0 - c.y1 < 200 or 0 < c.y0 - cube.y1 < 200)
            ]
            if len(near) != 1:
                raise BuildError(f"{name}: cube {LETTERS[k]}: {len(near)} letters")
            cells.append(crop_clean(a, _union([cube, near[0]]), pad=10))
        figures = [crop_clean(a, c.box(), pad=12) for c in tasks]
        log.info(
            "%s: 5 reference cubes + %d task cubes in rows %s",
            name,
            len(tasks),
            [len(r) for r in rows],
        )
        return cells, figures


# --------------------------------------------------------------------------- #
# Builder: source -> authoring Test JSON + assets.
# --------------------------------------------------------------------------- #
def overall_seconds_of(sections: Sequence[dict[str, Any]], intro_cap: int) -> int:
    return sum(
        intro_cap
        + int((sec.get("study") or {}).get("study_seconds") or 0)
        + int(sec["time_limit_seconds"])
        for sec in sections
    )


class Builder:
    def __init__(self, src: Path, source: dict[str, Any]) -> None:
        self.src = src
        self.book = source
        self.test_id: str = source["meta"]["test_id"]
        self.sub = {s["n"]: s for s in source["subtests"]}
        self.scans = Scans(src / "pics")
        self.assets: dict[str, Image.Image] = {}
        self.notes: list[str] = [f"{u['where']}: {u['issue']}" for u in source.get("uncertain", [])]

    def sid(self, n: int) -> str:
        return f"{self.test_id}-t{n}"

    def iid(self, q: int) -> str:
        n = next(k for k, (a, b) in RANGES.items() if a <= q <= b)
        return f"{self.test_id}-t{n}-q{q:03d}"

    def asset(self, name: str, img: Image.Image) -> str:
        asset_id = f"{self.test_id}-{name}.png"
        if asset_id in self.assets:
            raise BuildError(f"duplicate asset {asset_id}")
        self.assets[asset_id] = img
        return asset_id

    def section(
        self, n: int, items: list[dict[str, Any]], practice: list[dict[str, Any]]
    ) -> dict[str, Any]:
        st = self.sub[n]
        minutes = int(self.book["timing"]["minutes"][str(n)])
        parts = [str(st["description"]).strip(), str(st.get("how", "")).strip()]
        if n == 1:
            parts.insert(0, str(self.book["general"]).strip())
        sec: dict[str, Any] = {
            "id": self.sid(n),
            "title": f"Субтест {n}. {st['name']}",
            "stimulus": {"kind": "passage_text", "text": ""},
            "items": items,
            "time_limit_seconds": minutes * 60,
            "intro": {"text": "\n\n".join(p for p in parts if p), "practice": practice},
        }
        study = self.book["timing"].get("study_seconds", {}).get(str(n))
        if study:
            sec["study"] = {"text": self.study_text(), "study_seconds": int(study)}
        return sec

    @staticmethod
    def _options(words: Sequence[str]) -> list[dict[str, str]]:
        return [
            {"kind": "text", "key": k, "text": f"{k}) {w}"}
            for k, w in zip(LETTERS, words, strict=True)
        ]

    def _choice_practice(
        self, n: int, images: list[list[str]] | None = None
    ) -> list[dict[str, Any]]:
        out = []
        for k, ex in enumerate(self.sub[n]["examples"]):
            words = ex.get("options")
            if words is None and n == 9:
                words = [row["category"].capitalize() for row in self.book["study"]]
            out.append(
                {
                    "kind": "choice",
                    "prompt": ex.get("prompt", ""),
                    "images": images[k] if images else [],
                    "options": [
                        {"key": key, "text": w}
                        for key, w in zip(LETTERS, words or [""] * 5, strict=True)
                    ],
                    "solution": ex["solution"],
                    "explanation": ex.get("explanation", ""),
                }
            )
        return out

    def _text_practice(self, n: int) -> list[dict[str, Any]]:
        return [
            {
                "kind": "text",
                "prompt": ex["prompt"],
                "solution": ex["solution"],
                "explanation": ex.get("explanation", ""),
            }
            for ex in self.sub[n]["examples"]
        ]

    def choice_words(self, n: int) -> dict[str, Any]:
        items = []
        for it in self.sub[n]["items"]:
            items.append(
                {
                    "item_type": "single_choice",
                    "id": self.iid(it["n"]),
                    "prompt": it["stem"] or T2_PROMPT,
                    "options": self._options(it["options"]),
                    "correct": it["answer"],
                    # The form's letters are part of the task: authored order always.
                    "shuffle_options": False,
                }
            )
        return self.section(n, items, self._choice_practice(n))

    def t4(self) -> dict[str, Any]:
        st = self.sub[4]
        items = []
        for it in st["items"]:
            q = str(it["n"])
            accepted = list(st.get("accepted", {}).get(q) or [it["answer"]])
            partial = list(st.get("partial", {}).get(q) or [])
            demoted = st.get("key_in_partial", {}).get(q)
            if it["answer"] in partial and demoted:
                # An owner decision (a scoring manual rates the printed key word as
                # an incomplete answer): kept visible in the preview notes.
                self.notes.append(f"T4 №{q}: printed key {it['answer']!r} scores 1 — {demoted}")
            elif it["answer"] not in accepted:
                raise BuildError(f"T4 {q}: the key {it['answer']!r} is not among the accepted")
            items.append(
                {
                    "item_type": "gap_fill",
                    "id": self.iid(it["n"]),
                    "prompt": it["stem"],
                    "accepted": accepted,
                    "partial": partial,
                    "review_unmatched": True,
                    "spelling": "tolerant",
                    "points": T4_POINTS,
                }
            )
        return self.section(4, items, self._text_practice(4))

    def numeric(self, n: int) -> dict[str, Any]:
        items = []
        for it in self.sub[n]["items"]:
            prompt = it["stem"] if n == 5 else "  ".join(map(str, it["series"])) + "  ?"
            items.append(
                {
                    "item_type": "gap_fill",
                    "id": self.iid(it["n"]),
                    "prompt": prompt,
                    "accepted": [it["answer"]],
                    "spelling": "exact",
                    "max_words": 1,
                    "numeric": True,
                }
            )
        return self.section(n, items, self._text_practice(n))

    def t7(self) -> dict[str, Any]:
        refs, figs, ref_idx = self.scans.t7("t7_items", 20)
        if len(refs) != 2:
            raise BuildError(f"T7: {len(refs)} reference rows, expected 2")
        ex_refs, ex_figs, _ = self.scans.t7("t7_examples", len(self.sub[7]["examples"]))
        st = self.sub[7]
        items = []
        for it, fig, r in zip(st["items"], figs, ref_idx, strict=True):
            img = compose(refs[r], fig, per_row=3)
            items.append(self._letter_item(it, T7_PROMPT, self.asset(f"t7-q{it['n']}", img)))
        prac = [
            [self.asset(f"t7-example-{k}", compose(ex_refs[0], fig, per_row=3))]
            for k, fig in enumerate(ex_figs, 1)
        ]
        self.notes.append(
            f"T7: items {st['first']}–{st['first'] + ref_idx.count(0) - 1} use reference row 1, "
            f"the rest row 2 — check the pictures in this preview."
        )
        return self.section(7, items, self._letter_practice(7, prac, T7_PROMPT))

    def t8(self) -> dict[str, Any]:
        ref, figs = self.scans.t8("t8_items", 20)
        ex_ref, ex_figs = self.scans.t8("t8_examples", len(self.sub[8]["examples"]))
        items = [
            self._letter_item(
                it, T8_PROMPT, self.asset(f"t8-q{it['n']}", compose(ref, fig, per_row=3))
            )
            for it, fig in zip(self.sub[8]["items"], figs, strict=True)
        ]
        prac = [
            [self.asset(f"t8-example-{k}", compose(ex_ref, fig, per_row=3))]
            for k, fig in enumerate(ex_figs, 1)
        ]
        return self.section(8, items, self._letter_practice(8, prac, T8_PROMPT))

    def _letter_item(self, it: dict[str, Any], prompt: str, image: str) -> dict[str, Any]:
        return {
            "item_type": "single_choice",
            "id": self.iid(it["n"]),
            "prompt": prompt,
            "image": image,
            "options": [{"kind": "text", "key": k, "text": k} for k in LETTERS],
            "correct": it["answer"],
            "shuffle_options": False,  # the letters name the reference pictures
        }

    def _letter_practice(
        self, n: int, images: list[list[str]], prompt: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "kind": "choice",
                "prompt": prompt,
                "images": imgs,
                "options": [{"key": k, "text": ""} for k in LETTERS],
                "solution": ex["solution"],
                "explanation": ex.get("explanation", ""),
            }
            for ex, imgs in zip(self.sub[n]["examples"], images, strict=True)
        ]

    def study_text(self) -> str:
        return "\n\n".join(
            f"{row['category'].upper()}\n{' · '.join(row['words'])}" for row in self.book["study"]
        )

    def t9(self) -> dict[str, Any]:
        cats = [row["category"].capitalize() for row in self.book["study"]]
        items = [
            {
                "item_type": "single_choice",
                "id": self.iid(it["n"]),
                "prompt": f"Слово, начинающееся с буквы «{it['letter']}», означает:",
                "options": self._options(cats),
                "correct": it["answer"],
                "shuffle_options": False,  # the letters name the categories
            }
            for it in self.sub[9]["items"]
        ]
        return self.section(9, items, self._choice_practice(9))

    def norm_scoring(self) -> dict[str, Any]:
        s = [self.sid(n) for n in range(1, 10)]
        timing = self.book["timing"]["source"]
        return {
            "composites": [
                {
                    "key": "V",
                    "label": "Вербальный (V = T1+T2+T3+T4)",
                    "section_ids": s[0:4],
                    "basis": "raw",
                },
                {
                    "key": "N",
                    "label": "Числовой (N = T5+T6)",
                    "section_ids": s[4:6],
                    "basis": "raw",
                },
                {
                    "key": "S",
                    "label": "Пространственный (S = T7+T8)",
                    "section_ids": s[6:8],
                    "basis": "raw",
                },
                {
                    "key": "G",
                    "label": "Общий интеллект (G = V+N+S+T9)",
                    "section_ids": s,
                    "basis": "raw",
                },
            ],
            "note": (
                "Тест структуры интеллекта Р. Амтхауэра (форма А). Нормы для перевода в IQ "
                "не загружены — показаны сырые баллы и % от максимума. Баллы: 1 за совпадение "
                "с ключом, субтест 4 — 2 / 1 / 0 (ответы вне ключа оценивает учитель); "
                f"максимум {MAX_RAW}. {timing}"
            ),
        }

    def build(self) -> dict[str, Any]:
        sections = [
            self.choice_words(1),
            self.choice_words(2),
            self.choice_words(3),
            self.t4(),
            self.numeric(5),
            self.numeric(6),
            self.t7(),
            self.t8(),
            self.t9(),
        ]
        total = overall_seconds_of(sections, settings.section_intro_max_seconds)
        return {
            "id": self.test_id,
            "title": self.book["meta"]["title"],
            "level": "IQ",
            "status": "draft",
            "ui_language": "ru",
            "duration_minutes": math.ceil(total / 60),
            "sections": sections,
            "norm_scoring": self.norm_scoring(),
        }


# --------------------------------------------------------------------------- #
# Checks.
# --------------------------------------------------------------------------- #
def check_counts(test: Any) -> list[str]:
    from app.grading.service import item_max_points

    problems = []
    sizes = [len(s.items) for s in test.sections]
    expect = [b - a + 1 for a, b in RANGES.values()]
    if sizes != expect:
        problems.append(f"items per subtest {sizes} != {expect}")
    top = sum(item_max_points(i, test.level) for s in test.sections for i in s.items)
    log.info("items per subtest %s = %d items, max raw %g", sizes, sum(sizes), top)
    if top != MAX_RAW:
        problems.append(f"max raw {top} != {MAX_RAW}")
    return problems


def self_grade(test: Any) -> list[str]:
    """All-correct = full marks without review; all-wrong = 0 with every open
    generalization answer in review; the raw report adds up."""
    from app.grading.service import GradingService
    from contracts import GapFillItem, SingleChoiceItem
    from scripts.build_iq_book import _Attempts, _Content

    problems: list[str] = []
    right: dict[str, str] = {}
    wrong: dict[str, str] = {}
    for sec in test.sections:
        for it in sec.items:
            if isinstance(it, SingleChoiceItem):
                right[it.id] = it.correct
                wrong[it.id] = next(o.key for o in it.options if o.key != it.correct)
            elif isinstance(it, GapFillItem):
                right[it.id] = it.accepted[-1].upper() + "."  # case + punctuation tolerated
                wrong[it.id] = "zzz" if not it.numeric else "0"
    birth = date(2008, 5, 1)
    svc = GradingService(_Content(test), _Attempts(test.id, right, birth))  # type: ignore[arg-type]
    res = svc.grade("selfcheck")
    log.info(
        "self-grade all-correct: %g / %g, needs_review=%s",
        res.score,
        res.max_score,
        res.needs_review,
    )
    if res.score != MAX_RAW or res.max_score != MAX_RAW or res.needs_review:
        problems.append(f"all-correct {res.score}/{res.max_score} review={res.needs_review}")
    report = svc.iq_report(res)
    if report is None or report.norms_loaded:
        problems.append("no raw report")
    else:
        comps = {c.key: (c.raw_sum, c.raw_max, c.percent) for c in report.composites}
        log.info("raw report (all correct): %s notes=%s", comps, report.notes)
        if comps != {
            "V": (92, 92, 100.0),
            "N": (40, 40, 100.0),
            "S": (40, 40, 100.0),
            "G": (192, 192, 100.0),
        }:
            problems.append(f"raw composites {comps}")
        if report.overall is not None or "no_norm_tables" not in report.notes:
            problems.append("a raw report must have no IQ and the no_norm_tables note")
    svc_w = GradingService(_Content(test), _Attempts(test.id, wrong, birth))  # type: ignore[arg-type]
    res_w = svc_w.grade("selfcheck")
    review = sum(g.needs_review for g in res_w.items)
    log.info(
        "self-grade all-wrong: %g / %g, %d answers in review", res_w.score, res_w.max_score, review
    )
    if res_w.score != 0 or review != len(test.sections[3].items):
        problems.append(f"all-wrong scored {res_w.score}, {review} in review")
    return problems


# --------------------------------------------------------------------------- #
# Preview page (teacher-only, local).
# --------------------------------------------------------------------------- #
_CSS = (
    "body{font:15px/1.45 system-ui,sans-serif;margin:16px auto;max-width:1000px;"
    "padding:0 16px;color:#222;background:#fff}h2{border-bottom:2px solid #333;margin-top:36px}"
    ".item{border:1px solid #ccc;border-radius:6px;padding:8px 10px;margin:8px 0}"
    ".key{background:#e5f6e5;padding:1px 6px;border-radius:4px;font-weight:600}"
    ".intro{white-space:pre-wrap;background:#f6f6f6;padding:8px;border-radius:6px}"
    ".notes li{color:#a40}.pic{max-width:min(640px,100%);border:1px solid #ddd}"
    "pre{white-space:pre-wrap;margin:4px 0}.small{color:#666;font-size:12px}"
)


def _img(asset_id: str) -> str:
    a = html.escape(asset_id)
    return f'<div><img class="pic" src="assets/{a}" alt="{a}"></div>'


def render_preview(test: dict[str, Any], notes: list[str], problems: list[str]) -> str:
    esc = html.escape
    out = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{esc(test['title'])} — preview</title><style>{_CSS}</style></head><body>",
        f"<h1>{esc(test['title'])}</h1>",
        f"<p>id <code>{esc(test['id'])}</code> · level {esc(test['level'])} · status "
        f"{esc(test['status'])} · ui {esc(test['ui_language'])} · {test['duration_minutes']} "
        "min overall (every intro cap + study + answering). <b>Teacher-only (contains the "
        "key).</b></p>",
    ]
    if problems:
        out.append(
            "<h3>Check problems</h3><ul class='notes'>"
            + "".join(f"<li>{esc(p)}</li>" for p in problems)
            + "</ul>"
        )
    out.append(
        "<h3>Needs a human look</h3><ul class='notes'>"
        + "".join(f"<li>{esc(n)}</li>" for n in notes)
        + "</ul>"
    )
    for sec in test["sections"]:
        study = sec.get("study")
        extra = f" (study {study['study_seconds']} s)" if study else ""
        out.append(f"<h2>{esc(sec['title'])} — {sec['time_limit_seconds']} s{extra}</h2>")
        intro = sec.get("intro") or {}
        out.append(f"<div class='intro'>{esc(intro.get('text', ''))}</div>")
        for k, pr in enumerate(intro.get("practice", []), 1):
            opts = " ".join(
                f"({o['key']}) {o.get('text', '')}".strip() for o in pr.get("options", [])
            )
            out.append(
                f"<div class='item'><b>Пример {k}</b> {esc(pr.get('prompt', ''))}"
                + "".join(_img(a) for a in pr.get("images", []))
                + (f"<div>{esc(opts)}</div>" if opts else "")
                + f"<div><span class='key'>{esc(pr.get('solution', ''))}</span> "
                f"{esc(pr.get('explanation', ''))}</div></div>"
            )
        if study:
            out.append(
                "<p><b>Study (shown, then hidden):</b></p>"
                f"<pre>{esc(study.get('text') or '')}</pre>"
            )
        for it in sec["items"]:
            out.append(_render_item(it))
    nsc = test["norm_scoring"]
    out.append(
        "<h2>Scoring</h2><p>"
        + esc(nsc["note"])
        + "</p><p>"
        + "; ".join(f"{esc(c['key'])} = {esc(c['label'])}" for c in nsc["composites"])
        + "</p></body></html>"
    )
    return "\n".join(out)


def _render_item(it: dict[str, Any]) -> str:
    esc = html.escape
    pts = f" · {it['points']} pts" if it.get("points") else ""
    body = [
        f"<div class='item'><span class='small'>{esc(it['id'])} · {esc(it['item_type'])}{pts}"
        f"</span><pre>{esc(it.get('prompt', ''))}</pre>"
    ]
    if it.get("image"):
        body.append(_img(it["image"]))
    if it["item_type"] == "single_choice":
        body.append(
            " ".join(
                f"<span class='{'key' if o['key'] == it['correct'] else ''}'>"
                f"{esc(o['text'])}</span>"
                for o in it["options"]
            )
        )
    else:
        body.append(
            "Key: " + " / ".join(f"<span class='key'>{esc(a)}</span>" for a in it["accepted"])
        )
        if it.get("partial"):
            body.append(" · partial: " + " / ".join(map(esc, it["partial"])))
        if it.get("review_unmatched"):
            body.append(" <span class='small'>(other answers -> teacher review)</span>")
    body.append("</div>")
    return "".join(body)


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the private Amthauer TSI import.")
    parser.add_argument("--src", default=str(DEFAULT_SRC), help="Private source folder.")
    parser.add_argument("--no-check", action="store_true", help="Skip the self-grade.")
    parser.add_argument(
        "--import-check",
        action="store_true",
        help="Also import into a throw-away sqlite db and export it back.",
    )
    args = parser.parse_args(argv)
    # Never touch the developer's database (app modules build an engine at import).
    os.environ["DATABASE_URL"] = "sqlite://"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    configure_logging("INFO")
    src = Path(args.src)
    if "private" not in src.resolve().parts:
        log.error("refusing to build outside a private/ folder: %s", src)
        return 2

    from scripts.build_iq_book import check_timing, import_check, validate

    try:
        source = transcribe(src)
        (src / "source.json").write_text(
            json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log.info("wrote %s", src / "source.json")
        b = Builder(src, source)
        test_json = b.build()
    except BuildError as exc:
        log.error("build stopped: %s", exc)
        return 1
    test = validate(test_json)
    problems = check_counts(test) + check_timing(test)

    out = src / "import"
    assets_dir = out / "assets"
    if assets_dir.exists():
        shutil.rmtree(assets_dir)
    assets_dir.mkdir(parents=True)
    for asset_id, img in b.assets.items():
        img.save(assets_dir / asset_id, optimize=True)
    (out / "test.json").write_text(
        json.dumps(test.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    log.info("wrote %s + %d assets", out / "test.json", len(b.assets))
    if not args.no_check:
        problems += self_grade(test)
    if args.import_check:
        problems += import_check(out)
    (out / "PREVIEW.html").write_text(
        render_preview(test.model_dump(mode="json"), b.notes, problems), encoding="utf-8"
    )
    log.info("wrote %s", out / "PREVIEW.html")
    for note in b.notes:
        log.warning("human check: %s", note)
    for p in problems:
        log.error("problem: %s", p)
    log.info("build %s: %d problem(s)", "OK" if not problems else "FAILED", len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
