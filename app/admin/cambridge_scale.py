"""Approximate Cambridge English Scale estimate for B2 First / C1 Advanced / C2 Proficiency.

Teacher-only, advisory, pure. Source: "A guide to converting practice test scores to
Cambridge English Scale scores" (Cambridge, ENG/36303/V1/NOV23, pp. 5-7): for each
skill it lists the practice-test raw mark at four anchor points of the scale (e.g.
B2 First Reading 37/42 -> 180, 24 -> 160, 16 -> 140, 10 -> 122 = the minimum score
reported). Between anchors the estimate is linear; above the top anchor it runs
linearly up to the paper's full mark -> the exam's highest reported score (B2 190,
C1 210, C2 230); below the lowest anchor nothing is reported ("ниже 122").

A test whose skill maximum differs from the official paper (a shortened mock) is
compared by proportion: raw' = awarded / max * official max. The overall estimate is
the mean of the skills present, rounded (Cambridge averages five skills incl.
Speaking, which this platform does not run — the label says so). The guide itself
warns the tables "should not be used to try to predict precise scores in the live
exam": the dashboard labels this «оценка по шкале Cambridge (примерно)».
"""

from __future__ import annotations

from dataclasses import dataclass

# level -> skill -> (official max raw, [(raw, scale) anchors, high to low])
_ANCHORS: dict[str, dict[str, tuple[int, list[tuple[int, int]]]]] = {
    "B2_FIRST": {
        "reading": (42, [(37, 180), (24, 160), (16, 140), (10, 122)]),
        "use_of_english": (28, [(24, 180), (18, 160), (11, 140), (7, 122)]),
        "writing": (40, [(34, 180), (24, 160), (16, 140), (10, 122)]),
        "listening": (30, [(27, 180), (18, 160), (12, 140), (8, 122)]),
    },
    "C1_ADVANCED": {
        "reading": (50, [(43, 200), (32, 180), (23, 160), (17, 142)]),
        "use_of_english": (28, [(23, 200), (16, 180), (11, 160), (8, 142)]),
        "writing": (40, [(34, 200), (24, 180), (16, 160), (10, 142)]),
        "listening": (30, [(26, 200), (18, 180), (13, 160), (11, 142)]),
    },
    "C2_PROFICIENCY": {
        "reading": (44, [(36, 220), (28, 200), (22, 180), (14, 162)]),
        "use_of_english": (28, [(22, 220), (17, 200), (13, 180), (9, 162)]),
        "writing": (40, [(34, 220), (24, 200), (16, 180), (10, 162)]),
        "listening": (30, [(24, 220), (18, 200), (14, 180), (10, 162)]),
    },
}
# Highest score each exam reports.
_SCALE_MAX = {"B2_FIRST": 190, "C1_ADVANCED": 210, "C2_PROFICIENCY": 230}
# CEFR bands of each exam's scale (lowest score of the band), best first.
_CEFR = {
    "B2_FIRST": [(180, "C1"), (160, "B2"), (140, "B1")],
    "C1_ADVANCED": [(200, "C2"), (180, "C1"), (160, "B2")],
    "C2_PROFICIENCY": [(200, "C2"), (180, "C1")],
}
SKILL_ORDER = ("reading", "use_of_english", "writing", "listening")


@dataclass(frozen=True)
class SkillScale:
    skill: str
    score: float
    max_score: float
    scale: int | None  # None = below the lowest score the exam reports
    cefr: str | None


def has_scale(level: str | None) -> bool:
    return level in _ANCHORS


def min_reported(level: str) -> int:
    return _ANCHORS[level]["reading"][1][-1][1]


def cefr_for(level: str, scale: int | None) -> str | None:
    if scale is None:
        return None
    for lowest, cefr in _CEFR[level]:
        if scale >= lowest:
            return cefr
    return None


def to_scale(level: str, skill: str, score: float, max_score: float) -> int | None:
    """The approximate scale score of `score / max_score` in one skill (None below
    the exam's lowest reported score; ValueError for an unknown level / skill)."""
    table = _ANCHORS.get(level, {}).get(skill)
    if table is None:
        raise ValueError(f"no Cambridge scale table for level={level!r} skill={skill!r}")
    official, anchors = table
    if max_score <= 0:
        return None
    raw = max(0.0, min(score / max_score, 1.0)) * official
    points = [(float(official), float(_SCALE_MAX[level]))] + [
        (float(r), float(s)) for r, s in anchors
    ]
    lowest_raw = points[-1][0]
    if raw < lowest_raw:
        return None
    for (hi_raw, hi_scale), (lo_raw, lo_scale) in zip(points, points[1:], strict=False):
        if lo_raw <= raw <= hi_raw:
            if hi_raw == lo_raw:
                return round(hi_scale)
            frac = (raw - lo_raw) / (hi_raw - lo_raw)
            return round(lo_scale + frac * (hi_scale - lo_scale))
    return round(points[0][1])  # pragma: no cover - raw is clamped to the full mark


def estimate(
    level: str, per_skill: dict[str, tuple[float, float]]
) -> tuple[list[SkillScale], int | None, str | None]:
    """Per-skill estimates (skills present, in Cambridge order) + overall + its CEFR.

    The overall is the rounded mean over the skills that reach a reported score;
    None when none does.
    """
    rows: list[SkillScale] = []
    for skill in SKILL_ORDER:
        got = per_skill.get(skill)
        if got is None or got[1] <= 0:
            continue
        scale = to_scale(level, skill, got[0], got[1])
        rows.append(SkillScale(skill, got[0], got[1], scale, cefr_for(level, scale)))
    reported = [r.scale for r in rows if r.scale is not None]
    overall = round(sum(reported) / len(reported)) if reported else None
    return rows, overall, cefr_for(level, overall)
