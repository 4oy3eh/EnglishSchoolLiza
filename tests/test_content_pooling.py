"""Phase 3 gate: seeded pooling + option shuffle.

Two seeded students must get different but valid section sets, and the option
permutation must round-trip displayed-index -> canonical key. Everything is a
pure function of the seed (reproducible, golden rule #7).
"""

from __future__ import annotations

from app.content.pooling import (
    SectionPool,
    TestBank,
    bank_from_sections,
    build_attempt_layout,
    draw_sections,
    shuffle_options,
)
from contracts import (
    GapFillItem,
    PassageTextStimulus,
    Section,
    SingleChoiceItem,
    TextOption,
)


def _choice(item_id: str, n_options: int = 4) -> SingleChoiceItem:
    keys = [chr(ord("A") + i) for i in range(n_options)]
    return SingleChoiceItem(
        id=item_id,
        prompt=f"Pick for {item_id}",
        options=[TextOption(key=k, text=f"opt {k}") for k in keys],
        correct="A",
    )


def _section(section_id: str) -> Section:
    return Section(
        id=section_id,
        skill="reading",
        stimulus=PassageTextStimulus(text=f"passage {section_id}"),
        items=[_choice(f"{section_id}-q1"), _choice(f"{section_id}-q2")],
    )


def _bank() -> TestBank:
    """One reading pool of 4 interchangeable passages, drawing 2 per attempt."""
    return TestBank(
        test_id="test-pool",
        pools=[
            SectionPool(
                key="reading-part-1",
                sections=[_section(f"sec-{i}") for i in range(4)],
                pick=2,
            )
        ],
    )


def test_two_students_get_different_section_sets() -> None:
    bank = _bank()
    alice = draw_sections(bank, seed=1001)
    bob = draw_sections(bank, seed=2002)

    alice_ids = [s.id for s in alice]
    bob_ids = [s.id for s in bob]

    # Both valid: exactly `pick` sections, all drawn from the pool, no dupes.
    pool_ids = {f"sec-{i}" for i in range(4)}
    for ids in (alice_ids, bob_ids):
        assert len(ids) == 2
        assert len(set(ids)) == 2
        assert set(ids) <= pool_ids
    # ...but different selections (these two seeds diverge).
    assert alice_ids != bob_ids


def test_draw_is_deterministic_for_a_seed() -> None:
    bank = _bank()
    first = [s.id for s in draw_sections(bank, seed=777)]
    second = [s.id for s in draw_sections(bank, seed=777)]
    assert first == second


def test_drawn_sections_keep_declared_order() -> None:
    # Chosen subset preserves pool order so item coherence is stable.
    ids = [s.id for s in draw_sections(_bank(), seed=42)]
    assert ids == sorted(ids, key=lambda i: int(i.split("-")[1]))


def test_pick_clamped_to_pool_size() -> None:
    bank = TestBank(
        test_id="t",
        pools=[SectionPool(key="p", sections=[_section("only")], pick=5)],
    )
    assert [s.id for s in draw_sections(bank, seed=3)] == ["only"]


def test_option_shuffle_round_trips_displayed_to_canonical() -> None:
    item = _choice("q1", n_options=4)
    shuffle = shuffle_options(item, seed=555)

    # The permutation is a bijection over the canonical keys.
    assert sorted(shuffle.order) == ["A", "B", "C", "D"]
    # displayed index -> canonical -> displayed index is the identity.
    for displayed in range(len(item.options)):
        canonical = shuffle.to_canonical(displayed)
        assert shuffle.to_displayed(canonical) == displayed


def test_option_shuffle_is_deterministic_and_item_scoped() -> None:
    item = _choice("q1")
    assert shuffle_options(item, seed=9).order == shuffle_options(item, seed=9).order
    # Different items under the same seed get independent permutations.
    other = _choice("q2")
    # (Not a strict guarantee they differ, but the streams are independent —
    # assert they are computed independently of item order by id.)
    assert shuffle_options(item, seed=9).item_id == "q1"
    assert shuffle_options(other, seed=9).item_id == "q2"


def test_build_attempt_layout_shuffles_every_single_choice() -> None:
    layout = build_attempt_layout(_bank(), seed=12345)

    # Two sections drawn, each with two single_choice items -> four shuffles.
    assert len(layout.section_ids) == 2
    assert len(layout.option_shuffles) == 4
    # Layout round-trips through to_canonical for a known item.
    some_item = next(iter(layout.option_shuffles))
    assert layout.to_canonical(some_item, 0) == layout.option_shuffles[some_item].order[0]

    # Serializable + reproducible.
    again = build_attempt_layout(_bank(), seed=12345)
    assert layout.to_dict() == again.to_dict()


def test_only_single_choice_items_are_shuffled() -> None:
    # A section mixing single_choice + gap_fill: only the single_choice item gets
    # an option permutation (gap_fill has no options to shuffle) — invariant #7.
    section = Section(
        id="mixed",
        skill="reading",
        stimulus=PassageTextStimulus(text="mixed section"),
        items=[
            _choice("mixed-sc"),
            GapFillItem(id="mixed-gap", prompt="The grass is ____.", accepted=["green"]),
        ],
    )
    bank = TestBank(test_id="t", pools=[SectionPool(key="p", sections=[section], pick=1)])
    layout = build_attempt_layout(bank, seed=1)

    assert set(layout.option_shuffles) == {"mixed-sc"}
    assert "mixed-gap" not in layout.option_shuffles


def test_bank_from_sections_groups_by_skill() -> None:
    sections = [_section("r1"), _section("r2")]
    bank = bank_from_sections("t", sections, pick=1)
    assert len(bank.pools) == 1
    assert bank.pools[0].key == "reading"
    assert {s.id for s in bank.pools[0].sections} == {"r1", "r2"}
