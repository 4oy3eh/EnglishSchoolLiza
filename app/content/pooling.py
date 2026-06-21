"""Per-attempt randomization: section pooling + option shuffle (Phase 3.2).

Anti-leak design (CLAUDE.md golden rule #7): randomization happens at the
**section/passage-pool level** (draw a subset of interchangeable sections from a
bank) plus an **option shuffle** for `single_choice` items. Item order *within* a
section is never touched — it follows the passage/recording.

Everything here is a **pure function of an integer seed**. The `Attempt.seed`
column already exists precisely so the whole layout is reproducible: the same
seed always yields the same drawn sections and the same option permutation, so
the per-attempt permutation never has to be stored separately — it is recomputed
on demand (and `to_dict()` is offered for callers that want to cache it).

A seed is split into independent sub-streams (per pool, per item) via a SHA-256
keyed RNG, so adding/removing one pool or item does not perturb the others.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.core.logging import get_logger
from contracts import Section, SingleChoiceItem

log = get_logger(__name__)


def _rng(*parts: object) -> random.Random:
    """A `random.Random` seeded deterministically from `parts`.

    Joining the parts through SHA-256 gives well-separated, reproducible streams
    so e.g. `(seed, "pool", "reading")` and `(seed, "options", "q1")` are
    independent.
    """
    digest = hashlib.sha256("::".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --------------------------------------------------------------------------- #
# Bank definition (which sections are interchangeable).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SectionPool:
    """A slot of interchangeable sections; draw `pick` of them per attempt.

    All sections in a pool should target the same skill/part so any draw is a
    valid test. `pick` is clamped to the number available.
    """

    key: str
    sections: Sequence[Section]
    pick: int = 1

    def __post_init__(self) -> None:
        if self.pick < 1:
            raise ValueError(f"pool {self.key!r} pick must be >= 1")
        if not self.sections:
            raise ValueError(f"pool {self.key!r} has no sections")


@dataclass(frozen=True)
class TestBank:
    """An ordered set of pools for one test; draw order follows `pools`."""

    __test__ = False  # not a pytest test class despite the `Test*` name

    test_id: str
    pools: Sequence[SectionPool]


# --------------------------------------------------------------------------- #
# Per-attempt layout (the reproducible draw).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptionShuffle:
    """Per-attempt option permutation for one `single_choice` item.

    `order[displayed_index]` is the canonical option `key` shown at that
    position. Delivery serves options in `order` (stripped of `key`) and maps the
    student's chosen displayed index back to the canonical key before grading.
    """

    item_id: str
    order: tuple[str, ...]

    def to_canonical(self, displayed_index: int) -> str:
        """Canonical option key for the option shown at `displayed_index`."""
        return self.order[displayed_index]

    def to_displayed(self, canonical_key: str) -> int:
        """Displayed position of the option whose canonical key is given."""
        return self.order.index(canonical_key)


@dataclass(frozen=True)
class AttemptLayout:
    """The full reproducible draw for one attempt."""

    test_id: str
    seed: int
    section_ids: tuple[str, ...]
    option_shuffles: Mapping[str, OptionShuffle] = field(default_factory=dict)

    def to_canonical(self, item_id: str, displayed_index: int) -> str:
        """Map a displayed option index back to its canonical key for `item_id`."""
        return self.option_shuffles[item_id].to_canonical(displayed_index)

    def to_dict(self) -> dict[str, object]:
        """Serializable form (e.g. to cache the layout alongside an attempt)."""
        return {
            "test_id": self.test_id,
            "seed": self.seed,
            "section_ids": list(self.section_ids),
            "option_shuffles": {
                item_id: list(sh.order) for item_id, sh in self.option_shuffles.items()
            },
        }


# --------------------------------------------------------------------------- #
# Draw functions (pure).
# --------------------------------------------------------------------------- #
def draw_sections(bank: TestBank, seed: int) -> list[Section]:
    """Draw the per-attempt section set from `bank`, reproducibly from `seed`.

    Each pool draws `pick` sections with an independent seeded RNG; chosen
    sections keep the pool's declared order (stable, coherence-preserving). Pools
    are emitted in declared order. Logs every draw decision (INFO).
    """
    chosen: list[Section] = []
    for pool in bank.pools:
        count = min(pool.pick, len(pool.sections))
        indices = sorted(_rng(seed, "pool", pool.key).sample(range(len(pool.sections)), count))
        picked = [pool.sections[i] for i in indices]
        chosen.extend(picked)
        log.info(
            "pool draw test=%s seed=%d pool=%s pick=%d/%d -> %s",
            bank.test_id,
            seed,
            pool.key,
            count,
            len(pool.sections),
            [s.id for s in picked],
        )
    return chosen


def shuffle_options(item: SingleChoiceItem, seed: int) -> OptionShuffle:
    """Reproducible option permutation for one `single_choice` item.

    Never reorders item options in place; returns the displayed→canonical map.
    """
    order = [opt.key for opt in item.options]
    _rng(seed, "options", item.id).shuffle(order)
    return OptionShuffle(item_id=item.id, order=tuple(order))


def build_attempt_layout(bank: TestBank, seed: int) -> AttemptLayout:
    """Compute the complete reproducible layout for an attempt with `seed`.

    Draws sections, then shuffles the options of every `single_choice` item in
    the drawn sections (item order within a section is left untouched, rule #7).
    """
    sections = draw_sections(bank, seed)
    shuffles: dict[str, OptionShuffle] = {}
    for section in sections:
        for item in section.items:
            if isinstance(item, SingleChoiceItem):
                shuffles[item.id] = shuffle_options(item, seed)
    log.info(
        "attempt layout test=%s seed=%d sections=%d shuffled_items=%d",
        bank.test_id,
        seed,
        len(sections),
        len(shuffles),
    )
    return AttemptLayout(
        test_id=bank.test_id,
        seed=seed,
        section_ids=tuple(s.id for s in sections),
        option_shuffles=shuffles,
    )


def bank_from_sections(
    test_id: str, sections: Sequence[Section], *, pick: int = 1
) -> TestBank:
    """Group a flat section list into one pool per `skill` (declared-order stable).

    A convenience for turning a stored `Test`'s sections into a bank: sections
    sharing a skill become an interchangeable pool drawing `pick` each. Sections
    with no skill each form their own singleton pool (always included).
    """
    order: list[str] = []
    groups: dict[str, list[Section]] = {}
    for section in sections:
        key = section.skill if section.skill is not None else f"_solo:{section.id}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(section)
    pools = [SectionPool(key=key, sections=groups[key], pick=pick) for key in order]
    return TestBank(test_id=test_id, pools=pools)
