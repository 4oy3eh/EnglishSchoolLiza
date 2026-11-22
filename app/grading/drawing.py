"""`drawing` grading — a PURE, deterministic stroke grader (no ML, no LLM).

Response (a JSON string the runner saves): {"w": W, "h": H, "strokes": [[[x, y],
...], ...]} in canvas pixels; the canvas covers the item's picture, so
u = x / W, v = y / H are normalized picture coordinates.

Two modes (the item's authoring-only `target`):

* **region** ("draw what is missing"): every stroke is resampled (step 0.004
  picture widths); each sample carries the ink length it stands for, measured in
  PICTURE WIDTHS. A sample is inside when it falls in a zone or within `margin` of
  one. Criteria: total ink >= `min_ink`; inside ink / total >= `min_inside_ratio`;
  with `require_all_zones` and several zones, each zone holds >= min_ink / zones.
* **lines** ("divide the square"): strokes are mapped into the square
  (square-local [0, 1] coordinates, distances in square sides) and simplified
  (Ramer-Douglas-Peucker, epsilon = tolerance / 4). For each allowed symmetry of
  the square (identity; with `allow_transforms` also rotations 90/180/270 and the
  four reflections) the reference segments are transformed and compared:
  - coverage of a reference segment = share of its 24 sample points within
    `tolerance` of the ink; the criterion is the WORST segment's coverage;
  - unexplained ink = share of the ink (samples; ink on the square's printed
    border ignored when `ignore_border`) farther than `tolerance` from every
    reference segment — a stray or extra line.
  The best symmetry wins (a passing one with the highest confidence, else the
  failing one closest to passing).

**Confidence.** Each criterion has a pass threshold and a "clear" value on each
side; its margin is 0 at the threshold and 1 at (or beyond) the clear value,
linear in between. Correct -> confidence = the smallest margin (every criterion
must hold); incorrect -> the largest margin among the FAILED criteria (one clear
failure suffices). Below `REVIEW_BELOW` the item goes to the teacher's review
queue (`needs_review`); the teacher's `ManualGrade` override settles it.

A blank drawing is 0 with full confidence. An unreadable / absurd payload (not
JSON, wrong shape, non-finite numbers, more than `MAX_STROKES` strokes or
`MAX_POINTS` points, > `MAX_RESPONSE_CHARS` chars) is 0 + needs_review — never an
exception.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.logging import get_logger
from contracts import DrawingItem, DrawingLinesTarget, DrawingRegionTarget, ItemGrade
from contracts.content import DRAWING_MAX_RESPONSE_CHARS, PolygonZone, RectZone

log = get_logger(__name__)

XY = tuple[float, float]

MAX_RESPONSE_CHARS = DRAWING_MAX_RESPONSE_CHARS  # shared with delivery's save cap
MAX_STROKES = 300
MAX_POINTS = 5_000
MAX_DIM = 20_000.0
REVIEW_BELOW = 0.5
DRAWING_DEFAULT_POINTS = 1.0

# region criteria (relative to the target's own thresholds)
_INK_CLEAR_PASS = 2.0  # x min_ink
_INK_CLEAR_FAIL = 0.5  # x min_ink
_INSIDE_CLEAR_PASS = 0.15  # threshold + 0.15
_INSIDE_CLEAR_FAIL = 0.25  # threshold - 0.25
_REGION_STEP = 0.004  # resampling step, picture widths

# lines criteria
COVERAGE_PASS, COVERAGE_CLEAR_PASS, COVERAGE_CLEAR_FAIL = 0.85, 0.97, 0.6
UNEXPLAINED_PASS, UNEXPLAINED_CLEAR_PASS, UNEXPLAINED_CLEAR_FAIL = 0.12, 0.03, 0.3
_REF_SAMPLES = 24
# Work caps (an adversarial scribble must not stall the grader): at most this many
# ink samples (the resampling step grows instead) and simplified ink segments
# (beyond -> 0 + needs_review).
MAX_SAMPLES = 20_000
MAX_INK_SEGMENTS = 1_500


class DrawingPayloadError(ValueError):
    """The saved drawing is not a readable {"w", "h", "strokes"} payload."""


@dataclass(frozen=True)
class DrawingVerdict:
    correct: bool
    confidence: float  # 0..1
    detail: str  # Russian, for the teacher's review row
    invalid: bool = False


# --------------------------------------------------------------------------- #
# Payload.
# --------------------------------------------------------------------------- #
def _num(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DrawingPayloadError("non-numeric coordinate")
    f = float(value)
    if not math.isfinite(f):
        raise DrawingPayloadError("non-finite coordinate")
    return f


def parse_drawing(response: str | None) -> tuple[float, float, list[list[XY]]]:
    """(w, h, strokes) from the saved JSON; "" / None -> an empty drawing (w=h=1).

    Raises `DrawingPayloadError` for anything malformed or absurdly large."""
    if response is None or not response.strip():
        return 1.0, 1.0, []
    if len(response) > MAX_RESPONSE_CHARS:
        raise DrawingPayloadError(f"payload too large ({len(response)} chars)")
    try:
        data = json.loads(response)
    except (ValueError, RecursionError) as exc:
        raise DrawingPayloadError("not JSON") from exc
    if not isinstance(data, dict):
        raise DrawingPayloadError("not an object")
    w, h = _num(data.get("w")), _num(data.get("h"))
    if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM):
        raise DrawingPayloadError("bad canvas size")
    raw = data.get("strokes")
    if not isinstance(raw, list) or len(raw) > MAX_STROKES:
        raise DrawingPayloadError("bad strokes list")
    strokes: list[list[XY]] = []
    total = 0
    for stroke in raw:
        if not isinstance(stroke, list):
            raise DrawingPayloadError("bad stroke")
        total += len(stroke)
        if total > MAX_POINTS:
            raise DrawingPayloadError("too many points")
        pts: list[XY] = []
        for p in stroke:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise DrawingPayloadError("bad point")
            x, y = _num(p[0]), _num(p[1])
            if not (-w <= x <= 2 * w and -h <= y <= 2 * h):
                raise DrawingPayloadError("point far outside the canvas")
            pts.append((x, y))
        if pts:
            strokes.append(pts)
    return w, h, strokes


# --------------------------------------------------------------------------- #
# Geometry helpers (pure).
# --------------------------------------------------------------------------- #
def _dist_point_segment(p: XY, a: XY, b: XY) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    den = dx * dx + dy * dy
    if den == 0.0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / den))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _point_in_polygon(p: XY, poly: Sequence[XY]) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def _zone_polygon(zone: RectZone | PolygonZone) -> list[XY]:
    if isinstance(zone, RectZone):
        return [
            (zone.x, zone.y),
            (zone.x + zone.w, zone.y),
            (zone.x + zone.w, zone.y + zone.h),
            (zone.x, zone.y + zone.h),
        ]
    return [(float(x), float(y)) for x, y in zone.points]


def _near_zone(p: XY, poly: Sequence[XY], margin: float) -> bool:
    if _point_in_polygon(p, poly):
        return True
    if margin <= 0:
        return False
    n = len(poly)
    return any(_dist_point_segment(p, poly[i], poly[(i + 1) % n]) <= margin for i in range(n))


def _length(strokes: Sequence[Sequence[XY]]) -> float:
    return sum(
        math.hypot(b[0] - a[0], b[1] - a[1])
        for st in strokes
        for a, b in zip(st, st[1:], strict=False)
    )


def _resample(stroke: Sequence[XY], step: float) -> list[tuple[XY, float]]:
    """Sample points along a polyline: (midpoint, ink length it stands for)."""
    out: list[tuple[XY, float]] = []
    for a, b in zip(stroke, stroke[1:], strict=False):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg == 0.0:
            continue
        n = max(1, math.ceil(seg / step))
        for k in range(n):
            t = (k + 0.5) / n
            out.append(((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])), seg / n))
    return out


def _rdp(points: Sequence[XY], eps: float) -> list[XY]:
    """Ramer-Douglas-Peucker simplification (iterative)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        best, idx = -1.0, -1
        for i in range(lo + 1, hi):
            d = _dist_point_segment(points[i], points[lo], points[hi])
            if d > best:
                best, idx = d, i
        if best > eps and idx > 0:
            keep[idx] = True
            stack.append((lo, idx))
            stack.append((idx, hi))
    return [p for p, k in zip(points, keep, strict=True) if k]


