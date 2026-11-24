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
  (square-local [0, 1] coordinates, distances in square sides), RDP-simplified
  (epsilon = tolerance / 4) and resampled; every ink sample carries the stroke's
  local direction (chord over +/- `tolerance` of ink, smoothing hand wobble). With
  `ignore_border`, ink on the printed border (within tolerance / 2) or outside the
  square is dropped — it divides nothing. Hand drawings are judged like a teacher
  does: the right lines, running the right way, roughly in the right place. For
  each allowed symmetry (identity; with `allow_transforms` also rotations
  90/180/270 and the four reflections) the reference segments are transformed and:
  - each ink sample belongs to the NEAREST segment running its way (within
    `MAX_ANGLE_DEG`) — so a "+" never explains a diagonal and one line cannot
    stand in for two;
  - coverage of a segment = share of its 24 samples (those on the border band
    skipped) that have ITS ink beside them (along the segment within tolerance /
    2, sideways within far = tolerance x `FAR_FACTOR`). A drawn line beside the
    answer's meets the border / the line it joins a little earlier or later: when
    the answer's end is such a joint and the ink stops at one too, the answer's
    part beyond the ink (up to reach) is a displaced end, not a gap;
    criterion = the WORST segment's coverage;
  - offset = the worst segment's 80th-percentile sideways distance of that ink
    (and, for an answer end in a corner, how far the drawn line, prolonged,
    passes that corner); it must be <= reach = tolerance x `REACH_FACTOR`
    (clear pass at tolerance, clear fail at far);
  - unexplained ink = share of the ink farther than reach sideways (or more than
    tolerance past a segment's end) from its segment, ramping to fully stray at
    far; ink with no segment running its way counts fully unless it lies within
    tolerance of a line (a junction, a rounded corner).
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
# A hand-drawn line may sit up to tolerance x REACH_FACTOR beside the answer's and
# still count (confidence falls from 1 at `tolerance` to 0 there); from
# tolerance x FAR_FACTOR on, the ink is clearly somewhere else.
REACH_FACTOR = 2.5
FAR_FACTOR = 4.0
# Ink belongs to a reference segment only when it runs within this angle of it.
MAX_ANGLE_DEG = 30.0
# A segment's offset = this quantile of its samples' sideways offsets.
OFFSET_QUANTILE = 0.8
# An answer end this close to a corner (x tolerance) is that corner.
CORNER_FACTOR = 0.6
# Work caps (an adversarial scribble must not stall the grader): at most this many
# ink samples (the resampling step grows instead) and simplified ink segments
# (beyond -> 0 + needs_review).
MAX_SAMPLES = 20_000
MAX_LINE_SAMPLES = 4_000
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


def _inside_square(p: XY) -> bool:
    return 0.0 <= p[0] <= 1.0 and 0.0 <= p[1] <= 1.0


@dataclass(frozen=True)
class _InkSample:
    p: XY
    ink: float  # the ink length it stands for (square sides)
    d: XY | None  # unit stroke direction around it (None: a dot)


def _ink_samples(
    strokes: Sequence[Sequence[XY]], step: float, half_window: float
) -> list[_InkSample]:
    """Resample every stroke; each sample carries the stroke's local direction —
    the chord over +/- `half_window` of ink around it (smooths hand wobble)."""
    out: list[_InkSample] = []
    for stroke in strokes:
        samples = _resample(stroke, step)
        if not samples:
            continue
        pts = [p for p, _ in samples]
        k = max(1, round(half_window / step))
        n = len(pts)
        for i, (p, ink) in enumerate(samples):
            a, b = pts[max(0, i - k)], pts[min(n - 1, i + k)]
            if a == b:  # a one-sample stroke: its own end-to-end direction
                a, b = stroke[0], stroke[-1]
            dx, dy = b[0] - a[0], b[1] - a[1]
            norm = math.hypot(dx, dy)
            out.append(_InkSample(p, ink, (dx / norm, dy / norm) if norm > 0 else None))
    return out


@dataclass(frozen=True)
class _LinesFit:
    coverage: float  # worst reference segment
    offset: float  # worst segment's sideways offset / corner miss (square sides)
    unexplained: float  # share of the ink


def _fit_lines(
    refs: Sequence[tuple[XY, XY]], ink: Sequence[_InkSample], tol: float, ignore_border: bool
) -> _LinesFit:
    """How well the ink reproduces `refs` (already transformed); see the module doc."""
    reach, far = tol * REACH_FACTOR, tol * FAR_FACTOR
    cos_max = math.cos(math.radians(MAX_ANGLE_DEG))
    geo: list[tuple[XY, XY, XY, float]] = []
    for a, b in refs:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        u = (dx / length, dy / length) if length > 0 else (1.0, 0.0)
        geo.append((a, b, u, length))
    slack = tol / 2  # along a segment: the ink must really be there
    # best[j][k] = the smallest sideways offset of ink beside sample k of segment j
    best = [[math.inf] * _REF_SAMPLES for _ in refs]
    owners: list[int] = []  # per ink sample: the segment it belongs to (-1: none)
    # the owned ink's extreme foot points along each segment: (along, ink index)
    lo_end: list[tuple[float, int] | None] = [None] * len(refs)
    hi_end: list[tuple[float, int] | None] = [None] * len(refs)
    stray = 0.0
    total = 0.0
    for i, s in enumerate(ink):
        dists = [_dist_point_segment(s.p, a, b) for a, b, _, _ in geo]
        # The ink belongs to the nearest segment running its way (one segment per ink).
        owner, owner_d = -1, math.inf
        if s.d is not None:
            for j, (_, _, u, _) in enumerate(geo):
                if abs(s.d[0] * u[0] + s.d[1] * u[1]) >= cos_max and dists[j] < owner_d:
                    owner, owner_d = j, dists[j]
        if owner_d > far:
            owner = -1
        owners.append(owner)
        total += s.ink
        stray_d = math.inf
        for j, (a, _, u, length) in enumerate(geo):
            if length == 0.0 or not (j == owner or dists[j] <= tol):
                continue
            rx, ry = s.p[0] - a[0], s.p[1] - a[1]
            along = rx * u[0] + ry * u[1]
            side = abs(rx * u[1] - ry * u[0])
            if j == owner:
                # Sideways the ink may stray `reach`; past the segment's end only
                # `tolerance` (a "+" is not a "T").
                stray_d = max(side, max(0.0, -along, along - length) * REACH_FACTOR)
                lo, hi = lo_end[j], hi_end[j]
                if lo is None or along < lo[0]:
                    lo_end[j] = (along, i)
                if hi is None or along > hi[0]:
                    hi_end[j] = (along, i)
            # reference samples within `slack` of the ink's foot point
            k_lo = math.ceil((along - slack) / length * _REF_SAMPLES - 0.5)
            k_hi = math.floor((along + slack) / length * _REF_SAMPLES - 0.5)
            row = best[j]
            for k in range(max(0, k_lo), min(_REF_SAMPLES - 1, k_hi) + 1):
                row[k] = min(row[k], side)
        if min(dists) > tol:  # right on a line (a corner, a junction) is never stray
            stray += s.ink * max(0.0, min(1.0, (stray_d - reach) / (far - reach)))

    def joint(p: XY, j: int) -> bool:
        """An answer end on the border or on another segment (a junction)."""
        return _border_distance(p) <= tol or any(
            _dist_point_segment(p, a, b) <= tol for m, (a, b, _, _) in enumerate(geo) if m != j
        )

    def ink_stops_at_joint(i: int, j: int) -> bool:
        """The ink of segment j ends on the border or on another drawn line."""
        p = ink[i].p
        return _border_distance(p) <= tol or any(
            owners[m] not in (-1, j) and math.hypot(q.p[0] - p[0], q.p[1] - p[1]) <= tol
            for m, q in enumerate(ink)
        )

    coverage, offset = 1.0, 0.0
    for j, (a, b, _, length) in enumerate(geo):
        # A line drawn a little beside the answer's meets the border (or the line
        # it joins) a little earlier or later. When the answer's end is such a
        # joint and the drawn line also stops at one, the answer's part beyond the
        # ink — up to `reach` — is not missing, it is just the displaced end.
        lo, hi = lo_end[j], hi_end[j]
        free_a = (
            (lo[0] - reach, lo[0]) if lo and joint(a, j) and ink_stops_at_joint(lo[1], j) else None
        )
        free_b = (
            (hi[0], hi[0] + reach) if hi and joint(b, j) and ink_stops_at_joint(hi[1], j) else None
        )
        counted: list[float] = []
        for k in range(_REF_SAMPLES):
            t = (k + 0.5) / _REF_SAMPLES
            p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            # the printed border hides the last bit of a line that meets it
            if ignore_border and _border_distance(p) <= tol / 2:
                continue
            if best[j][k] > far and any(
                f is not None and f[0] <= t * length <= f[1] for f in (free_a, free_b)
            ):
                continue
            counted.append(best[j][k])
        if counted:
            covered = sorted(d for d in counted if d <= far)
            coverage = min(coverage, len(covered) / len(counted))
            if covered:
                offset = max(
                    offset, covered[min(len(covered) - 1, int(OFFSET_QUANTILE * len(covered)))]
                )
        # An answer line that ends in a corner must head into that corner in the
        # drawing too (else the pieces get other corners): the drawn line's end,
        # prolonged, must pass the corner within the offset allowance.
        for end in (a, b):
            corner = (float(round(end[0])), float(round(end[1])))
            if math.hypot(end[0] - corner[0], end[1] - corner[1]) > tol * CORNER_FACTOR:
                continue
            own = [q for m, q in enumerate(ink) if owners[m] == j]
            if own:
                q = min(own, key=lambda q: math.hypot(q.p[0] - corner[0], q.p[1] - corner[1]))
                rx, ry = corner[0] - q.p[0], corner[1] - q.p[1]
                miss = abs(rx * q.d[1] - ry * q.d[0]) if q.d is not None else math.hypot(rx, ry)
                offset = max(offset, miss)
    return _LinesFit(coverage, offset, stray / total if total > 0 else 0.0)


def evaluate_lines(
    target: DrawingLinesTarget, w: float, h: float, strokes: list[list[XY]]
) -> DrawingVerdict:
    sq = target.square
    tol = target.tolerance
    reach, far = tol * REACH_FACTOR, tol * FAR_FACTOR
    local = [
        _rdp([((x / w - sq.x) / sq.w, (y / h - sq.y) / sq.h) for x, y in stroke], tol / 4)
        for stroke in strokes
    ]
    n_segments = sum(1 for st in local for a, b in zip(st, st[1:], strict=False) if a != b)
    if not n_segments:
        return DrawingVerdict(False, 1.0, "пусто: ничего не нарисовано")
    if n_segments > MAX_INK_SEGMENTS:
        return DrawingVerdict(False, 0.0, f"слишком много штрихов ({n_segments} отрезков)")
    step = max(tol / 4, _length(local) / MAX_LINE_SAMPLES)
    ink = _ink_samples(local, step, tol)
    if target.ignore_border:
        # The square's printed border (and anything beyond it) is not a dividing line.
        ink = [s for s in ink if _inside_square(s.p) and _border_distance(s.p) > tol / 2]
    if not ink:
        return DrawingVerdict(False, 1.0, "внутри квадрата ничего не нарисовано")

    candidates = [TRANSFORMS[0]] if not target.allow_transforms else list(TRANSFORMS)
    best: tuple[tuple[int, float], str, _LinesFit, bool, float] | None = None
    for label, fn in candidates:
        refs = [(fn(seg.a), fn(seg.b)) for seg in target.segments]
        fit = _fit_lines(refs, ink, tol, target.ignore_border)
        correct, conf = _combine(
            [
                (
                    _passes(fit.coverage, COVERAGE_PASS, COVERAGE_CLEAR_PASS),
                    _margin(fit.coverage, COVERAGE_PASS, COVERAGE_CLEAR_PASS, COVERAGE_CLEAR_FAIL),
                ),
                (_passes(fit.offset, reach, tol), _margin(fit.offset, reach, tol, far)),
                (
                    _passes(fit.unexplained, UNEXPLAINED_PASS, UNEXPLAINED_CLEAR_PASS),
                    _margin(
                        fit.unexplained,
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
            best = (rank, label, fit, correct, conf)
    assert best is not None
    _, label, fit, correct, conf = best
    detail = (
        f"линии ({label}): покрытие {fit.coverage:.0%} (нужно ≥ {COVERAGE_PASS:.0%}), "
        f"смещение {fit.offset:.2f} стороны (допустимо ≤ {reach:.2f}), "
        f"лишние штрихи {fit.unexplained:.0%} (допустимо ≤ {UNEXPLAINED_PASS:.0%})"
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
