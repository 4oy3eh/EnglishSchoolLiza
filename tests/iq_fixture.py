"""A SYNTHETIC, tiny IQ test for the Phase 17 tests (no licensed content).

Three subtests (all invented here): "pairs" (mark_toggle, 3 rows to mark of 6),
"drawing" (one region + one lines drawing) and "memory" (a study text + two
single_choice recall items). Each intro carries invented practice problems of
every kind (mark / drawing / text + choice) for the step-by-step intro.
Composite I = pairs + memory, II = drawing. The norm tables are made-up numbers
with the shapes the book uses (an open "140+" top cell, an open "<70" bottom cell).

Also: helpers that turn normalized coordinates into the runner's stroke payload.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# Canvas of the synthetic drawings (pixels) — deliberately not square.
W, H = 400.0, 300.0
# Where the "divide the square" square sits on the picture (normalized).
SQUARE = {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}
# The reference figure: an "L" from the middle of the left side to the centre,
# then down to the middle of the bottom side (asymmetric, so rotations matter).
L_SEGMENTS = [((0.0, 0.5), (0.5, 0.5)), ((0.5, 0.5), (0.5, 1.0))]
ZONE = {"kind": "rect", "x": 0.6, "y": 0.6, "w": 0.3, "h": 0.3}

# mark_toggle rows: True = should be marked.
PAIR_ROWS = [("r1", True), ("r2", False), ("r3", True), ("r4", False), ("r5", True), ("r6", False)]


def picture_payload(strokes_uv: Sequence[Sequence[tuple[float, float]]]) -> str:
    """Strokes in normalized PICTURE coordinates -> the saved JSON (canvas px)."""
    return json.dumps(
        {"w": W, "h": H, "strokes": [[[u * W, v * H] for u, v in st] for st in strokes_uv]}
    )


def square_payload(strokes_st: Sequence[Sequence[tuple[float, float]]]) -> str:
    """Strokes in SQUARE-local coordinates -> the saved JSON (canvas px)."""
    sq = SQUARE
    return picture_payload(
        [[(sq["x"] + s * sq["w"], sq["y"] + t * sq["h"]) for s, t in st] for st in strokes_st]
    )


def norm_scoring_json(prefix: str) -> dict[str, Any]:
    s1, s2, s3 = f"{prefix}-s1", f"{prefix}-s2", f"{prefix}-s3"
    rows: list[dict[str, Any]] = []
    for total in range(3, 21):
        teen: int | str = 60 + 4 * total
        adult: int | str = 55 + 4 * total
        senior: int | str | None = 65 + 4 * total
        if total == 20:
            teen = "140+"
        if total == 3:
            adult = "<70"
            senior = None
        rows.append({"sum": total, "values": {"teen": teen, "adult": adult, "senior": senior}})
    return {
        "composites": [
            {"key": "I", "label": "Composite I", "section_ids": [s1, s3]},
            {"key": "II", "label": "Composite II", "section_ids": [s2]},
        ],
        "scaled_tables": {
            s1: [
                {"raw_min": 0, "raw_max": 0, "scaled": 1},
                {"raw_min": 1, "raw_max": 1, "scaled": 4},
                {"raw_min": 2, "raw_max": 2, "scaled": 7},
                {"raw_min": 3, "raw_max": 3, "scaled": 10},
            ],
            s2: [
                {"raw_min": 0, "raw_max": 0, "scaled": 3},
                {"raw_min": 1, "raw_max": 1, "scaled": 8},
                {"raw_min": 2, "raw_max": 2, "scaled": 12},
            ],
            s3: [
                {"raw_min": 0, "raw_max": 0, "scaled": 2},
                {"raw_min": 1, "raw_max": 1, "scaled": 6},
                {"raw_min": 2, "raw_max": 2, "scaled": 10},
            ],
        },
        "by_age": {
            "age_groups": [
                {"key": "teen", "min_age": 14, "max_age": 19},
                {"key": "adult", "min_age": 20, "max_age": 59},
                {"key": "senior", "min_age": 60, "max_age": None},
            ],
            "rows": rows,
        },
        "final": "mean_of_composites",
        "brackets": [
            {"label": "Below average", "min": 0},
            {"label": "Average", "min": 90},
            {"label": "Above average", "min": 110},
            {"label": "Superior", "min": 130},
        ],
        "min_age": 14,
        "note": "Synthetic norms for tests only.",
    }


def _opts(a: str, b: str) -> list[dict[str, str]]:
    return [{"kind": "text", "key": "A", "text": a}, {"kind": "text", "key": "B", "text": b}]


def iq_test_json(test_id: str = "iq-demo") -> dict[str, Any]:
    """The authoring JSON of the synthetic IQ test (what `import_test` takes)."""
    blank = {"kind": "passage_text", "text": ""}
    return {
        "id": test_id,
        "title": "Synthetic IQ",
        "level": "IQ",
        "duration_minutes": 10,
        "sections": [
            {
                "id": f"{test_id}-s1",
                "title": "Pairs",
                "stimulus": blank,
                "time_limit_seconds": 60,
                "intro": {
                    "text": "Put an X on every pair that is NOT identical.",
                    "samples": [{"text": "1234 — 1243  (X)"}],
                    "practice": [
                        {
                            "kind": "mark",
                            "prompt": "5678 ....... 5687",
                            "solution": "1",
                            "explanation": "The last two digits are swapped.",
                        },
                        {
                            "kind": "mark",
                            "prompt": "4321 ....... 4321",
                            "solution": "0",
                            "explanation": "The pair is identical.",
                        },
                    ],
                },
                "items": [
                    {
                        "item_type": "mark_toggle",
                        "id": f"{test_id}-{rid}",
                        "prompt": f"pair {rid}",
                        "correct": ok,
                    }
                    for rid, ok in PAIR_ROWS
                ],
            },
            {
                "id": f"{test_id}-s2",
                "title": "Drawing",
                "stimulus": blank,
                "time_limit_seconds": 120,
                "intro": {
                    "text": "Draw.",
                    "images": ["iq-sample.png"],
                    "practice": [
                        {
                            "kind": "drawing",
                            "prompt": "Try: draw the missing line.",
                            "images": ["iq-practice.png"],
                            "solution_images": ["iq-practice-answer.png"],
                            "explanation": "One line across the middle.",
                        }
                    ],
                },
                "items": [
                    {
                        "item_type": "drawing",
                        "id": f"{test_id}-d1",
                        "prompt": "Draw what is missing.",
                        "asset_id": "iq-pic.png",
                        "mode": "region",
                        "target": {"mode": "region", "zones": [dict(ZONE)], "min_ink": 0.2},
                    },
                    {
                        "item_type": "drawing",
                        "id": f"{test_id}-d2",
                        "prompt": "Divide the square.",
                        "asset_id": "iq-square.png",
                        "mode": "lines",
                        "target": {
                            "mode": "lines",
                            "square": dict(SQUARE),
                            "segments": [{"a": a, "b": b} for a, b in L_SEGMENTS],
                            "tolerance": 0.08,
                        },
                    },
                ],
            },
            {
                "id": f"{test_id}-s3",
                "title": "Memory",
                "stimulus": blank,
                "time_limit_seconds": 90,
                "study": {"text": "cat dog sun", "study_seconds": 20},
                "intro": {
                    "text": "Remember the words, then answer.",
                    "samples": [{"text": "red blue -> first: red"}],
                    "practice": [
                        {
                            "kind": "text",
                            "prompt": "two, four, six, ___",
                            "solution": "8",
                            "explanation": "Add two each time.",
                        },
                        {
                            "kind": "choice",
                            "prompt": "Odd one out?",
                            "options": [
                                {"key": "a", "text": "apple"},
                                {"key": "b", "text": "pear"},
                                {"key": "c", "text": "chair"},
                            ],
                            "solution": "c",
                            "explanation": "A chair is not a fruit.",
                        },
                    ],
                },
                "items": [
                    {
                        "item_type": "single_choice",
                        "id": f"{test_id}-m1",
                        "prompt": "Which animal was first?",
                        "options": _opts("cat", "cow"),
                        "correct": "A",
                        "shuffle_options": False,
                    },
                    {
                        "item_type": "single_choice",
                        "id": f"{test_id}-m2",
                        "prompt": "What came last?",
                        "options": _opts("moon", "sun"),
                        "correct": "B",
                        "shuffle_options": False,
                    },
                ],
            },
        ],
        "norm_scoring": norm_scoring_json(test_id),
    }


ASSETS = {
    "iq-pic.png": PNG,
    "iq-square.png": PNG,
    "iq-sample.png": PNG,
    "iq-practice.png": PNG,
    "iq-practice-answer.png": PNG,
}
