"""Cambridge papers: which paper a section belongs to + per-paper minutes (pure).

A test may be taken WHOLE (default) or the teacher may assign a student only some
of its papers (`RosterEntry.papers`, snapshotted on `Attempt.papers` at start).
Everything here is a pure function of the authoring `Test`, shared by delivery
(what to serve, the deadline), grading / results / admin (what to score and
report) — so all of them agree on "the served test".

Paper of a section = f(level, section.skill) — never authored per section:

| level                      | reading (+UoE)            | writing           | listening |
|----------------------------|---------------------------|-------------------|-----------|
| YLE (Starters/Movers/Flyers), A2 Key | Reading & Writing | Reading & Writing | Listening |
| B1 Preliminary             | Reading                   | Writing           | Listening |
| B2 First, C1, C2           | Reading & Use of English  | Writing           | Listening |

A section without a skill counts as reading (the runner treats it the same way).

Minutes: `Test.paper_minutes` when set (the editor guarantees it covers every paper
and sums to `duration_minutes`); otherwise `duration_minutes` is split across the
test's papers proportionally to the official Cambridge per-paper times
(`OFFICIAL_PAPER_MINUTES`, largest-remainder rounding), so the parts ALWAYS sum to
the full duration. The whole test is always `duration_minutes`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import get_args

from contracts.content import Paper, Section, Test

ALL_PAPERS: tuple[str, ...] = get_args(Paper)

# Cambridge's own paper names (the runner and the teacher UI show them in English,
# the way the exam names them: "Только: Listening").
PAPER_LABELS: dict[str, str] = {
    "reading_writing": "Reading & Writing",
    "reading": "Reading",
    "reading_use_of_english": "Reading & Use of English",
    "writing": "Writing",
    "listening": "Listening",
}

_YLE_A2 = ("PRE_A1_STARTERS", "A1_MOVERS", "A2_FLYERS", "A2_KEY")
_FIVE = ("B2_FIRST", "C1_ADVANCED", "C2_PROFICIENCY")

# Official per-paper times (minutes) from the Cambridge handbooks / information
# for candidates. Listening includes the transfer time where the paper-based exam
# has one. YLE Listening has no fixed paper time — the recordings set the pace —
# so the official approximate durations are used (Starters ~20, Movers / Flyers
# ~25); Reading & Writing is 20 / 30 / 40.
OFFICIAL_PAPER_MINUTES: dict[str, dict[str, int]] = {
    "PRE_A1_STARTERS": {"listening": 20, "reading_writing": 20},
    "A1_MOVERS": {"listening": 25, "reading_writing": 30},
    "A2_FLYERS": {"listening": 25, "reading_writing": 40},
    "A2_KEY": {"reading_writing": 60, "listening": 35},
    "B1_PRELIMINARY": {"reading": 45, "writing": 45, "listening": 36},
    "B2_FIRST": {"reading_use_of_english": 75, "writing": 80, "listening": 40},
    "C1_ADVANCED": {"reading_use_of_english": 90, "writing": 90, "listening": 40},
    "C2_PROFICIENCY": {"reading_use_of_english": 90, "writing": 90, "listening": 40},
}


class PaperSelectionError(ValueError):
    """An assignment names no paper, or a paper the test does not have (Russian)."""


def paper_for(level: str, skill: str | None) -> str:
    """The paper a section of `skill` is sat in at `level` (see the module table)."""
    if skill == "listening":
        return "listening"
    if level in _YLE_A2:
        return "reading_writing"
    if skill == "writing":
        return "writing"
    if level in _FIVE:
        return "reading_use_of_english"
    return "reading"


def section_paper(level: str, section: Section) -> str:
    """The paper `section` belongs to (by its PAPER `skill`, not `reported_skill`:
    B2-C2 Use of English parts are in the Reading & Use of English paper)."""
    return paper_for(level, section.skill)


def papers_of(test: Test) -> list[str]:
    """The test's papers in order of first appearance."""
    out: list[str] = []
    for section in test.sections:
        paper = section_paper(test.level, section)
        if paper not in out:
            out.append(paper)
    return out


