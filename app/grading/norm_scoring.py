"""Norm-referenced scoring (IQ family, Phase 17; raw reports Phase 20) — PURE, deterministic.

(per-section raw scores, `NormScoring`, age) -> `IQReport`:

0. **norms loaded?** `by_age` None (Phase 20: a test supplied without its norm
   tables, e.g. Amthauer's TSI) = a RAW report: raw, % of maximum, raw composite
   sums — no score / overall / bracket, `norms_loaded=False`, note
   `no_norm_tables` (no age notes: nothing is looked up by age).
1. **raw -> scaled** per section (`scaled_tables[section_id]`, inclusive bands; the
   age group's own table in `scaled_tables_by_age` wins when there is one).
   `percent` = raw / raw_max * 100 (one decimal) whenever the maximum is known.
   A fractional raw (a teacher's partial mark) is rounded half-up first. Raw above
   the last band -> the top band's scaled + note `raw_above_scale_table:<id>`;
   below the first band -> the first band's scaled + `raw_below_scale_table:<id>`;
   in a gap -> None + `raw_not_in_scale_table:<id>`; no table -> None +
   `no_scale_table:<id>` (only when the norms have scaled tables at all or a
   `basis="scaled"` composite needs the section); section not graded (not served)
   -> None + `section_not_graded:<id>`.
2. **composites**: the sum of their sections' scaled scores (`basis="scaled"`) or
   raw scores (`basis="raw"`: `raw_sum`, `raw_max`, `percent`; looked up rounded
   half-up); any section missing -> no sum, `incomplete_composite:<key>`.
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
   A composite without a score -> no overall (`overall_incomplete`). Or `final =
   "composite"`: the overall is `final_composite`'s score (IST: the total -> IQ).
5. **bracket**: the highest `brackets[*].min` <= `overall`.

Advisory and non-clinical; never reads integrity data (golden rule #2).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from app.core.logging import get_logger
from contracts import IQCompositeScore, IQReport, IQSectionScore, NormScoring
from contracts.content import NORM_OPEN_PATTERN, AgeGroup, NormCell, NormRow, ScaledBand

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


def percent_of(value: float | None, maximum: float | None) -> float | None:
    """value / maximum * 100, one decimal; None when either is unknown or max is 0."""
    if value is None or not maximum:
        return None
    return round(100.0 * value / maximum, 1)


def _lookup(
    total: int,
    rows: Sequence[NormRow],
    group: AgeGroup,
    key: str,
    notes: list[str],
) -> tuple[int | None, str | None, str | None]:
    """(score, open_bound, display) of a composite sum in the by-age table."""
    by_sum = {r.sum: r for r in rows}
    row, forced = by_sum.get(total), None
    if row is None and rows and total > rows[-1].sum:
        row, forced = rows[-1], "above"
        notes.append("above_table")
    elif row is None and rows and total < rows[0].sum:
        row, forced = rows[0], "below"
        notes.append("below_table")
    if row is None:
        notes.append(f"sum_not_in_table:{key}")
        return None, None, None
    number, bound, display = parse_cell(row.values.get(group.key))
    if number is None:
        notes.append(f"no_norm_value:{key}")
        return None, None, None
    bound = bound or forced
    if forced == "above" and display is not None and not display.endswith("+"):
        display = f"{number}+"
    elif forced == "below" and display is not None and not display.startswith("<"):
        display = f"<{number}"
    return number, bound, display


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
    maxima = raw_max_by_section or {}
    order: list[str] = list(raw_by_section)
    for comp in norms.composites:
        order.extend(sid for sid in comp.section_ids if sid not in order)
    order.extend(sid for sid in norms.scaled_tables if sid not in order)

    loaded = norms.by_age is not None
    group: AgeGroup | None = None
    if not loaded:
        notes.append("no_norm_tables")
    elif age is None:
        notes += ["no_birth_date", "no_norms_for_age"]
    elif age < norms.min_age:
        notes += ["under_min_age", "no_norms_for_age"]
    else:
        assert norms.by_age is not None
        group = age_group_for(age, norms.by_age.age_groups)
        if group is None:
            notes.append("no_norms_for_age")

    tables: dict[str, list[ScaledBand]] = dict(norms.scaled_tables)
    if group is not None:
        tables.update(norms.scaled_tables_by_age.get(group.key, {}))
    any_tables = bool(norms.scaled_tables) or bool(norms.scaled_tables_by_age)
    needs_scaled = {
        sid for c in norms.composites if c.basis == "scaled" for sid in c.section_ids
    }

    section_notes: list[str] = []
    sections: list[IQSectionScore] = []
    scaled: dict[str, int | None] = {}
    for sid in order:
        raw = raw_by_section.get(sid)
        value: int | None = None
        if raw is None:
            section_notes.append(f"section_not_graded:{sid}")
        elif not tables.get(sid):
            if any_tables or sid in needs_scaled:
                section_notes.append(f"no_scale_table:{sid}")
        else:
            value, kind = scaled_for(round_half_up(raw), tables[sid])
            if kind == "above":
                section_notes.append(f"raw_above_scale_table:{sid}")
            elif kind == "below":
                section_notes.append(f"raw_below_scale_table:{sid}")
            elif kind == "gap":
                section_notes.append(f"raw_not_in_scale_table:{sid}")
        scaled[sid] = value
        sections.append(
            IQSectionScore(
                section_id=sid,
                title=(titles or {}).get(sid),
                raw=raw,
                raw_max=maxima.get(sid),
                percent=percent_of(raw, maxima.get(sid)),
                scaled=value,
            )
        )
    # Phase 17 order: section notes first, then the age notes.
    notes = section_notes + notes

    rows = sorted(norms.by_age.rows, key=lambda r: r.sum) if norms.by_age else []
    composites: list[IQCompositeScore] = []
    for comp in norms.composites:
        raws = [raw_by_section.get(sid) for sid in comp.section_ids]
        raw_sum = (
            round(sum(r for r in raws if r is not None), 2)
            if all(r is not None for r in raws)
            else None
        )
        known_max = [maxima[sid] for sid in comp.section_ids if sid in maxima]
        raw_max = round(sum(known_max), 2) if len(known_max) == len(comp.section_ids) else None
        out = IQCompositeScore(
            key=comp.key,
            label=comp.label,
            section_ids=list(comp.section_ids),
            basis=comp.basis,
            raw_sum=raw_sum,
            raw_max=raw_max,
            percent=percent_of(raw_sum, raw_max),
        )
        total: int | None = None
        if comp.basis == "raw":
            if raw_sum is None:
                notes.append(f"incomplete_composite:{comp.key}")
            else:
                total = round_half_up(raw_sum)
        else:
            parts = [scaled.get(sid) for sid in comp.section_ids]
            if any(p is None for p in parts):
                notes.append(f"incomplete_composite:{comp.key}")
            else:
                total = sum(p for p in parts if p is not None)
                out = out.model_copy(update={"scaled_sum": total})
        if group is not None and total is not None:
            number, bound, display = _lookup(total, rows, group, comp.key, notes)
            if number is not None:
                out = out.model_copy(
                    update={"score": number, "score_display": display, "open_bound": bound}
                )
        composites.append(out)

    overall_exact: float | None = None
    overall: int | None = None
    overall_display: str | None = None
    bracket: str | None = None
    if group is not None:
        counted = (
            [c for c in composites if c.key == norms.final_composite]
            if norms.final == "composite"
            else composites
        )
        scores = [c.score for c in counted]
        if counted and all(s is not None for s in scores):
            overall_exact = sum(s for s in scores if s is not None) / len(scores)
            overall = round_half_up(overall_exact)
            bounds = {c.open_bound for c in counted if c.open_bound}
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
        norms_loaded=loaded,
        overall_exact=overall_exact,
        overall=overall,
        overall_display=overall_display,
        bracket=bracket,
        notes=list(dict.fromkeys(notes)),
        note=norms.note,
    )
    log.info(
        "iq report attempt=%s test=%s norms=%s age_group=%s composites=%s overall=%s notes=%s",
        attempt_id,
        test_id,
        "loaded" if loaded else "raw-only",
        report.age_group,
        [c.score_display if loaded else c.raw_sum for c in composites],
        overall_display,
        report.notes,
    )
    return report