def _margin(value: float, threshold: float, clear_pass: float, clear_fail: float) -> float:
    """0 at the threshold, 1 at the clear value on the side `value` is on.

    Works for "higher is better" (clear_pass > threshold > clear_fail) and "lower
    is better" (clear_pass < threshold < clear_fail) criteria."""
    higher_better = clear_pass >= threshold
    passed = value >= threshold if higher_better else value <= threshold
    edge = clear_pass if passed else clear_fail
    span = abs(edge - threshold)
    if span == 0.0:
        return 1.0
    return max(0.0, min(1.0, abs(value - threshold) / span))


def _passes(value: float, threshold: float, clear_pass: float) -> bool:
    return value >= threshold if clear_pass >= threshold else value <= threshold


def _combine(criteria: list[tuple[bool, float]]) -> tuple[bool, float]:
    """(passed, margin) per criterion -> (correct, confidence)."""
    if all(ok for ok, _ in criteria):
        return True, min(m for _, m in criteria)
    return False, max(m for ok, m in criteria if not ok)


# --------------------------------------------------------------------------- #
# Region mode.
# --------------------------------------------------------------------------- #
def evaluate_region(
    target: DrawingRegionTarget, w: float, h: float, strokes: list[list[XY]]
) -> DrawingVerdict:
    polys = [_zone_polygon(z) for z in target.zones]
    total = 0.0
    inside = 0.0
    per_zone = [0.0] * len(polys)
    # Lengths in picture widths: both axes divided by W keeps the aspect ratio.
    scaled = [[(x / w, y / w) for x, y in stroke] for stroke in strokes]
    step = max(_REGION_STEP, _length(scaled) / MAX_SAMPLES)
    for lengths in scaled:
        for pt, ink in _resample(lengths, step):
            uv = (pt[0], pt[1] * w / h)  # back to normalized picture coords
            total += ink
            hit = False
            for k, poly in enumerate(polys):
                if _near_zone(uv, poly, target.margin):
                    per_zone[k] += ink
                    hit = True
            if hit:
                inside += ink
    if total == 0.0:
        return DrawingVerdict(False, 1.0, "пусто: ничего не нарисовано")
    ratio = inside / total
    t = target.min_inside_ratio
    criteria = [
        (
            total >= target.min_ink,
            _margin(
                total,
                target.min_ink,
                target.min_ink * _INK_CLEAR_PASS,
                target.min_ink * _INK_CLEAR_FAIL,
            ),
        ),
        (
            ratio >= t,
            _margin(ratio, t, min(1.0, t + _INSIDE_CLEAR_PASS), max(0.0, t - _INSIDE_CLEAR_FAIL)),
        ),
    ]
    if target.require_all_zones and len(polys) > 1:
        need = target.min_ink / len(polys)
        worst = min(per_zone)
        criteria.append(
            (worst >= need, _margin(worst, need, need * _INK_CLEAR_PASS, need * _INK_CLEAR_FAIL))
        )
    correct, conf = _combine(criteria)
    detail = (
        f"область: штрихов {total:.2f} (нужно ≥ {target.min_ink:g}), "
        f"в зоне {ratio:.0%} (нужно ≥ {t:.0%})"
    )
    if target.require_all_zones and len(polys) > 1:
        detail += f", в каждой зоне ≥ {min(per_zone):.2f}"
    return DrawingVerdict(correct, conf, detail)


