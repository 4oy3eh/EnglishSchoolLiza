"""Build the IQ book import (authoring Test JSON + assets) from PRIVATE sources.

Usage (repo root):
    python -m scripts.build_iq_book [--src DIR] [--no-check] [--import-check]

Reads ONLY private, gitignored material (Phase 17.7 — licensed content never enters
git; this script holds no items, answers, tables or coordinates of the book):

* ``<src>/source.json``  — the transcribed book (subtests, items, keys, norm tables);
* ``<src>/layout.json``  — page geometry (crop boxes, deskew angles) and the drawing
  targets authored with ``scripts/iq_targets_tool.py`` (zones for "draw what is
  missing", approximate dividing lines for "divide the square");
* ``<src>/pages/pNN.png`` — the 200-dpi page scans.

Writes into ``<src>/import/``: ``test.json`` (level IQ, status draft), ``assets/``
(files named ``<asset_id>``, the import format of ``app.content.transfer``) and
``PREVIEW.html`` (a local review page: every item with its picture, zone / line
overlays and the key — teacher-only, never served).

Steps: crop + clean the pictures -> refine the "divide the square" reference lines
against the printed answer drawings (darkness centroid across each line, least-squares
fit, snap to the square's border) -> assemble the Test -> ``editor.validate_test_data``
-> per-section max raw == top of the section's raw->scaled table -> a self-grade
(all-correct responses, synthetic strokes inside every zone / along every line must
score full marks; strokes outside / a wrong figure / marking every row must not) ->
optional ``--import-check``: import into a THROW-AWAY sqlite db + assets dir via
``python -m app.content.transfer`` and export it back (never the dev db).
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger("scripts.build_iq_book")

DEFAULT_SRC = Path("private/iq/cambridge_selfscoring")
Box = Sequence[float]
XY = tuple[float, float]

# Item prompts are this project's own neutral wording; the book's instructions travel
# verbatim (from source.json) on each subtest's intro screen.
DRAW_MISSING_PROMPT = "Draw the missing part."
DIVIDE_PROMPT = "Divide the empty square into the pieces shown."


# --------------------------------------------------------------------------- #
# Pictures.
# --------------------------------------------------------------------------- #
class Pages:
    """Page scans (grayscale), deskewed per layout, cached."""

    def __init__(self, src: Path, deskew: dict[str, float]) -> None:
        self.dir = src / "pages"
        self.deskew = deskew
        self._cache: dict[str, Image.Image] = {}

    def get(self, page: str) -> Image.Image:
        if page not in self._cache:
            img = Image.open(self.dir / f"{page}.png").convert("L")
            angle = self.deskew.get(page)
            if angle:
                img = img.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=200)
            self._cache[page] = img
        return self._cache[page]

    def array(self, page: str) -> np.ndarray:
        return np.asarray(self.get(page), dtype=np.float32)


def _paper(a: np.ndarray, block: int = 40) -> np.ndarray:
    """Local paper brightness: the 90th percentile per block (ink-only blocks take
    the page median), bilinearly upsampled — flattens the scan's grey shading."""
    h, w = a.shape
    by, bx = max(1, round(h / block)), max(1, round(w / block))
    ys = np.linspace(0, h, by + 1).astype(int)
    xs = np.linspace(0, w, bx + 1).astype(int)
    grid = np.array(
        [
            [float(np.percentile(a[ys[i] : ys[i + 1], xs[j] : xs[j + 1]], 90)) for j in range(bx)]
            for i in range(by)
        ]
    )
    median = float(np.median(grid))
    grid = np.where(grid < median - 35.0, median, grid)
    small = Image.fromarray(grid.astype(np.float32), mode="F")
    return np.asarray(small.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32)


def clean(img: Image.Image) -> Image.Image:
    """Grey scan -> white paper, black ink: divide by the local paper level, then
    linear levels (paper -> white, the darkest ink -> black)."""
    a = np.asarray(img, dtype=np.float32)
    rel = a / np.maximum(_paper(a), 1.0)  # paper ~ 1.0
    white = 0.86
    black = min(float(np.percentile(rel, 0.5)), 0.45)
    out = np.clip((rel - black) / max(0.05, white - black), 0.0, 1.0)
    return Image.fromarray((out * 255.0).round().astype(np.uint8), mode="L")


def crop(pages: Pages, page: str, box: Box, pad: float = 0.0) -> Image.Image:
    x0, y0, x1, y1 = box
    return clean(
        pages.get(page).crop((round(x0 - pad), round(y0 - pad), round(x1 + pad), round(y1 + pad)))
    )


def despeckle(a: np.ndarray, min_area: int = 14) -> np.ndarray:
    """Whiten ink components (8-connected, < 128) smaller than `min_area` px."""
    ink = a < 128
    h, w = ink.shape
    seen = np.zeros_like(ink)
    out = a.copy()
    for y0 in range(h):
        for x0 in range(w):
            if not ink[y0, x0] or seen[y0, x0]:
                continue
            stack, comp = [(y0, x0)], []
            seen[y0, x0] = True
            while stack:
                y, x = stack.pop()
                comp.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < h and 0 <= nx < w and ink[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            if len(comp) < min_area:
                ys, xs = zip(*comp, strict=True)
                out[list(ys), list(xs)] = 255.0
    return out


def tighten(
    img: Image.Image, pad: int = 6, max_frag_w: int = 18, max_frag_ink: float = 0.15
) -> Image.Image:
    """Crop a picture to its ink, dropping thin edge fragments (a neighbouring "(b)"
    label caught by the recorded box): an outermost column cluster at most
    `max_frag_w` px wide, at least 1.4x as tall as wide (a bracket, not a bar or a
    dot) and holding at most `max_frag_ink` of the ink; column gaps < 4 px never
    separate clusters."""
    a = despeckle(np.asarray(img, dtype=np.float32))
    ink = a < 128
    total = float(ink.sum())
    if total == 0:
        return img
    clusters = [list(c) for c in _runs((ink.sum(axis=0) > 0).tolist())]
    merged: list[list[int]] = []
    for c in clusters:  # gaps < 4 px do not separate clusters
        if merged and c[0] - merged[-1][1] < 4:
            merged[-1][1] = c[1]
        else:
            merged.append(c)

    def frag(c: list[int]) -> bool:
        width = c[1] - c[0] + 1
        rows = np.nonzero(ink[:, c[0] : c[1] + 1].any(axis=1))[0]
        height = int(rows[-1] - rows[0] + 1) if rows.size else 0
        return (
            width <= max_frag_w
            and height >= 1.4 * width  # a bracket, not a bar / dot
            and float(ink[:, c[0] : c[1] + 1].sum()) <= max_frag_ink * total
        )

    while len(merged) > 1 and frag(merged[0]):
        merged.pop(0)
    while len(merged) > 1 and frag(merged[-1]):
        merged.pop()
    x0, x1 = merged[0][0], merged[-1][1]
    rows = np.nonzero(ink[:, x0 : x1 + 1].any(axis=1))[0]
    y0, y1 = int(rows[0]), int(rows[-1])
    out = np.full((y1 - y0 + 1 + 2 * pad, x1 - x0 + 1 + 2 * pad), 255, dtype=np.uint8)
    out[pad:-pad, pad:-pad] = a[y0 : y1 + 1, x0 : x1 + 1].astype(np.uint8)
    return Image.fromarray(out, mode="L")


def _runs(mask: Iterable[bool]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start: int | None = None
    i = -1
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, i))
    return out


def _longest_dark_run(col: np.ndarray) -> tuple[int, int, int]:
    """(length, start, end) of the longest dark run in a 1-D boolean array."""
    best = (0, 0, 0)
    for s, e in _runs(col.tolist()):
        if e - s + 1 > best[0]:
            best = (e - s + 1, s, e)
    return best