def split_minutes(total: int, weights: Sequence[int]) -> list[int]:
    """Split `total` whole minutes proportionally to `weights`; the parts sum to
    `total` exactly (largest remainder; ties -> the earlier part). Every part gets
    at least 1 minute when `total >= len(weights)` (taken from the largest part)."""
    n = len(weights)
    if n == 0:
        return []
    wsum = sum(max(0, w) for w in weights)
    if wsum <= 0:
        weights, wsum = [1] * n, n
    exact = [total * max(0, w) / wsum for w in weights]
    parts = [int(x) for x in exact]
    rest = total - sum(parts)
    order = sorted(range(n), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in order[:rest]:
        parts[i] += 1
    if total >= n:
        for i in range(n):
            if parts[i] == 0:
                donor = max(range(n), key=lambda j: (parts[j], -j))
                parts[donor] -= 1
                parts[i] = 1
    return parts


def official_minutes(level: str, paper: str) -> int:
    """The official minutes of one paper at `level` (fallback: 30)."""
    return OFFICIAL_PAPER_MINUTES.get(level, {}).get(paper, 30)


def explicit_minutes_valid(test: Test) -> bool:
    """True when `test.paper_minutes` is set, names exactly the test's papers and
    sums to `duration_minutes` (what the editor enforces)."""
    pm = test.paper_minutes
    if not pm:
        return False
    return set(pm) == set(papers_of(test)) and sum(pm.values()) == test.duration_minutes


def paper_minutes(test: Test) -> dict[str, int]:
    """Minutes per paper (test order). Explicit when valid, else the proportional
    split of `duration_minutes` by the official times. Sums to `duration_minutes`."""
    papers = papers_of(test)
    if explicit_minutes_valid(test):
        assert test.paper_minutes is not None
        return {p: int(test.paper_minutes[p]) for p in papers}  # type: ignore[index]
    parts = split_minutes(test.duration_minutes, [official_minutes(test.level, p) for p in papers])
    return dict(zip(papers, parts, strict=True))


def normalize_papers(test: Test, papers: Iterable[str] | None) -> list[str] | None:
    """Validate a teacher's choice of papers for `test`.

    None -> None (the whole test). Unknown or absent papers, or an empty list ->
    `PaperSelectionError` (Russian). Duplicates dropped, test order kept; a choice
    of EVERY paper is the whole test -> None.
    """
    if papers is None:
        return None
    chosen = list(papers)
    if not chosen:
        raise PaperSelectionError("Выберите хотя бы одну часть теста (или «Все части»).")
    present = papers_of(test)
    unknown = [p for p in chosen if p not in ALL_PAPERS]
    if unknown:
        raise PaperSelectionError(f"Неизвестная часть теста: {', '.join(map(str, unknown))}.")
    absent = [p for p in chosen if p not in present]
    if absent:
        names = ", ".join(PAPER_LABELS[p] for p in dict.fromkeys(absent))
        have = ", ".join(PAPER_LABELS[p] for p in present)
        raise PaperSelectionError(f"В этом тесте нет части «{names}». Есть: {have}.")
    ordered = [p for p in present if p in chosen]
    return None if len(ordered) == len(present) else ordered


def served_papers(test: Test, papers: Iterable[str] | None) -> list[str]:
    """The papers actually served for an assignment (lenient, for reads/delivery):
    the chosen ones still present in the test, in test order; none chosen / none
    left -> every paper of the test."""
    present = papers_of(test)
    if papers is None:
        return present
    wanted = set(papers)
    kept = [p for p in present if p in wanted]
    return kept or present


def is_partial(test: Test, papers: Iterable[str] | None) -> bool:
    """True when the assignment serves fewer papers than the test has."""
    return len(served_papers(test, papers)) < len(papers_of(test))


def served_minutes(test: Test, papers: Iterable[str] | None) -> int:
    """Minutes for an assignment: `duration_minutes` for the whole test, else the
    sum of the chosen papers' minutes (at least 1)."""
    if not is_partial(test, papers):
        return test.duration_minutes
    minutes = paper_minutes(test)
    return max(1, sum(minutes[p] for p in served_papers(test, papers)))


def served_test(test: Test, papers: Iterable[str] | None) -> Test:
    """The test restricted to the assigned papers: only their sections (authored
    order kept — golden rule #7), `duration_minutes` = their minutes. The whole test
    (None / every paper) is returned unchanged."""
    if papers is None or not is_partial(test, papers):
        return test
    keep = set(served_papers(test, papers))
    minutes = paper_minutes(test)
    return test.model_copy(
        update={
            "sections": [s for s in test.sections if section_paper(test.level, s) in keep],
            "duration_minutes": max(1, sum(minutes[p] for p in keep)),
            "paper_minutes": {p: minutes[p] for p in minutes if p in keep},
        }
    )


def papers_label(papers: Iterable[str] | None) -> str:
    """ "Listening + Reading & Writing" (Cambridge names), or "" for None."""
    if papers is None:
        return ""
    return " + ".join(PAPER_LABELS.get(p, p) for p in papers)


def partial_label(test: Test, papers: Iterable[str] | None) -> str | None:
    """«Частично: Listening» for a partial assignment, None for the whole test."""
    if not is_partial(test, papers):
        return None
    return "Частично: " + papers_label(served_papers(test, papers))
