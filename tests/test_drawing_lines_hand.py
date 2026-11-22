"""`lines` drawing grader vs genuinely HAND-DRAWN answers (synthetic, generic).

A finger on a phone wobbles, overshoots the printed border or stops short of it,
and places a line roughly — a teacher still accepts the right lines running the
right way in roughly the right place. Wrong figures must still score 0: a blank
square, a scribble, a "+" for a diagonal, a missing line, a line far off, a line
continued through a junction, a line that misses the corner it should end in.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence

import pytest

import tests.iq_fixture as fx
from app.grading.drawing import REVIEW_BELOW, TRANSFORMS, evaluate_drawing, grade_drawing
from contracts import DrawingItem, DrawingLinesTarget

XY = tuple[float, float]
Seg = tuple[XY, XY]

L_FIG: list[Seg] = [((0.0, 0.5), (0.5, 0.5)), ((0.5, 0.5), (0.5, 1.0))]
# Chiral: an L plus a tick up from its left arm (no rotation maps it onto its mirror).
CHIRAL: list[Seg] = [*L_FIG, ((0.25, 0.5), (0.25, 0.3))]
DIAGONAL: list[Seg] = [((0.0, 0.0), (1.0, 1.0))]
# A diagonal and a parallel cut-off corner (three pieces: 2 triangles + a strip).
PARALLELS: list[Seg] = [((0.0, 1.0), (1.0, 0.0)), ((0.3, 1.0), (1.0, 0.3))]
# A sideways "Y": two arms from the left corners to a junction, a trunk to the right.
Y_FIG: list[Seg] = [((0.0, 0.0), (0.3, 0.5)), ((0.0, 1.0), (0.3, 0.5)), ((0.3, 0.5), (1.0, 0.5))]
# Two slanted lines (one must not stand in for both).
TWO_SLANTS: list[Seg] = [((0.0, 0.1), (0.25, 1.0)), ((0.7, 0.0), (0.95, 1.0))]
T_FIG: list[Seg] = [((0.5, 0.0), (0.5, 1.0)), ((0.5, 0.5), (1.0, 0.5))]


def _target(segs: Sequence[Seg], **kw: object) -> DrawingLinesTarget:
    return DrawingLinesTarget.model_validate(
        {
            "mode": "lines",
            "square": dict(fx.SQUARE),
            "segments": [{"a": a, "b": b} for a, b in segs],
            "tolerance": 0.08,
            **kw,
        }
    )


def _item(segs: Sequence[Seg]) -> DrawingItem:
    return DrawingItem.model_validate(
        {
            "item_type": "drawing",
            "id": "hand-1",
            "prompt": "Divide the square.",
            "asset_id": "sq.png",
            "mode": "lines",
            "target": _target(segs).model_dump(),
        }
    )


def _hand(
    a: XY, b: XY, *, wobble: float = 0.025, waves: float = 2.5, seed: int = 0, n: int = 40
) -> list[XY]:
    """A finger-drawn stroke from a to b: a smooth sideways wave + a little jitter."""
    rnd = random.Random(seed)
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    nx, ny = -dy / length, dx / length
    pts = []
    for k in range(n + 1):
        t = k / n
        off = wobble * math.sin(2 * math.pi * waves * t + seed) + rnd.uniform(-0.006, 0.006)
        pts.append((a[0] + t * dx + off * nx, a[1] + t * dy + off * ny))
    return pts


def _draw(segs: Sequence[Seg], fn: Callable[[XY], XY] = lambda p: p, **kw: object) -> str:
    return fx.square_payload(
        [_hand(fn(a), fn(b), seed=i, **kw) for i, (a, b) in enumerate(segs)]  # type: ignore[arg-type]
    )


def _extend(seg: Seg, before: float, after: float) -> Seg:
    """Lengthen (positive) or shorten (negative) a segment at both ends."""
    (ax, ay), (bx, by) = seg
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length
    return (ax - before * ux, ay - before * uy), (bx + after * ux, by + after * uy)


def _ok(target: DrawingLinesTarget, payload: str) -> None:
    v = evaluate_drawing(target, payload)
    assert v.correct and v.confidence >= REVIEW_BELOW, v.detail


def _wrong(target: DrawingLinesTarget, payload: str) -> None:
    v = evaluate_drawing(target, payload)
    assert not v.correct and v.confidence >= REVIEW_BELOW, v.detail


# --------------------------------------------------------------------------- #
# Accepted: what a hand really draws.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("segs", [L_FIG, DIAGONAL, PARALLELS, Y_FIG, TWO_SLANTS, T_FIG])
def test_wobbly_finger_lines_pass_confidently(segs: list[Seg]) -> None:
    g = grade_drawing(_item(segs), _draw(segs))
    assert g.awarded == 1 and not g.needs_review, g.feedback


def test_overshooting_past_the_border_passes() -> None:
    # Every stroke runs 0.06 beyond the square (over the printed border, outside).
    for segs in (DIAGONAL, TWO_SLANTS, T_FIG):
        _ok(_target(segs), _draw([_extend(s, 0.06, 0.06) for s in segs]))


def test_stopping_short_of_the_border_passes() -> None:
    # The finger stops at the printed border's inner edge (0.06 short of the box).
    for segs in (DIAGONAL, TWO_SLANTS):
        _ok(_target(segs), _draw([_extend(s, -0.06, -0.06) for s in segs]))


def test_a_line_drawn_a_little_beside_the_answer_passes() -> None:
    # The vertical arm drawn 0.1 to the side: same pieces, slightly other sizes.
    _ok(_target(L_FIG), _draw([L_FIG[0], ((0.6, 0.5), (0.6, 1.0))]))


def test_a_parallel_line_displaced_meets_the_border_elsewhere_and_passes() -> None:
    # The corner cut drawn 0.1 closer to the corner (its ends slide along the
    # sides — not a gap at either end).
    _ok(_target(PARALLELS), _draw([PARALLELS[0], ((0.4, 1.0), (1.0, 0.4))]))


def test_a_displaced_junction_passes() -> None:
    # The Y's junction drawn 0.1 further right: arms and trunk still meet.
    j = (0.4, 0.52)
    _ok(_target(Y_FIG), _draw([((0.0, 0.0), j), ((0.0, 1.0), j), (j, (1.0, 0.5))]))


@pytest.mark.parametrize("k", range(1, 8))
def test_rotated_or_reflected_hand_drawing_passes(k: int) -> None:
    label, fn = TRANSFORMS[k]
    v = evaluate_drawing(_target(CHIRAL), _draw(CHIRAL, fn))
    assert v.correct and v.confidence >= REVIEW_BELOW and label in v.detail
    # without transforms only the identity is the answer
    assert not evaluate_drawing(_target(CHIRAL, allow_transforms=False), _draw(CHIRAL, fn)).correct


def test_a_retraced_line_passes() -> None:
    segs = [*TWO_SLANTS, TWO_SLANTS[0]]  # the first line drawn twice
    _ok(_target(TWO_SLANTS), _draw(segs))


# --------------------------------------------------------------------------- #
# Rejected: wrong figures, however carefully drawn.
# --------------------------------------------------------------------------- #
def test_an_empty_square_is_wrong() -> None:
    for payload in ("", fx.square_payload([]), fx.square_payload([[(1.2, 0.1), (1.2, 0.9)]])):
        v = evaluate_drawing(_target(L_FIG), payload)
        assert not v.correct and v.confidence == 1.0


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("segs", [L_FIG, DIAGONAL, PARALLELS, Y_FIG, TWO_SLANTS])
def test_a_random_scribble_is_wrong(segs: list[Seg], seed: int) -> None:
    rnd = random.Random(seed)
    scribble = [[(0.05 + 0.9 * rnd.random(), 0.05 + 0.9 * rnd.random()) for _ in range(60)]]
    _wrong(_target(segs), fx.square_payload(scribble))


def test_a_plus_is_not_a_diagonal() -> None:
    plus: list[Seg] = [((0.5, 0.0), (0.5, 1.0)), ((0.0, 0.5), (1.0, 0.5))]
    _wrong(_target(DIAGONAL), _draw(plus))


def test_a_plus_is_not_a_t() -> None:
    # The T's bar continued through the junction to the other side: wrong (the
    # extra half-line is a fifth of the ink, so the teacher double-checks it).
    v = evaluate_drawing(_target(T_FIG), _draw([T_FIG[0], ((0.0, 0.5), (1.0, 0.5))]))
    assert not v.correct and "лишние" in v.detail


@pytest.mark.parametrize("segs", [PARALLELS, Y_FIG, TWO_SLANTS, T_FIG, CHIRAL])
def test_a_missing_line_is_wrong(segs: list[Seg]) -> None:
    for m in range(len(segs)):
        _wrong(_target(segs), _draw([s for i, s in enumerate(segs) if i != m]))


def test_one_line_between_two_answers_is_wrong() -> None:
    # A single line midway between the two slanted lines explains neither.
    _wrong(_target(TWO_SLANTS), _draw([((0.35, 0.05), (0.6, 0.95))]))


def test_a_line_in_the_wrong_position_is_wrong() -> None:
    _wrong(_target(L_FIG), _draw([L_FIG[0], ((0.85, 0.5), (0.85, 1.0))]))
    # the second slanted line drawn far from where it belongs
    _wrong(_target(TWO_SLANTS), _draw([TWO_SLANTS[0], ((0.35, 0.0), (0.6, 1.0))]))


def test_a_diagonal_that_misses_its_corner_is_wrong() -> None:
    # Into the corner in the answer; the drawing ends 0.4 along the side instead.
    _wrong(_target(DIAGONAL), _draw([((0.4, 0.0), (1.0, 1.0))]))