def find_blank_square(img: Image.Image) -> tuple[float, float, float, float]:
    """The empty answer square = the rightmost outlined square of an item crop.

    Returns (left, top, right, bottom) border CENTRE lines in crop pixels: the two
    rightmost long vertical strokes whose runs span the same rows and whose spacing
    equals that span."""
    dark = np.asarray(img, dtype=np.float32) < 128
    h, w = dark.shape
    cols = [_longest_dark_run(dark[:, x]) for x in range(w)]
    min_len = int(h * 0.3)
    xs = [x for x in range(w) if cols[x][0] >= min_len]
    groups = _runs([x in set(xs) for x in range(w)])
    if len(groups) < 2:
        raise ValueError("no square found")

    def span(g: tuple[int, int]) -> tuple[float, float, float]:
        mid = max(range(g[0], g[1] + 1), key=lambda x: cols[x][0])
        _, s, e = cols[mid]
        return (g[0] + g[1]) / 2, s, e

    right = span(groups[-1])
    for g in reversed(groups[:-1]):
        left = span(g)
        side = right[0] - left[0]
        if abs(left[1] - right[1]) <= 6 and abs(left[2] - right[2]) <= 6:
            height = (right[2] + left[2]) / 2 - (right[1] + left[1]) / 2
            if abs(side - height) <= 0.2 * height:
                top = (left[1] + right[1]) / 2 + 2
                bottom = (left[2] + right[2]) / 2 - 2
                return left[0], top, right[0], bottom
    raise ValueError("no square found")


def inner_box(arr: np.ndarray, box: Box, margin: int = 4) -> tuple[int, int, int, int]:
    """The inside of a printed frame around `box` (page px): just within its first /
    last strong vertical and horizontal lines (the recorded box may be a few px off)."""
    x0, y0, x1, y1 = (round(v) for v in box)
    X0, Y0, X1, Y1 = x0 - 15, y0 - 15, x1 + 15, y1 + 15
    sub = arr[Y0:Y1, X0:X1] < 120
    xs = _runs((sub.mean(axis=0) > 0.75).tolist())
    ys = _runs((sub.mean(axis=1) > 0.75).tolist())
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError(f"frame not found near {box}")
    return (
        X0 + xs[0][1] + margin,
        Y0 + ys[0][1] + margin,
        X0 + xs[-1][0] - margin,
        Y0 + ys[-1][0] - margin,
    )


def square_of_page_box(arr: np.ndarray, box: Box) -> tuple[float, float, float, float]:
    """Border centre lines of a printed square around `box` (page px): first / last
    strong vertical and horizontal line runs of the thresholded neighbourhood."""
    x0, y0, x1, y1 = (round(v) for v in box)
    X0, Y0, X1, Y1 = x0 - 12, y0 - 12, x1 + 12, y1 + 12
    sub = arr[Y0:Y1, X0:X1] < 120
    xs = _runs((sub.mean(axis=0) > 0.7).tolist())
    ys = _runs((sub.mean(axis=1) > 0.7).tolist())
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError(f"answer square not found near {box}")
    return (
        X0 + (xs[0][0] + xs[0][1]) / 2,
        Y0 + (ys[0][0] + ys[0][1]) / 2,
        X0 + (xs[-1][0] + xs[-1][1]) / 2,
        Y0 + (ys[-1][0] + ys[-1][1]) / 2,
    )


# --------------------------------------------------------------------------- #
# "Divide the square": refine approximate lines against the printed answer.
# --------------------------------------------------------------------------- #
def _sample(arr: np.ndarray, x: float, y: float) -> float:
    xi, yi = int(round(x)), int(round(y))
    if 0 <= yi < arr.shape[0] and 0 <= xi < arr.shape[1]:
        return float(arr[yi, xi])
    return 255.0


def _intersect_border(p: XY, d: XY, near: XY) -> XY:
    """Where the line p + t*d crosses the square border closest to `near`."""
    best: XY = near
    best_dist = math.inf
    for axis in (0, 1):
        if abs(d[axis]) < 1e-9:
            continue
        for edge in (0.0, 1.0):
            t = (edge - p[axis]) / d[axis]
            q = (p[0] + t * d[0], p[1] + t * d[1])
            if -0.02 <= q[1 - axis] <= 1.02:
                dist = math.hypot(q[0] - near[0], q[1] - near[1])
                if dist < best_dist:
                    best, best_dist = q, dist
    return best


def refine_segments(
    arr: np.ndarray, sq: tuple[float, float, float, float], approx: list[list[list[float]]]
) -> tuple[list[tuple[XY, XY]], list[str]]:
    """Fit each approximate square-local segment to the ink of the printed answer.

    Per segment: 21 probes along its middle 80 %; at each, the darkness-weighted
    centroid across the line (±7 px); a total-least-squares line through the
    centroids; endpoints = the approximate ones projected onto it (an endpoint on
    the border snaps to the border crossing). Notes list weak fits."""
    sx, sy, sr, sb = sq
    sw, sh = sr - sx, sb - sy
    notes: list[str] = []
    out: list[tuple[XY, XY]] = []
    for a_raw, b_raw in approx:
        a = (float(a_raw[0]), float(a_raw[1]))
        bb = (float(b_raw[0]), float(b_raw[1]))
        ax, ay = sx + a[0] * sw, sy + a[1] * sh
        bx, by = sx + bb[0] * sw, sy + bb[1] * sh
        length = math.hypot(bx - ax, by - ay)
        ux, uy = (bx - ax) / length, (by - ay) / length
        nx, ny = -uy, ux
        pts: list[XY] = []
        for k in range(21):
            f = 0.1 + 0.8 * k / 20
            cx, cy = ax + f * (bx - ax), ay + f * (by - ay)
            offs = np.arange(-7, 8)
            vals = np.array([_sample(arr, cx + o * nx, cy + o * ny) for o in offs])
            weight = np.clip(150.0 - vals, 0.0, None)
            if weight.sum() < 60.0:  # no ink here (broken / faint line)
                continue
            o = float((offs * weight).sum() / weight.sum())
            pts.append(((cx + o * nx - sx) / sw, (cy + o * ny - sy) / sh))
        if len(pts) < 5:
            notes.append(f"segment {a}->{bb}: too little ink ({len(pts)} probes), kept as authored")
            out.append((a, bb))
            continue
        p = np.array(pts)
        mean = p.mean(axis=0)
        _, _, vt = np.linalg.svd(p - mean)
        d = (float(vt[0][0]), float(vt[0][1]))
        resid = float(np.abs((p - mean) @ np.array([-d[1], d[0]])).max())
        if resid > 0.03:
            notes.append(f"segment {a}->{bb}: wobbly fit (max residual {resid:.3f})")
        m = (float(mean[0]), float(mean[1]))

        def project(q: XY, m: XY = m, d: XY = d) -> XY:
            s = (q[0] - m[0]) * d[0] + (q[1] - m[1]) * d[1]
            return (m[0] + s * d[0], m[1] + s * d[1])

        ends: list[XY] = []
        for q in (a, bb):
            on_border = min(q[0], 1 - q[0], q[1], 1 - q[1]) <= 0.03
            e = _intersect_border(m, d, q) if on_border else project(q)
            ends.append((round(min(1.0, max(0.0, e[0])), 3), round(min(1.0, max(0.0, e[1])), 3)))
        out.append((ends[0], ends[1]))
    return out, notes


# --------------------------------------------------------------------------- #
# Text helpers.
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"^\[[^\]]*\]\s*", re.MULTILINE)


def _merged_runs(mask: Sequence[bool], max_gap: int) -> list[tuple[int, int]]:
    """Runs of True, joining runs separated by fewer than `max_gap` False."""
    out: list[tuple[int, int]] = []
    for r0, r1 in _runs(mask):
        if out and r0 - out[-1][1] - 1 < max_gap:
            out[-1] = (out[-1][0], r1)
        else:
            out.append((r0, r1))
    return out


def grid_cells(img: Image.Image, rows: int, cols: int, pad: int = 4) -> list[Image.Image]:
    """Cut a sheet of `rows` x `cols` printed boxes (the T1 study sheet: symbol over
    number, each framed) into one picture per box, reading order. Rows / columns are
    found from the ink profile (white gaps between the boxes); scan speckle is
    ignored. Raises when the sheet does not show exactly rows x cols boxes."""
    a = despeckle(np.asarray(img, dtype=np.float32), min_area=30)
    ink = a < 128
    h, w = ink.shape
    row_runs = _merged_runs((ink.sum(axis=1) > w * 0.02).tolist(), max_gap=6)
    row_runs = sorted(sorted(row_runs, key=lambda r: r[1] - r[0], reverse=True)[:rows])
    if len(row_runs) != rows:
        raise ValueError(f"grid: {len(row_runs)} rows found, want {rows}")
    out: list[Image.Image] = []
    for y0, y1 in row_runs:
        band = ink[y0 : y1 + 1]
        col_runs = [
            r
            for r in _merged_runs((band.sum(axis=0) > (y1 - y0 + 1) * 0.05).tolist(), max_gap=4)
            if r[1] - r[0] > 15
        ]
        if len(col_runs) != cols:
            raise ValueError(f"grid: {len(col_runs)} boxes in a row, want {cols}")
        for x0, x1 in col_runs:
            out.append(
                img.crop(
                    (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad + 1), min(h, y1 + pad + 1))
                )
            )
    return out