# --------------------------------------------------------------------------- #
# Lines mode.
# --------------------------------------------------------------------------- #
Transform = Callable[[XY], XY]
TRANSFORMS: tuple[tuple[str, Transform], ...] = (
    ("без поворота", lambda p: (p[0], p[1])),
    ("поворот 90°", lambda p: (1 - p[1], p[0])),
    ("поворот 180°", lambda p: (1 - p[0], 1 - p[1])),
    ("поворот 270°", lambda p: (p[1], 1 - p[0])),
    ("отражение ↔", lambda p: (1 - p[0], p[1])),
    ("отражение ↕", lambda p: (p[0], 1 - p[1])),
    ("отражение по диагонали", lambda p: (p[1], p[0])),
    ("отражение по другой диагонали", lambda p: (1 - p[1], 1 - p[0])),
)


def _border_distance(p: XY) -> float:
    s, t = p
    if 0.0 <= s <= 1.0 and 0.0 <= t <= 1.0:
        return min(s, 1 - s, t, 1 - t)
    dx = max(0.0 - s, 0.0, s - 1.0)
    dy = max(0.0 - t, 0.0, t - 1.0)
    return math.hypot(dx, dy)


def evaluate_lines(
    target: DrawingLinesTarget, w: float, h: float, strokes: list[list[XY]]
) -> DrawingVerdict:
    sq = target.square
    tol = target.tolerance
    local = [
        _rdp([((x / w - sq.x) / sq.w, (y / h - sq.y) / sq.h) for x, y in stroke], tol / 4)
        for stroke in strokes
    ]
    ink_segments = [(a, b) for st in local for a, b in zip(st, st[1:], strict=False) if a != b]
    if not ink_segments:
        return DrawingVerdict(False, 1.0, "пусто: ничего не нарисовано")
    if len(ink_segments) > MAX_INK_SEGMENTS:
        return DrawingVerdict(False, 0.0, f"слишком много штрихов ({len(ink_segments)} отрезков)")
    step = max(tol / 4, _length(local) / MAX_SAMPLES)
    samples = [s for st in local for s in _resample(st, step)]
    if target.ignore_border:
        samples = [(p, ink) for p, ink in samples if _border_distance(p) > tol / 2]
    ink_total = sum(ink for _, ink in samples)

    candidates = [TRANSFORMS[0]] if not target.allow_transforms else list(TRANSFORMS)
    best: tuple[tuple[int, float], str, float, float, bool, float] | None = None
    for label, fn in candidates:
        refs = [(fn(seg.a), fn(seg.b)) for seg in target.segments]
        coverage = 1.0
        for a, b in refs:
            pts = [
                (
                    a[0] + (k + 0.5) / _REF_SAMPLES * (b[0] - a[0]),
                    a[1] + (k + 0.5) / _REF_SAMPLES * (b[1] - a[1]),
                )
                for k in range(_REF_SAMPLES)
            ]
            near = sum(
                1
                for p in pts
                if any(_dist_point_segment(p, ia, ib) <= tol for ia, ib in ink_segments)
            )
            coverage = min(coverage, near / _REF_SAMPLES)
        stray = sum(
            ink for p, ink in samples if all(_dist_point_segment(p, a, b) > tol for a, b in refs)
        )
        unexplained = stray / ink_total if ink_total > 0 else 0.0
        correct, conf = _combine(
            [
                (
                    _passes(coverage, COVERAGE_PASS, COVERAGE_CLEAR_PASS),
                    _margin(coverage, COVERAGE_PASS, COVERAGE_CLEAR_PASS, COVERAGE_CLEAR_FAIL),
                ),
                (
                    _passes(unexplained, UNEXPLAINED_PASS, UNEXPLAINED_CLEAR_PASS),
                    _margin(
                        unexplained,
                        UNEXPLAINED_PASS,
                        UNEXPLAINED_CLEAR_PASS,
                        UNEXPLAINED_CLEAR_FAIL,
                    ),
                ),
            ]
        )
        # Prefer a passing symmetry with the highest confidence; else the failing
        # one closest to passing (lowest failure confidence).
        rank = (1, conf) if correct else (0, -conf)
        if best is None or rank > best[0]:
            best = (rank, label, coverage, unexplained, correct, conf)
    assert best is not None
    _, label, coverage, unexplained, correct, conf = best
    detail = (
        f"линии ({label}): покрытие {coverage:.0%} (нужно ≥ {COVERAGE_PASS:.0%}), "
        f"лишние штрихи {unexplained:.0%} (допустимо ≤ {UNEXPLAINED_PASS:.0%})"
    )
    return DrawingVerdict(correct, conf, detail)


