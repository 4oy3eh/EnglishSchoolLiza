"""Norm-referenced scoring (IQ family, Phase 17) — PURE and deterministic.

(per-section raw scores, `NormScoring`, age) -> `IQReport`:

1. **raw -> scaled** per section (`scaled_tables[section_id]`, inclusive bands).
   A fractional raw (a teacher's partial mark) is rounded half-up first. Raw above
   the last band -> the top band's scaled + note `raw_above_scale_table:<id>`;
   below the first band -> the first band's scaled + `raw_below_scale_table:<id>`;
   in a gap -> None + `raw_not_in_scale_table:<id>`; no table -> None +
   `no_scale_table:<id>`; section not graded (not served) -> None +
   `section_not_graded:<id>`.
2. **composites**: the sum of their sections' scaled scores; any section missing
   -> no sum, `incomplete_composite:<key>`.
3. **score by age**: the age group (whole years, inclusive bands, `max_age` None
   = open) picks the column of `by_age.rows`; the row whose `sum` equals the
   composite sum gives the score. A cell may be an int, None (`no_norm_value:<key>`)
   or an OPEN bound: "130+" = at least 130 (open_bound "above"), "<70" = below 70
   (open_bound "below"); the number is used for the arithmetic. A sum above the
   table's last row takes the last row and is open "above" (note `above_table`);
   below the first row takes the first row, open "below" (`below_table`); a sum
   between rows that is not printed -> None + `sum_not_in_table:<key>`.
   No age -> `no_birth_date` + `no_norms_for_age`; age < `min_age` ->
   `under_min_age` + `no_norms_for_age`; no group covers the age ->
   `no_norms_for_age`. Raw and scaled are still reported in every case.
4. **final** = `mean_of_composites` (the book: overall = (I + II) / 2):
   `overall_exact` unrounded, `overall` rounded half-up. When a composite is open
   "above" the true mean is at least that -> `overall_display` "125+"; open
   "below" -> "<75"; both kinds at once -> plain number + `mixed_open_bounds`.
   A composite without a score -> no overall (`overall_incomplete`).
5. **bracket**: the highest `brackets[*].min` <= `overall`.

Advisory and non-clinical; never reads integrity data (golden rule #2).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from app.core.logging import get_logger
from contracts import IQCompositeScore, IQReport, IQSectionScore, NormScoring
from contracts.content import NORM_OPEN_PATTERN, AgeGroup, NormCell, ScaledBand

log = get_logger(__name__)

_OPEN_RE = re.compile(NORM_OPEN_PATTERN)


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def parse_cell(cell: NormCell) -> tuple[int | None, str | None, str | None]:
    """A norm cell -> (number, open_bound, display). "130+" -> (130, "above", "130+")."""
    if cell is None:
        return None, None, None
    if isinstance(cell, int):
        return cell, None, str(cell)
    m = _OPEN_RE.match(cell)
    if m is None:  # the contract's pattern makes this unreachable
        return None, None, None
    if m.group(1) is not None:
        return int(m.group(1)), "above", cell
    return int(m.group(2)), "below", cell


def scaled_for(raw: int, bands: Sequence[ScaledBand]) -> tuple[int | None, str | None]:
    """(scaled, note-kind) for an integer raw score; kind in {None, 'above', 'below', 'gap'}."""
    ordered = sorted(bands, key=lambda b: b.raw_min)
    for band in ordered:
        if band.raw_min <= raw <= band.raw_max:
            return band.scaled, None
    if raw > max(b.raw_max for b in ordered):
        top = max(ordered, key=lambda b: b.raw_max)
        return top.scaled, "above"
    if raw < ordered[0].raw_min:
        return ordered[0].scaled, "below"
    return None, "gap"


def age_group_for(age: int, groups: Sequence[AgeGroup]) -> AgeGroup | None:
    for group in groups:
        if age >= group.min_age and (group.max_age is None or age <= group.max_age):
            return group
    return None


def compute_iq_report(
    raw_by_section: Mapping[str, float | None],
    norms: NormScoring,
    age: int | None,
    *,
    raw_max_by_section: Mapping[str, float] | None = None,
    titles: Mapping[str, str | None] | None = None,
    attempt_id: str | None = None,
    test_id: str | None = None,
) -> IQReport:
    """The structured IQ report (see the module rules). Pure."""
    notes: list[str] = []
    order: list[str] = list(raw_by_section)
    for comp in norms.composites:
        order.extend(sid for sid in comp.section_ids if sid not in order)
    order.extend(sid for sid in norms.scaled_tables if sid not in order)

    sections: list[IQSectionScore] = []
    scaled: dict[str, int | None] = {}
    for sid in order:
        raw = raw_by_section.get(sid)
        value: int | None = None
        if raw is None:
            notes.append(f"section_not_graded:{sid}")
        elif sid not in norms.scaled_tables or not norms.scaled_tables[sid]:
            notes.append(f"no_scale_table:{sid}")
        else:
            value, kind = scaled_for(round_half_up(raw), norms.scaled_tables[sid])
            if kind == "above":
                notes.append(f"raw_above_scale_table:{sid}")
            elif kind == "below":
                notes.append(f"raw_below_scale_table:{sid}")
            elif kind == "gap":
                notes.append(f"raw_not_in_scale_table:{sid}")
        scaled[sid] = value
        sections.append(
            IQSectionScore(
                section_id=sid,
                title=(titles or {}).get(sid),
                raw=raw,
                raw_max=(raw_max_by_section or {}).get(sid),
                scaled=value,
            )
        )

    group: AgeGroup | None = None
    if age is None:
        notes += ["no_birth_date", "no_norms_for_age"]
    elif age < norms.min_age:
        notes += ["under_min_age", "no_norms_for_age"]
    else:
        group = age_group_for(age, norms.by_age.age_groups)
        if group is None:
            notes.append("no_norms_for_age")

    rows = sorted(norms.by_age.rows, key=lambda r: r.sum)
    by_sum = {r.sum: r for r in rows}
    composites: list[IQCompositeScore] = []
    for comp in norms.composites:
        parts = [scaled.get(sid) for sid in comp.section_ids]
        total: int | None = None
        if any(p is None for p in parts):
            notes.append(f"incomplete_composite:{comp.key}")
        else:
            total = sum(p for p in parts if p is not None)
        out = IQCompositeScore(
            key=comp.key, label=comp.label, section_ids=list(comp.section_ids), scaled_sum=total
        )
        if group is not None and total is not None:
            row, forced = by_sum.get(total), None
            if row is None and rows and total > rows[-1].sum:
                row, forced = rows[-1], "above"
                notes.append("above_table")
            elif row is None and rows and total < rows[0].sum:
                row, forced = rows[0], "below"
                notes.append("below_table")
            if row is None:
                notes.append(f"sum_not_in_table:{comp.key}")
            else:
                number, bound, display = parse_cell(row.values.get(group.key))
                if number is None:
                    notes.append(f"no_norm_value:{comp.key}")
                else:
                    bound = bound or forced
                    if forced == "above" and display is not None and not display.endswith("+"):
                        display = f"{number}+"
                    elif forced == "below" and display is not None and not display.startswith("<"):
                        display = f"<{number}"
                    out = out.model_copy(
                        update={"score": number, "score_display": display, "open_bound": bound}
                    )
        composites.append(out)

    overall_exact: float | None = None
    overall: int | None = None
    overall_display: str | None = None
    bracket: str | None = None
    if group is not None:
        scores = [c.score for c in composites]
        if composites and all(s is not None for s in scores):
            overall_exact = sum(s for s in scores if s is not None) / len(scores)
            overall = round_half_up(overall_exact)
            bounds = {c.open_bound for c in composites if c.open_bound}
            if bounds == {"above"}:
                overall_display = f"{overall}+"
            elif bounds == {"below"}:
                overall_display = f"<{overall}"
            else:
                if len(bounds) > 1:
                    notes.append("mixed_open_bounds")
                overall_display = str(overall)
            for br in sorted(norms.brackets, key=lambda b: b.min):
                if overall >= br.min:
                    bracket = br.label
        else:
            notes.append("overall_incomplete")

    report = IQReport(
        attempt_id=attempt_id,
        test_id=test_id,
        age=age,
        age_group=group.key if group is not None else None,
        sections=sections,
        composites=composites,
        final=norms.final,
        overall_exact=overall_exact,
        overall=overall,
        overall_display=overall_display,
        bracket=bracket,
        notes=list(dict.fromkeys(notes)),
        note=norms.note,
    )
    log.info(
        "iq report attempt=%s test=%s age_group=%s composites=%s overall=%s notes=%s",
        attempt_id,
        test_id,
        report.age_group,
        [c.score_display for c in composites],
        overall_display,
        report.notes,
    )
    return report