def split_rows(img: Image.Image, n: int) -> list[Image.Image]:
    """Cut a picture holding `n` printed rows (practice problems) into one picture
    per row, at the widest ink-free band near each expected border."""
    a = np.asarray(img, dtype=np.float32) < 128
    h = a.shape[0]
    empty = (a.sum(axis=1) == 0).tolist()
    cuts = [0]
    for k in range(1, n):
        lo, hi = round(h * (k / n - 0.25 / n)), round(h * (k / n + 0.25 / n))
        runs = [(r0 + lo, r1 + lo) for r0, r1 in _runs(empty[lo:hi])]
        if runs:
            r0, r1 = max(runs, key=lambda r: r[1] - r[0])
            cuts.append((r0 + r1) // 2)
        else:
            cuts.append(round(h * k / n))
    cuts.append(h)
    return [img.crop((0, cuts[i], img.size[0], cuts[i + 1])) for i in range(n)]


def split_panels(img: Image.Image, n: int, inset: int = 6) -> list[Image.Image]:
    """Cut a strip of `n` framed panels (practice problems side by side) at the
    printed vertical frame lines between them (fallback: equal widths)."""
    a = np.asarray(img, dtype=np.float32) < 120
    w = a.shape[1]
    strong = (a.mean(axis=0) > 0.75).tolist()
    lines = [(c0 + c1) / 2 for c0, c1 in _runs(strong)]
    edges = [0.0]
    for k in range(1, n):
        want = w * k / n
        near = [x for x in lines if abs(x - want) < w / (3 * n)]
        edges.append(min(near, key=lambda x: abs(x - want)) if near else want)
    edges.append(float(w))
    out = []
    for i in range(n):
        x0 = round(edges[i]) + (inset if i else 0)
        x1 = round(edges[i + 1]) - (inset if i < n - 1 else 0)
        out.append(img.crop((x0, 0, x1, img.size[1])))
    return out


def strip_tags(text: str) -> str:
    """Drop the transcriber's "[Item page] " style line tags, keep the book text."""
    return _TAG_RE.sub("", text).strip()


def _letters(n: int) -> list[str]:
    return [chr(ord("a") + i) for i in range(n)]


def overall_seconds_of(sections: Sequence[dict[str, Any]], intro_cap: int) -> int:
    """The delivery deadline rule over authoring section dicts: every section's
    intro allowance + study window + answering phase."""
    return sum(
        intro_cap
        + int((sec.get("study") or {}).get("study_seconds") or 0)
        + int(sec["time_limit_seconds"])
        for sec in sections
    )


# --------------------------------------------------------------------------- #
# Builder.
# --------------------------------------------------------------------------- #
class Builder:
    def __init__(self, src: Path) -> None:
        self.src = src
        self.book: dict[str, Any] = json.loads((src / "source.json").read_text("utf-8"))
        self.layout: dict[str, Any] = json.loads((src / "layout.json").read_text("utf-8"))
        self.pages = Pages(src, self.layout.get("deskew", {}))
        self.out = src / "import"
        self.assets_dir = self.out / "assets"
        self.assets: dict[str, Image.Image] = {}
        self.notes: list[str] = []  # needs a human look (PREVIEW + final log)
        self.test_id: str = self.layout["test_id"]
        self.subtests = {s["n"]: s for s in self.book["subtests"]}
        for u in self.book.get("uncertain", []):
            self.notes.append(f"transcription — {u['where']}: {u['issue']}")

    # -- ids ---------------------------------------------------------------- #
    def sid(self, n: int) -> str:
        return f"{self.test_id}-t{n}"

    def iid(self, n: int, q: int) -> str:
        return f"{self.test_id}-t{n}-q{q:02d}"

    def asset(self, name: str, img: Image.Image) -> str:
        asset_id = f"{self.test_id}-{name}.png"
        if asset_id in self.assets:
            raise ValueError(f"duplicate asset {asset_id}")
        self.assets[asset_id] = img
        return asset_id

    # -- sections ------------------------------------------------------------ #
    def section(self, n: int, items: list[dict[str, Any]], intro: dict[str, Any]) -> dict[str, Any]:
        st = self.subtests[n]
        return {
            "id": self.sid(n),
            "title": f"Test {n}: {st['name']}",
            "stimulus": {"kind": "passage_text", "text": ""},
            "items": items,
            "time_limit_seconds": int(st["time_seconds"]),
            "intro": intro,
        }

    def _closing(self, st: dict[str, Any]) -> str:
        sample = st.get("sample") or {}
        return str(sample.get("closing") or "")

    def _intro(
        self,
        st: dict[str, Any],
        samples: list[dict[str, Any]],
        prefix: str = "",
        practice: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """The subtest's intro. `samples` = the book's worked examples (with their
        answers); `practice` = the book's practice problems as INTERACTIVE
        `IntroPractice` (answered + checked on the runner's Example screen, never
        scored — their answers are printed in the booklet next to them)."""
        text = strip_tags(st["instructions"])
        closing = self._closing(st)
        parts = [p for p in (prefix, text, closing) if p]
        return {
            "text": "\n\n".join(parts),
            "images": [],
            "samples": samples,
            "practice": practice or [],
        }

    @staticmethod
    def _per_problem(text: str, n: int) -> list[str]:
        """A practice explanation printed once for n problems: split per sentence
        when it has exactly n sentences, else the whole text for each."""
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
        return parts if len(parts) == n else [text.strip()] * n

    def recall_crops(self) -> list[Image.Image]:
        """The recall page's symbol boxes (its own order, no numbers), one clean
        picture per slot in reading order (row by row, left to right): slot k is
        recall item k. Each is a different asset from the study grid (the study
        picture's id is known to the client), so no recall item reuses it."""
        geo = self.layout["t1"]
        boxes = [
            (x0, y0, x1, y1)
            for (y0, y1), row in zip(geo["recall_y"], geo["recall_x"], strict=True)
            for x0, x1 in row
        ]
        # pad 6 around the symbol box, but only 2 below it: the empty answer box
        # printed under each symbol must not show as stubs at the bottom edge.
        pad, below = 6, 2
        out = []
        for box in boxes:
            img = crop(self.pages, geo["recall_page"], box, pad=pad)
            out.append(img.crop((0, 0, img.size[0], img.size[1] - (pad - below))))
        return out

    def t1(self) -> dict[str, Any]:
        """Symbol memory: study the numbered grid (study window), then recall the
        number of each symbol — one gap_fill per recall slot with that symbol's
        picture. Book timing: `time_seconds_study` to study, the rest of
        `time_seconds` to recall (book: 1 min + 1 min); delivery runs the answering
        phase for `time_limit_seconds` AFTER the study window."""
        st, geo = self.subtests[1], self.layout["t1"]
        # The study sheet as one picture per symbol box (symbol + its number): a
        # phone shows them as a legible grid instead of one tiny 10-wide sheet.
        sheet = crop(self.pages, geo["study_page"], geo["study_box"])
        cells = grid_cells(sheet, rows=2, cols=10)
        study_ids = [self.asset(f"t1-study-{k:02d}", c) for k, c in enumerate(cells, 1)]
        crops = self.recall_crops()
        if len(st["items"]) != len(crops):
            raise ValueError("T1: recall slots != items")
        study_s = int(st["time_seconds_study"])
        recall_s = int(st["time_seconds"]) - study_s
        if recall_s <= 0:
            raise ValueError("T1: time_seconds must exceed time_seconds_study")
        items = []
        for it in st["items"]:
            n = int(it["n"])
            items.append(
                {
                    "item_type": "gap_fill",
                    "id": self.iid(1, n),
                    "prompt": "Number for this symbol",
                    "image": self.asset(f"t1-recall-{n:02d}", crops[n - 1]),
                    "accepted": [str(it["answer"])],
                    "spelling": "exact",
                    "max_words": 1,
                    "numeric": True,
                }
            )
        if any(i["image"] in study_ids for i in items):
            raise ValueError("T1: a recall item reuses the study picture")
        general = strip_tags(self.book["instructions_general"])
        sec = self.section(1, items, self._intro(st, [], prefix=general))
        sec["time_limit_seconds"] = recall_s
        sec["study"] = {"images": study_ids, "study_seconds": study_s}
        return sec

    def t2(self) -> dict[str, Any]:
        st = self.subtests[2]
        what = {
            "missing_digit": "Missing digit",
            "duplicate_letter": "Repeated letter",
        }
        items = []
        for it in st["items"]:
            item: dict[str, Any] = {
                "item_type": "gap_fill",
                "id": self.iid(2, it["n"]),
                "prompt": f"{what[it['kind']]}: {it['prompt']}",
                "accepted": [str(it["answer"])],
                "spelling": "exact",
                "max_words": 1,
            }
            if it["kind"] == "missing_digit":
                item["numeric"] = True
            if "answer_printed" in it and str(it["answer"]) == "0":
                # The key prints the zero like a letter O; a student may type it so.
                item["accepted_variants"] = ["O"]
                self.notes.append(
                    f"T2 Q{it['n']}: key printed {it['answer_printed']!r}; accepting '0' "
                    "and the letter 'O'"
                )
            items.append(item)
        sm = st["sample"]
        samples = [{"text": f"{e['prompt']}  ->  {e['answer']}"} for e in sm["examples"]] + [
            {"text": sm["sample_text"]}
        ]
        why = self._per_problem(sm["practice_text"], len(sm["practice"]))
        practice = [
            {
                "kind": "text",
                "prompt": (
                    f"{what['missing_digit']}: {e['prompt']}"
                    if str(e["prompt"]).isdigit()
                    else f"{what['duplicate_letter']}: {e['prompt']}"
                ),
                "solution": str(e["answer"]),
                "explanation": w,
            }
            for e, w in zip(sm["practice"], why, strict=True)
        ]
        return self.section(2, items, self._intro(st, samples, practice=practice))

    def t3(self) -> dict[str, Any]:
        st = self.subtests[3]
        items = []
        for it in st["items"]:
            keys = _letters(len(it["options"]))
            correct = keys[it["options"].index(it["answer"])]
            items.append(
                {
                    "item_type": "single_choice",
                    "id": self.iid(3, it["n"]),
                    "prompt": "Odd one out?",
                    "options": [
                        {"kind": "text", "key": k, "text": w}
                        for k, w in zip(keys, it["options"], strict=True)
                    ],
                    "correct": correct,
                    "shuffle_options": False,
                }
            )
        sm = st["sample"]
        samples = [
            {"text": f"{' · '.join(e['options'])}  ->  {e['answer']}. {e['explanation']}"}
            for e in sm["examples"]
        ]
        practice = []
        for e in sm["practice"]:
            keys = _letters(len(e["options"]))
            practice.append(
                {
                    "kind": "choice",
                    "prompt": "Odd one out?",
                    "options": [
                        {"key": k, "text": w} for k, w in zip(keys, e["options"], strict=True)
                    ],
                    "solution": keys[e["options"].index(e["answer"])],
                    "explanation": f"{e['answer']}. {e['explanation']}",
                }
            )
        return self.section(3, items, self._intro(st, samples, practice=practice))

    def t4(self) -> dict[str, Any]:
        st, geo = self.subtests[4], self.layout["t4"]
        items = []
        for it in st["items"]:
            q = it["n"]
            options = []
            for key in _letters(5):
                img = tighten(
                    crop(self.pages, geo["page"], it["option_bboxes"][key], pad=geo["pad"])
                )
                options.append(
                    {
                        "kind": "image",
                        "key": key,
                        "asset_id": self.asset(f"t4-q{q:02d}-{key}", img),
                        "alt": f"({key})",
                    }
                )
            items.append(
                {
                    "item_type": "single_choice",
                    "id": self.iid(4, q),
                    "prompt": "Odd one out?",
                    "options": options,
                    "correct": it["answer"],
                    "shuffle_options": False,
                }
            )
        sm = st["sample"]
        ex_img = self.asset(
            "t4-sample", crop(self.pages, geo["sample_page"], geo["sample_boxes"][0])
        )
        rows = split_rows(
            crop(self.pages, geo["sample_page"], geo["sample_boxes"][1]), len(sm["practice"])
        )
        samples = [
            {
                "text": "Sample problems: "
                + " ".join(f"{i}. {e['explanation']}" for i, e in enumerate(sm["examples"], 1)),
                "images": [ex_img],
            },
        ]
        practice = []
        for i, (e, row_img) in enumerate(zip(sm["practice"], rows, strict=True), 1):
            practice.append(
                {
                    "kind": "choice",
                    "prompt": "Odd one out?",
                    "images": [self.asset(f"t4-practice-{i}", row_img)],
                    "options": [{"key": k} for k in e["options"]],
                    "solution": e["answer"],
                    "explanation": e["explanation"],
                }
            )
        return self.section(4, items, self._intro(st, samples, practice=practice))

    def t5(self) -> dict[str, Any]:
        st = self.subtests[5]
        items = [
            {
                "item_type": "mark_toggle",
                "id": self.iid(5, it["n"]),
                "prompt": f"{it['left']} ....... {it['right']}",
                "correct": it["left"] != it["right"],
            }
            for it in st["items"]
        ]
        for it in st["items"]:
            if (it["left"] != it["right"]) != bool(it["answer"]):
                raise ValueError(f"T5 Q{it['n']}: key disagrees with the printed pair")
        sm = st["sample"]

        def row(e: dict[str, Any]) -> str:
            return f"{e['left']} {'...X...' if e['answer'] else '.......'} {e['right']}"

        samples = [
            {"text": "\n".join(row(e) for e in sm["examples"]) + "\n" + sm["sample_text"]},
        ]
        practice = [
            {
                "kind": "mark",
                "prompt": f"{e['left']} ....... {e['right']}",
                "solution": "1" if e["answer"] else "0",
                "explanation": sm["practice_text"],
            }
            for e in sm["practice"]
        ]
        return self.section(5, items, self._intro(st, samples, practice=practice))

    def _cell_images(self) -> dict[int, Image.Image]:
        geo = self.layout["t6"]
        gx, gy, inset = geo["grid_x"], geo["grid_y"], geo["inset"]
        out: dict[int, Image.Image] = {}
        for r in range(len(gy) - 1):
            for c in range(len(gx) - 1):
                n = r * (len(gx) - 1) + c + 1
                box = (gx[c] + inset, gy[r] + inset, gx[c + 1] - inset, gy[r + 1] - inset)
                out[n] = crop(self.pages, geo["page"], box)
        return out

    def t6(self) -> dict[str, Any]:
        st, geo = self.subtests[6], self.layout["t6"]
        cells = self._cell_images()
        items = []
        for it in st["items"]:
            q = it["n"]
            spec = geo["zones"].get(str(q))
            if not spec:
                raise ValueError(f"T6 Q{q}: no zone authored in layout.json")
            target: dict[str, Any] = {"mode": "region", "zones": spec["zones"]}
            for key in ("min_ink", "min_inside_ratio", "margin", "require_all_zones"):
                if key in spec:
                    target[key] = spec[key]
            items.append(
                {
                    "item_type": "drawing",
                    "id": self.iid(6, q),
                    "prompt": DRAW_MISSING_PROMPT,
                    "asset_id": self.asset(f"t6-q{q:02d}", cells[q]),
                    "mode": "region",
                    "target": target,
                    "_answer": it["answer"],
                }
            )
        sm = st["sample"]
        img = self.asset("t6-sample", crop(self.pages, geo["sample_page"], geo["sample_box"]))
        samples = [
            {
                "text": " ".join(e["explanation"] for e in sm["examples"]),
                "images": [img],
            }
        ]
        return self.section(6, items, self._intro(st, samples))

    def t7(self) -> dict[str, Any]:
        st = self.subtests[7]
        items = []
        for it in st["items"]:
            q = it["n"]
            prompt = it["question"]
            if "clues" in it:
                prompt += "\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(it["clues"], 1))
            if "options" in it:
                opts = it["options"]
                keys = list(opts)
                texts = [opts[k] for k in keys]
                correct = it["answer"]
            else:  # the five-houses puzzle: the book wants a name -> its five men
                names = re.findall(r"Mr\. [A-Z][a-z]+", it["question"])
                texts = list(dict.fromkeys(names))
                keys = _letters(len(texts))
                correct = keys[texts.index(it["answer"])]
            item: dict[str, Any] = {
                "item_type": "single_choice",
                "id": self.iid(7, q),
                "prompt": prompt,
                "options": [
                    {"kind": "text", "key": k, "text": t} for k, t in zip(keys, texts, strict=True)
                ],
                "correct": correct,
                "shuffle_options": False,
            }
            if int(it.get("points", 1)) != 1:
                item["points"] = int(it["points"])
            items.append(item)
        e = st["sample"]["examples"][0]
        samples = [
            {
                "text": "\n".join(e["statements"])
                + f"\n{e['question']}\n"
                + "\n".join(f"({k}) {v}" for k, v in e["options"].items())
                + f"\nAnswer: {e['explanation']}"
            }
        ]
        return self.section(7, items, self._intro(st, samples))

    def t8(self) -> dict[str, Any]:
        st, geo = self.subtests[8], self.layout["t8"]
        ans = self.pages.array(geo["answer_page"])
        items = []
        for it in st["items"]:
            q = it["n"]
            box = inner_box(self.pages.array(geo["page"]), it["item_bbox"], geo["inset"])
            img = crop(self.pages, geo["page"], box)
            sl, stp, sr, sb = find_blank_square(img)
            w, h = img.size
            square = {
                "x": round(sl / w, 4),
                "y": round(stp / h, 4),
                "w": round((sr - sl) / w, 4),
                "h": round((sb - stp) / h, 4),
            }
            sq = square_of_page_box(ans, it["answer_bbox"])
            segs, notes = refine_segments(ans, sq, geo["segments_approx"][str(q)])
            self.notes += [f"T8 Q{q}: {n}" for n in notes]
            items.append(
                {
                    "item_type": "drawing",
                    "id": self.iid(8, q),
                    "prompt": DIVIDE_PROMPT,
                    "asset_id": self.asset(f"t8-q{q:02d}", img),
                    "mode": "lines",
                    "target": {
                        "mode": "lines",
                        "square": square,
                        "segments": [{"a": list(a), "b": list(b)} for a, b in segs],
                        "tolerance": geo["tolerance"],
                        "allow_transforms": True,
                    },
                    "_answer": it["answer"],
                    "_answer_square": sq,
                }
            )
        s_img = self.asset(
            "t8-sample", crop(self.pages, geo["sample_page"], geo["sample_boxes"][0])
        )
        panels = split_panels(
            crop(self.pages, geo["sample_page"], geo["sample_boxes"][1]),
            len(st["sample"]["practice"]),
        )
        samples = [{"text": "Samples 1-3 with their answers.", "images": [s_img]}]
        # The booklet prints no answers for practice 4-6: a "try it" canvas each,
        # with a reminder instead of an answer picture.
        practice = [
            {
                "kind": "drawing",
                "prompt": DIVIDE_PROMPT,
                "images": [self.asset(f"t8-practice-{e['n']}", panel)],
                "explanation": (
                    "The booklet prints no answer for this practice problem. Check that "
                    "your lines cut the empty square into exactly the pieces shown — the "
                    "pieces may be turned or flipped."
                ),
            }
            for e, panel in zip(st["sample"]["practice"], panels, strict=True)
        ]
        st_notes = st.get("notes")
        intro = self._intro(st, samples, practice=practice)
        if st_notes:
            intro["text"] += f"\n\n{st_notes}"
        return self.section(8, items, intro)

    def t9(self) -> dict[str, Any]:
        st = self.subtests[9]
        items = [
            {
                "item_type": "gap_fill",
                "id": self.iid(9, it["n"]),
                "prompt": "Next number: " + ", ".join(str(v) for v in it["sequence"]) + ", ___",
                "accepted": [str(it["answer"])],
                "spelling": "exact",
                "max_words": 1,
                "numeric": True,
            }
            for it in st["items"]
        ]
        sm = st["sample"]
        samples = [
            {"text": f"{' '.join(map(str, e['sequence']))}  ->  {e['answer']}. {e['explanation']}"}
            for e in sm["examples"]
        ]
        practice = [
            {
                "kind": "text",
                "prompt": "Next number: " + ", ".join(str(v) for v in e["sequence"]) + ", ___",
                "solution": str(e["answer"]),
                "explanation": e["explanation"],
            }
            for e in sm["practice"]
        ]
        return self.section(9, items, self._intro(st, samples, practice=practice))

    def t10(self) -> dict[str, Any]:
        st = self.subtests[10]
        items = []
        for it in st["items"]:
            items.append(
                {
                    "item_type": "gap_fill",
                    "id": self.iid(10, it["n"]),
                    "prompt": "One word for all three: " + "   ".join(it["words"]),
                    "accepted": [str(it["answer"])],
                    "spelling": "exact",
                    "max_words": 1,
                }
            )
            if "answer_printed" in it:
                self.notes.append(
                    f"T10 Q{it['n']}: key printed {it['answer_printed']!r}, recorded "
                    f"{it['answer']!r}"
                )
        sm = st["sample"]
        samples = [
            {"text": f"{' '.join(e['words'])}  ->  {e['answer']}. {e['explanation']}"}
            for e in sm["examples"]
        ]
        practice = [
            {
                "kind": "text",
                "prompt": "One word for all three: " + "   ".join(e["words"]),
                "solution": str(e["answer"]),
                "explanation": e["explanation"],
            }
            for e in sm["practice"]
        ]
        return self.section(10, items, self._intro(st, samples, practice=practice))

    # -- norms --------------------------------------------------------------- #
    def norm_scoring(self) -> dict[str, Any]:
        b = self.book
        summ = b["scoring_summary"]
        groups = []
        for key in b["iq_by_age"]["age_groups"]:
            lo, hi = (int(v) for v in key.split("-"))
            groups.append({"key": key, "min_age": lo, "max_age": hi})
        rows = [{"sum": int(r["sum"]), "values": dict(r["iq"])} for r in b["iq_by_age"]["rows"]]
        brackets = [
            {"label": br["label"], "min": int(br["min"]) if br["min"] is not None else 0}
            for br in b["brackets"]
        ]
        tables = {
            self.sid(int(n)): [
                {
                    "raw_min": int(x["raw_min"]),
                    "raw_max": int(x["raw_max"]),
                    "scaled": int(x["scaled"]),
                }
                for x in sorted(bands, key=lambda x: x["raw_min"])
            ]
            for n, bands in b["scaled_conversion"].items()
        }
        lo_age, hi_age = b["meta"]["ages"]
        return {
            "composites": [
                {
                    "key": "L",
                    "label": "Left-brain (odd tests)",
                    "section_ids": [self.sid(n) for n in summ["left_brain_tests"]],
                },
                {
                    "key": "R",
                    "label": "Right-brain (even tests)",
                    "section_ids": [self.sid(n) for n in summ["right_brain_tests"]],
                },
            ],
            "scaled_tables": tables,
            "by_age": {"age_groups": groups, "rows": rows},
            "final": "mean_of_composites",
            "brackets": brackets,
            "min_age": int(lo_age),
            "note": (
                f"Book norms ({b['meta']['title']}, 1993): not standardized, non-clinical — "
                f"an indication only. Ages {lo_age}-{hi_age}. Raw 0 has no printed row "
                "(scored as the table's lowest band, flagged). A scaled sum above the "
                "table counts as its top row. " + str(b["iq_by_age"].get("footnote", ""))
            ),
        }

    # -- whole test ---------------------------------------------------------- #
    def build(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """(authoring Test JSON, private extras per item id for the preview)."""
        sections = [
            self.t1(), self.t2(), self.t3(), self.t4(), self.t5(),
            self.t6(), self.t7(), self.t8(), self.t9(), self.t10(),
        ]  # fmt: skip
        extras: dict[str, Any] = {}
        for sec in sections:
            for item in sec["items"]:
                extra = {k[1:]: item.pop(k) for k in list(item) if k.startswith("_")}
                if extra:
                    extras[item["id"]] = extra
        # Delivery's overall deadline for an all-timed test is Σ(intro cap + study +
        # answering) (app/delivery/sections.overall_seconds); `duration_minutes` is
        # not used for it, so it states that same maximum (default intro cap).
        total = overall_seconds_of(sections, settings.section_intro_max_seconds)
        test = {
            "id": self.test_id,
            "title": self.layout["title"],
            "level": "IQ",
            "status": "draft",
            "duration_minutes": math.ceil(total / 60),
            "sections": sections,
            "norm_scoring": self.norm_scoring(),
        }
        return test, extras


# --------------------------------------------------------------------------- #
# Checks.
# --------------------------------------------------------------------------- #
def validate(test_json: dict[str, Any]) -> Any:
    from app.content.editor import validate_test_data

    test = validate_test_data(test_json)
    log.info(
        "editor validation OK: %d sections, %d items",
        len(test.sections),
        sum(len(s.items) for s in test.sections),
    )
    return test


def check_max_raw(test: Any) -> list[str]:
    """Each section's max raw (sum of item points) == the top raw of its table."""
    from app.grading.service import item_max_points

    problems = []
    assert test.norm_scoring is not None
    for sec in test.sections:
        top = sum(item_max_points(i, test.level) for i in sec.items)
        bands = test.norm_scoring.scaled_tables.get(sec.id, [])
        table_top = max((b.raw_max for b in bands), default=None)
        status = "OK" if table_top == top else "MISMATCH"
        log.info("max raw %s: items=%g table=%s %s", sec.id, top, table_top, status)
        if table_top != top:
            problems.append(f"{sec.id}: items max {top:g} != table top {table_top}")
    return problems


def check_timing(test: Any) -> list[str]:
    """Delivery runs every section on its own clock (no silent degrade) and
    `duration_minutes` states delivery's overall deadline (default intro cap)."""
    from app.delivery.sections import overall_seconds, plan_sections

    plans = plan_sections(test)
    if plans is None:
        return ["not every section has time_limit_seconds -> section clocks off"]
    seconds = overall_seconds(plans, settings.section_intro_max_seconds)
    log.info(
        "timing: %d timed sections, clocks %d s + intro caps %d s = %d s -> %d min (test says %d)",
        len(plans),
        sum(p.study_seconds + p.time_limit_seconds for p in plans),
        settings.section_intro_max_seconds * len(plans),
        seconds,
        math.ceil(seconds / 60),
        test.duration_minutes,
    )
    t1 = plans[0]
    log.info("timing T1: study %d s, then recall %d s", t1.study_seconds, t1.time_limit_seconds)
    problems = []
    if math.ceil(seconds / 60) != test.duration_minutes:
        problems.append(f"duration_minutes {test.duration_minutes} != delivery {seconds} s")
    return problems


def _zone_poly(zone: dict[str, Any]) -> list[XY]:
    if zone["kind"] == "rect":
        x, y, w, h = zone["x"], zone["y"], zone["w"], zone["h"]
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return [(float(p[0]), float(p[1])) for p in zone["points"]]


def _inside(p: XY, poly: Sequence[XY]) -> bool:
    x, y = p
    inside = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def scribble_in(poly: Sequence[XY], aspect: float, need: float) -> list[list[XY]]:
    """Serpentine strokes through interior grid points of `poly` with >= `need` ink
    (picture widths). `aspect` = h / w of the picture (v is in heights)."""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    for n in range(4, 40, 2):
        strokes: list[list[XY]] = []
        for r in range(n):
            v = min(ys) + (r + 0.5) / n * (max(ys) - min(ys))
            row = [(min(xs) + (c + 0.5) / n * (max(xs) - min(xs)), v) for c in range(n)]
            if r % 2:
                row.reverse()
            cur: list[XY] = []
            for p in row:
                if _inside(p, poly):
                    cur.append(p)
                elif cur:
                    strokes.append(cur)
                    cur = []
            if cur:
                strokes.append(cur)
        strokes = [s for s in strokes if len(s) >= 2]
        ink = sum(
            math.hypot(b[0] - a[0], (b[1] - a[1]) * aspect)
            for s in strokes
            for a, b in zip(s, s[1:], strict=False)
        )
        if ink >= need:
            return strokes
    raise ValueError("zone too small for a synthetic scribble")


def far_scribble(zones: list[list[XY]]) -> list[list[XY]]:
    """A small closed scribble (0.14 x 0.14) in the spot farthest from every zone."""
    best: list[list[XY]] = []
    best_d = -1.0
    for cx in (0.1, 0.3, 0.5, 0.7, 0.9):
        for cy in (0.1, 0.3, 0.5, 0.7, 0.9):
            box = [
                (cx - 0.07, cy - 0.07),
                (cx + 0.07, cy - 0.07),
                (cx + 0.07, cy + 0.07),
                (cx - 0.07, cy + 0.07),
            ]
            if any(_inside(p, z) for z in zones for p in [*box, (cx, cy)]):
                continue
            d = min(
                min(math.hypot(px - qx, py - qy) for px, py in z for qx, qy in box) for z in zones
            )
            if d > best_d:
                best, best_d = [[*box, box[0]]], d
    return best


def payload(w: float, h: float, strokes_uv: Sequence[Sequence[XY]]) -> str:
    return json.dumps(
        {"w": w, "h": h, "strokes": [[[u * w, v * h] for u, v in s] for s in strokes_uv]}
    )


def _identity(p: XY) -> XY:
    return p


def line_strokes(
    square: dict[str, float],
    segs: Sequence[tuple[XY, XY]],
    fn: Callable[[XY], XY] = _identity,
) -> list[list[XY]]:
    """Strokes along square-local segments (optionally transformed), picture coords."""
    out = []
    for a, b in segs:
        a2, b2 = fn(a), fn(b)
        pts = [
            (a2[0] + k / 12 * (b2[0] - a2[0]), a2[1] + k / 12 * (b2[1] - a2[1])) for k in range(13)
        ]
        out.append([(square["x"] + s * square["w"], square["y"] + t * square["h"]) for s, t in pts])
    return out


class _Content:
    def __init__(self, test: Any) -> None:
        self.test = test

    def get_test(self, test_id: str) -> Any:
        return self.test if test_id == self.test.id else None


class _Attempts:
    """Duck-typed attempt repository for the in-memory self-grade."""

    def __init__(self, test_id: str, answers: dict[str, str], birth: date) -> None:
        self.attempt = SimpleNamespace(
            id="selfcheck",
            test_id=test_id,
            papers=None,
            roster_entry_id="r1",
            started_at=datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        )
        self.answers = answers
        self.entry = SimpleNamespace(birth_date=birth)

    def get_attempt(self, attempt_id: str) -> Any:
        return self.attempt

    def get_answers(self, attempt_id: str) -> list[Any]:
        return [SimpleNamespace(item_id=k, response=v) for k, v in self.answers.items()]

    def get_roster_entry(self, entry_id: str) -> Any:
        return self.entry


def _segs(tgt: dict[str, Any]) -> list[tuple[XY, XY]]:
    return [((s["a"][0], s["a"][1]), (s["b"][0], s["b"][1])) for s in tgt["segments"]]


def self_grade(test: Any, sizes: dict[str, tuple[int, int]]) -> list[str]:
    """All-correct responses must score every point; wrong ones must not."""
    from app.grading.drawing import TRANSFORMS, grade_drawing
    from app.grading.norm_scoring import compute_iq_report, scaled_for
    from app.grading.service import GradingService
    from contracts import DrawingItem, GapFillItem, MarkToggleItem, SingleChoiceItem

    problems: list[str] = []
    answers: dict[str, str] = {}
    drawings: list[Any] = []
    for sec in test.sections:
        for it in sec.items:
            if isinstance(it, SingleChoiceItem):
                answers[it.id] = it.correct
            elif isinstance(it, GapFillItem):
                answers[it.id] = it.accepted[0]
            elif isinstance(it, MarkToggleItem):
                answers[it.id] = "1" if it.correct else "0"
            elif isinstance(it, DrawingItem):
                drawings.append(it)
                w, h = sizes[it.asset_id]
                tgt = it.target.model_dump(mode="json")
                if it.mode == "region":
                    polys = [_zone_poly(z) for z in tgt["zones"]]
                    need = 2.0 * tgt["min_ink"] / len(polys)
                    strokes = [s for p in polys for s in scribble_in(p, h / w, need)]
                else:
                    strokes = line_strokes(tgt["square"], _segs(tgt))
                answers[it.id] = payload(w, h, strokes)
    birth = date(1994, 5, 1)
    svc = GradingService(_Content(test), _Attempts(test.id, answers, birth))  # type: ignore[arg-type]
    result = svc.grade("selfcheck")
    log.info(
        "self-grade all-correct: %.0f / %.0f, needs_review=%s",
        result.score,
        result.max_score,
        result.needs_review,
    )
    for g in result.items:
        if g.awarded != g.max_points or g.needs_review:
            problems.append(
                f"all-correct {g.item_id}: {g.awarded}/{g.max_points} "
                f"review={g.needs_review} {g.feedback or ''}"
            )
    if result.score != result.max_score:
        problems.append(f"all-correct total {result.score} != {result.max_score}")
    report = svc.iq_report(result)
    if report is not None:
        log.info(
            "self-grade IQ (age 32): composites=%s overall=%s bracket=%s notes=%s",
            [(c.key, c.scaled_sum, c.score_display) for c in report.composites],
            report.overall_display,
            report.bracket,
            report.notes,
        )

    # Drawing robustness: rotated answers pass; outside / wrong figure / partial fail.
    rotated_ok = outside_zero = cross_zero = partial_zero = 0
    for it in drawings:
        w, h = sizes[it.asset_id]
        tgt = it.target.model_dump(mode="json")
        if it.mode == "region":
            bad = far_scribble([_zone_poly(z) for z in tgt["zones"]])
            g = grade_drawing(it, payload(w, h, bad))
            outside_zero += int(not g.awarded)
            if g.awarded:
                problems.append(f"outside scribble scored on {it.id}: {g.feedback}")
            continue
        segs = _segs(tgt)
        rot = TRANSFORMS[1][1]
        g = grade_drawing(it, payload(w, h, line_strokes(tgt["square"], segs, rot)))
        rotated_ok += int(g.awarded == g.max_points)
        if not g.awarded:
            problems.append(f"rotated answer refused on {it.id}: {g.feedback}")
        cross = [((0.5, 0.0), (0.5, 1.0)), ((0.0, 0.5), (1.0, 0.5))]
        g = grade_drawing(it, payload(w, h, line_strokes(tgt["square"], cross)))
        cross_zero += int(not g.awarded)
        if g.awarded:
            problems.append(f"'+' cross scored on {it.id}: {g.feedback}")
        if len(segs) > 1:
            g = grade_drawing(it, payload(w, h, line_strokes(tgt["square"], segs[:-1])))
            partial_zero += int(not g.awarded)
            if g.awarded:
                problems.append(f"missing segment scored on {it.id}: {g.feedback}")
    log.info(
        "self-grade drawings: outside scribble -> 0 on %d region items; rotated 90° "
        "accepted on %d lines items; '+' cross -> 0 on %d; one segment missing -> 0 on %d",
        outside_zero,
        rotated_ok,
        cross_zero,
        partial_zero,
    )

    # Wrong answers everywhere + every row marked -> 0.
    wrong: dict[str, str] = {}
    for sec in test.sections:
        for it in sec.items:
            if isinstance(it, SingleChoiceItem):
                wrong[it.id] = next(o.key for o in it.options if o.key != it.correct)
            elif isinstance(it, GapFillItem):
                wrong[it.id] = "zzz"
            elif isinstance(it, MarkToggleItem):
                wrong[it.id] = "1"
    svc_w = GradingService(_Content(test), _Attempts(test.id, wrong, birth))  # type: ignore[arg-type]
    res_w = svc_w.grade("selfcheck")
    log.info("self-grade all-wrong + every row marked: %.0f / %.0f", res_w.score, res_w.max_score)
    if res_w.score != 0:
        problems.append(f"all-wrong scored {res_w.score}")

    # The book's worked examples.
    ns = test.norm_scoring
    t1 = test.sections[0].id
    scaled, _ = scaled_for(5, ns.scaled_tables[t1])
    log.info("book example 'Test 1 raw 5 -> scaled 11': got %s", scaled)
    if scaled != 11:
        problems.append(f"Test 1 raw 5 -> {scaled}, book says 11")
    row = next((r for r in ns.by_age.rows if r.sum == 55), None)
    val = row.values.get("30-34") if row is not None else None
    log.info("book example 'odd sum 55, age 30-34 -> IQ 100': got %s", val)
    if val != 100:
        problems.append(f"sum 55 age 30-34 -> {val}, book says 100")
    raw_top = {s.id: max(b.raw_max for b in ns.scaled_tables[s.id]) for s in test.sections}
    rep = compute_iq_report(raw_top, ns, 32)
    log.info(
        "full marks at 32: %s -> %s (%s)",
        [c.score_display for c in rep.composites],
        rep.overall_display,
        rep.bracket,
    )
    return problems


def import_check(out: Path) -> list[str]:
    """Import into a THROW-AWAY sqlite db + assets dir, export back, compare."""
    from contracts import Test

    problems: list[str] = []
    test_id = json.loads((out / "test.json").read_text("utf-8"))["id"]
    with tempfile.TemporaryDirectory(prefix="iq_import_check_") as tmp:
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite:///{Path(tmp, 'check.db').as_posix()}"
        env["ASSETS_DIR"] = str(Path(tmp, "assets"))
        env["PYTHONIOENCODING"] = "utf-8"
        base = [sys.executable, "-m", "app.content.transfer"]
        log.info("temp-db import: DATABASE_URL=%s", env["DATABASE_URL"])
        imp = subprocess.run(
            [
                *base,
                "import",
                "--file",
                str(out / "test.json"),
                "--assets-dir",
                str(out / "assets"),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in (imp.stdout + imp.stderr).splitlines():
            log.info("  transfer| %s", line)
        if imp.returncode != 0:
            return [f"import into the temp db failed (exit {imp.returncode})"]
        exported = Path(tmp, "export.json")
        exp = subprocess.run(
            [*base, "export", "--id", test_id, "--out", str(exported)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if exp.returncode != 0:
            return [f"export from the temp db failed (exit {exp.returncode}): {exp.stderr}"]
        a = Test.model_validate_json((out / "test.json").read_text("utf-8"))
        b = Test.model_validate_json(exported.read_text("utf-8"))
        if a.model_dump(mode="json") != b.model_dump(mode="json"):
            problems.append("round trip through the temp db changed the test")
        stored = [p for p in Path(tmp, "assets").rglob("*") if p.is_file()]
        log.info(
            "temp-db import + export round trip: %s; %d asset file(s) stored",
            "identical" if not problems else "DIFFERENT",
            len(stored),
        )
    return problems


# --------------------------------------------------------------------------- #
# Preview page (teacher-only, local).
# --------------------------------------------------------------------------- #
def _svg_overlay(item: dict[str, Any]) -> str:
    tgt = item["target"]
    parts = []
    if tgt["mode"] == "region":
        for z in tgt["zones"]:
            pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in _zone_poly(z))
            parts.append(f'<polygon points="{pts}" class="zone"/>')
    else:
        sq = tgt["square"]
        parts.append(
            f'<rect x="{sq["x"]}" y="{sq["y"]}" width="{sq["w"]}" height="{sq["h"]}" class="sq"/>'
        )
        for s in tgt["segments"]:
            (ax, ay), (bx, by) = s["a"], s["b"]
            parts.append(
                f'<line x1="{sq["x"] + ax * sq["w"]:.4f}" y1="{sq["y"] + ay * sq["h"]:.4f}" '
                f'x2="{sq["x"] + bx * sq["w"]:.4f}" y2="{sq["y"] + by * sq["h"]:.4f}" '
                'class="seg"/>'
            )
    return (
        '<svg class="ov" viewBox="0 0 1 1" preserveAspectRatio="none">' + "".join(parts) + "</svg>"
    )


def _img(asset_id: str, cls: str = "pic") -> str:
    a = html.escape(asset_id)
    return f'<img class="{cls}" src="assets/{a}" alt="{a}">'


_CSS = (
    "body{font:14px/1.45 system-ui,sans-serif;margin:16px auto;max-width:1100px;"
    "padding:0 16px;color:#222;background:#fff}"
    "h2{border-bottom:2px solid #333;margin-top:36px}"
    ".item{border:1px solid #ccc;border-radius:6px;padding:8px 10px;margin:8px 0}"
    ".key{background:#e5f6e5;padding:1px 6px;border-radius:4px;font-weight:600}"
    ".wrap{position:relative;display:inline-block;margin:4px 8px 4px 0;vertical-align:top}"
    ".wrap img{display:block;max-width:420px}"
    ".ov{position:absolute;inset:0;width:100%;height:100%}"
    ".zone{fill:rgba(255,0,0,.18);stroke:#d00;stroke-width:.004}"
    ".sq{fill:none;stroke:#06c;stroke-width:.004;stroke-dasharray:.01}"
    ".seg{stroke:#d00;stroke-width:.012;stroke-linecap:round;opacity:.75}"
    ".opt{display:inline-block;text-align:center;margin:2px 10px 2px 0}"
    ".opt img{max-height:90px;border:1px solid #ddd}.opt.ok img{outline:3px solid #2a2}"
    ".intro{white-space:pre-wrap;background:#f6f6f6;padding:8px;border-radius:6px}"
    ".notes li{color:#a40}.pic{max-width:100%}pre{white-space:pre-wrap;margin:4px 0}"
    ".small{color:#666;font-size:12px}"
)


def _render_item(n: int, it: dict[str, Any], extras: dict[str, Any], sheets: dict[str, str]) -> str:
    esc = html.escape
    pts = f" · {it['points']} pts" if it.get("points") else ""
    body = [
        f"<div class='item'><b>{n}.</b> <span class='small'>{esc(it['id'])} · "
        f"{esc(it['item_type'])}{pts}</span><br><pre>{esc(it.get('prompt', ''))}</pre>"
    ]
    t = it["item_type"]
    if t == "single_choice":
        for o in it["options"]:
            ok = o["key"] == it["correct"]
            if o["kind"] == "image":
                body.append(
                    f"<span class='opt{' ok' if ok else ''}'>{_img(o['asset_id'], '')}"
                    f"<br>({esc(o['key'])})</span>"
                )
            else:
                body.append(
                    f"<span class='opt'><span class='{'key' if ok else ''}'>({esc(o['key'])}) "
                    f"{esc(o['text'])}</span></span>"
                )
    elif t == "gap_fill":
        if it.get("image"):
            body.append(f"{_img(it['image'], '')}<br>")
        body.append(
            "Key: " + " / ".join(f"<span class='key'>{esc(a)}</span>" for a in it["accepted"])
        )
        if it.get("accepted_variants"):
            body.append(" · also: " + ", ".join(map(esc, it["accepted_variants"])))
        body.append(f" <span class='small'>(spelling {esc(str(it.get('spelling')))})</span>")
    elif t == "mark_toggle":
        key = "mark X (not identical)" if it["correct"] else "leave blank (identical)"
        body.append(f"Key: <span class='key'>{key}</span>")
    elif t == "drawing":
        ex = extras.get(it["id"], {})
        body.append(f"<div class='wrap'>{_img(it['asset_id'], '')}{_svg_overlay(it)}</div>")
        if it["id"] in sheets:
            body.append(
                f"<div class='wrap'><img src='{esc(sheets[it['id']])}' alt='printed answer'>"
                "<span class='small'>printed answer + fitted lines</span></div>"
            )
        tgt = it["target"]
        desc = {k: v for k, v in tgt.items() if k not in ("zones", "segments", "square")}
        body.append(
            f"<p>Key: <span class='key'>{esc(str(ex.get('answer', '')))}</span> "
            f"<span class='small'>{esc(json.dumps(desc))}</span></p>"
        )
    body.append("</div>")
    return "".join(body)


def render_preview(
    test: dict[str, Any],
    extras: dict[str, Any],
    notes: list[str],
    problems: list[str],
    sheets: dict[str, str],
) -> str:
    esc = html.escape
    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{esc(test['title'])} — preview</title><style>{_CSS}</style></head><body>",
        f"<h1>{esc(test['title'])}</h1>",
        f"<p>id <code>{esc(test['id'])}</code> · level {esc(test['level'])} · status "
        f"{esc(test['status'])} · {test['duration_minutes']} min overall (every intro cap + "
        "study + answering, as delivery sets the deadline). "
        "<b>Teacher-only (contains the key).</b> Red = target zone / reference line, "
        "blue dashed = the square.</p>",
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
        out.append(
            f"<h2>{esc(sec['title'] or sec['id'])} — {sec['time_limit_seconds']} s{extra}</h2>"
        )
        intro = sec.get("intro") or {}
        out.append(f"<div class='intro'>{esc(intro.get('text', ''))}</div>")
        for smp in intro.get("samples", []):
            out.append(
                f"<div class='item'><pre>{esc(smp.get('text', ''))}</pre>"
                + "".join(_img(a) for a in smp.get("images", []))
                + "</div>"
            )
        for k, pr in enumerate(intro.get("practice", []), 1):
            opts = " ".join(
                f"({o['key']}) {o.get('text', '')}".strip() for o in pr.get("options", [])
            )
            out.append(
                f"<div class='item'><b>Practice {k}</b> [{esc(pr.get('kind', 'text'))}] "
                f"{esc(pr.get('prompt', ''))}"
                + (f"<div>{esc(opts)}</div>" if opts else "")
                + "".join(_img(a) for a in pr.get("images", []))
                + f"<div class='key'>answer: {esc(pr.get('solution', '') or '—')} · "
                f"{esc(pr.get('explanation', ''))}</div>"
                + "".join(_img(a) for a in pr.get("solution_images", []))
                + "</div>"
            )
        if study:
            out.append(
                "<p><b>Study picture</b> (shown, then hidden):</p>"
                + (_img(study["asset_id"]) if study.get("asset_id") else "")
                + "".join(_img(a) for a in study.get("images", []))
            )
        stim = sec["stimulus"]
        if stim.get("text") or stim.get("images"):
            out.append(
                f"<p><b>Stimulus:</b> {esc(stim.get('text', ''))}</p>"
                + "".join(_img(a) for a in stim.get("images", []))
            )
        for n, it in enumerate(sec["items"], 1):
            out.append(_render_item(n, it, extras, sheets))
    nsc = test["norm_scoring"]
    out.append(
        "<h2>Norm scoring</h2><p>"
        + esc(nsc["note"])
        + "</p><p>Composites: "
        + "; ".join(f"{esc(c['key'])} = {esc(c['label'])}" for c in nsc["composites"])
        + "</p><p>Brackets: "
        + "; ".join(f"{esc(b['label'])} ≥ {b['min']}" for b in nsc["brackets"])
        + "</p><p class='small'>Scaled tables and the IQ-by-age table: see test.json.</p>"
    )
    out.append("</body></html>")
    return "\n".join(out)


def answer_overlay(
    arr: np.ndarray,
    sq: Sequence[float],
    segs: Sequence[dict[str, Any]],
    scale: int = 3,
) -> Image.Image:
    """The printed answer square enlarged, with the fitted reference lines on top."""
    sx, sy, sr, sb = sq
    m = 8
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).crop(
        (round(sx - m), round(sy - m), round(sr + m), round(sb + m))
    )
    w, h = img.size
    img = clean(img).convert("RGB").resize((w * scale, h * scale))
    d = ImageDraw.Draw(img)
    for s in segs:
        (ax, ay), (bx, by) = s["a"], s["b"]
        p = [
            ((m + ax * (sr - sx)) * scale, (m + ay * (sb - sy)) * scale),
            ((m + bx * (sr - sx)) * scale, (m + by * (sb - sy)) * scale),
        ]
        d.line(p, fill=(230, 0, 0), width=3)
    return img


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the private IQ book import.")
    parser.add_argument("--src", default=str(DEFAULT_SRC), help="Private source folder.")
    parser.add_argument("--no-check", action="store_true", help="Skip the self-grade.")
    parser.add_argument(
        "--import-check",
        action="store_true",
        help="Also import into a throw-away sqlite db and export it back.",
    )
    args = parser.parse_args(argv)
    # Never touch the developer's database: the app modules imported for validation /
    # grading create an engine from DATABASE_URL at import time -> in-memory sqlite.
    os.environ["DATABASE_URL"] = "sqlite://"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    configure_logging("INFO")
    src = Path(args.src)
    if "private" not in src.resolve().parts:
        log.error("refusing to build outside a private/ folder: %s", src)
        return 2

    b = Builder(src)
    test_json, extras = b.build()
    test = validate(test_json)
    problems = check_max_raw(test) + check_timing(test)

    out = b.out
    if b.assets_dir.exists():
        shutil.rmtree(b.assets_dir)
    b.assets_dir.mkdir(parents=True)
    for asset_id, img in b.assets.items():
        img.save(b.assets_dir / asset_id, optimize=True)
    sizes = {a: img.size for a, img in b.assets.items()}
    (out / "test.json").write_text(
        json.dumps(test.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    log.info("wrote %s + %d assets", out / "test.json", len(b.assets))

    # Printed-answer overlays for the "divide the square" items (preview only).
    sheets_dir = out / "preview"
    if sheets_dir.exists():
        shutil.rmtree(sheets_dir)
    sheets_dir.mkdir()
    ans = b.pages.array(b.layout["t8"]["answer_page"])
    sheets: dict[str, str] = {}
    for sec in test_json["sections"]:
        for it in sec["items"]:
            ex = extras.get(it["id"], {})
            if "answer_square" in ex:
                name = f"{it['id']}-printed.png"
                answer_overlay(ans, ex["answer_square"], it["target"]["segments"]).save(
                    sheets_dir / name
                )
                sheets[it["id"]] = f"preview/{name}"

    if not args.no_check:
        problems += self_grade(test, sizes)
    if args.import_check:
        problems += import_check(out)
    (out / "PREVIEW.html").write_text(
        render_preview(test.model_dump(mode="json"), extras, b.notes, problems, sheets),
        encoding="utf-8",
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