# --------------------------------------------------------------------------- #
# Entry points.
# --------------------------------------------------------------------------- #
def evaluate_drawing(
    target: DrawingRegionTarget | DrawingLinesTarget, response: str | None
) -> DrawingVerdict:
    """The deterministic verdict for one drawing (never raises on a bad payload)."""
    try:
        w, h, strokes = parse_drawing(response)
    except DrawingPayloadError as exc:
        return DrawingVerdict(False, 0.0, f"рисунок не читается ({exc})", invalid=True)
    if isinstance(target, DrawingRegionTarget):
        return evaluate_region(target, w, h, strokes)
    return evaluate_lines(target, w, h, strokes)


def drawing_points(item: DrawingItem) -> float:
    """What a drawing item is worth: its `points`, else 1."""
    return float(item.points) if item.points is not None else DRAWING_DEFAULT_POINTS


def grade_drawing(item: DrawingItem, response: str | None) -> ItemGrade:
    """All-or-nothing; a low-confidence (or unreadable) drawing -> needs_review."""
    verdict = evaluate_drawing(item.target, response)
    points = drawing_points(item)
    review = verdict.invalid or verdict.confidence < REVIEW_BELOW
    feedback = f"{verdict.detail}; уверенность {verdict.confidence:.2f}"
    if review:
        feedback += " — проверьте вручную"
    log.info(
        "grade drawing item=%s mode=%s correct=%s confidence=%.2f review=%s",
        item.id,
        item.mode,
        verdict.correct,
        verdict.confidence,
        review,
    )
    if verdict.invalid:
        log.warning("grade drawing item=%s unreadable payload -> review", item.id)
    return ItemGrade(
        item_id=item.id,
        awarded=points if verdict.correct else 0.0,
        max_points=points,
        method="drawing",
        needs_review=review,
        feedback=feedback,
    )
