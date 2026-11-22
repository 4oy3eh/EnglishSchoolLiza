"""Timed sections (IQ family, Phase 17.2) — the pure clock rules.

A test runs in "timed sections" mode when EVERY served section has its own
`time_limit_seconds` (`plan_sections` returns the plan, else None and delivery keeps
its plain single-deadline behaviour). Then:

* sections run strictly in order; section k becomes current (its untimed intro /
  sample screen) when section k-1 closed — the first one at attempt start;
* "begin" starts its clock; an intro left open for `intro_cap` seconds begins by
  itself at `intro_started_at + intro_cap` (`started_by="intro_cap"`);
* a section with a `study` phase shows the stimulus from begin for `study_seconds`
  (the study window), then its answering phase runs for `time_limit_seconds`;
  without one the answering phase starts at begin;
* deadline = begin + study_seconds + time_limit_seconds, capped by the attempt's
  own deadline; at the deadline the section closes (`expired`), or earlier when the
  student finishes it (`finished`); the next section's intro starts at that moment.

`resolve` replays these rules over the stored rows up to `now` and returns the
complete materialized timeline (existing rows + the transitions that have already
happened by the clock but were not yet written — an abandoned tab's sections close
on time even though nobody asked). It is pure and deterministic, so writing its
result is idempotent. Times are server time and never pause (golden rule #3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from contracts import AttemptSection, Test
from contracts.runtime import SectionPhase, SectionStartedBy


@dataclass(frozen=True)
class SectionPlan:
    section_id: str
    position: int
    time_limit_seconds: int
    study_seconds: int  # 0 = no study phase


def plan_sections(test: Test) -> list[SectionPlan] | None:
    """The timed-sections plan of a (served) test, or None when not every section
    has `time_limit_seconds` (then delivery ignores section clocks entirely)."""
    if not test.sections or any(s.time_limit_seconds is None for s in test.sections):
        return None
    return [
        SectionPlan(
            section_id=s.id,
            position=i,
            time_limit_seconds=int(s.time_limit_seconds or 0),
            study_seconds=s.study.study_seconds if s.study is not None else 0,
        )
        for i, s in enumerate(test.sections)
    ]


def overall_seconds(plans: list[SectionPlan], intro_cap: int) -> int:
    """The attempt's overall time: every section's intro allowance + study + limit."""
    return sum(intro_cap + p.study_seconds + p.time_limit_seconds for p in plans)


def begun(
    sec: AttemptSection,
    plan: SectionPlan,
    at: datetime,
    by: SectionStartedBy,
    attempt_deadline: datetime | None,
) -> AttemptSection:
    """`sec` with its clock started at `at` (study window + deadline fixed now)."""
    deadline = at + timedelta(seconds=plan.study_seconds + plan.time_limit_seconds)
    if attempt_deadline is not None:
        deadline = min(deadline, attempt_deadline)
    study_ends = None
    if plan.study_seconds:
        study_ends = min(at + timedelta(seconds=plan.study_seconds), deadline)
    return sec.model_copy(
        update={
            "started_at": at,
            "started_by": by,
            "study_started_at": at if plan.study_seconds else None,
            "study_ends_at": study_ends,
            "answering_started_at": None if plan.study_seconds else at,
            "deadline": deadline,
        }
    )


def resolve(
    plans: list[SectionPlan],
    rows: dict[str, AttemptSection],
    *,
    attempt_id: str,
    attempt_started_at: datetime,
    attempt_deadline: datetime | None,
    now: datetime,
    intro_cap: int,
) -> list[AttemptSection]:
    """Replay the clock rules up to `now`: the rows of every section that has been
    reached (closed ones + the current one), in order. Sections after the current
    one have no row (pending)."""
    out: list[AttemptSection] = []
    cursor: datetime = attempt_started_at
    for plan in plans:
        sec = rows.get(plan.section_id)
        if sec is None:
            if cursor > now:  # pragma: no cover - cursor is always a past moment
                break
            sec = AttemptSection(
                attempt_id=attempt_id,
                section_id=plan.section_id,
                position=plan.position,
                intro_started_at=cursor,
            )
        if sec.started_at is None:
            auto_at = sec.intro_started_at + timedelta(seconds=intro_cap)
            if now < auto_at:
                out.append(sec)  # current: on its intro screen
                break
            sec = begun(sec, plan, auto_at, "intro_cap", attempt_deadline)
        if (
            sec.answering_started_at is None
            and sec.study_ends_at is not None
            and now >= sec.study_ends_at
        ):
            sec = sec.model_copy(update={"answering_started_at": sec.study_ends_at})
        if sec.closed_at is None:
            if sec.deadline is None:  # a begun section always has its deadline
                raise ValueError(
                    f"section {sec.section_id!r} of attempt {attempt_id!r} begun without a deadline"
                )
            if now < sec.deadline:
                out.append(sec)  # current: study or answering
                break
            sec = sec.model_copy(update={"closed_at": sec.deadline, "close_reason": "expired"})
        out.append(sec)
        if sec.closed_at is None:  # pragma: no cover - set just above or stored closed
            raise ValueError(f"section {sec.section_id!r} of attempt {attempt_id!r} not closed")
        cursor = sec.closed_at
    return out


def phase_of(sec: AttemptSection | None, now: datetime) -> SectionPhase:
    """pending (not reached) / intro / study / answering / closed — at `now`."""
    if sec is None:
        return "pending"
    if sec.closed_at is not None and sec.closed_at <= now:
        return "closed"
    if sec.started_at is None:
        return "intro"
    if sec.study_ends_at is not None and now < sec.study_ends_at:
        return "study"
    return "answering"


def remaining_seconds(sec: AttemptSection | None, now: datetime, intro_cap: int) -> int:
    """Seconds left in the CURRENT phase: intro -> until the auto-begin, study ->
    until the recall starts, answering -> until the deadline; 0 otherwise."""
    phase = phase_of(sec, now)
    if sec is None or phase in ("pending", "closed"):
        return 0
    if phase == "intro":
        end = sec.intro_started_at + timedelta(seconds=intro_cap)
    elif phase == "study":
        assert sec.study_ends_at is not None
        end = sec.study_ends_at
    else:
        assert sec.deadline is not None
        end = sec.deadline
    return max(0, int((end - now).total_seconds()))
