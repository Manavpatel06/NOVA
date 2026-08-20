"""Contract: nova/security/tiers.py (T2.0/F45) — the shared tier ordering.

`Tier` deliberately stays a plain `Enum`, not `IntEnum`, because `.value` is
the string ("read"/"act"/"confirm") that audit records, config, and every
existing test already read and compare against -- an `IntEnum` would
silently change `.value`'s type. Ordering is instead a real `__lt__`/`__le__`
/`__gt__`/`__ge__` implementation backed by a private `_RANK` table, added
specifically so `Ladder.authorize()`'s `floor=` keyword can compose via a
genuine `max(declared, floor)` rather than an accident of declaration order
-- before this fix `max(Tier.READ, Tier.CONFIRM)` raised `TypeError`
(docstring, verified below).
"""
from __future__ import annotations

import pytest

from nova.security.tiers import Tier

ALL_TIERS = [Tier.READ, Tier.ACT, Tier.CONFIRM]


# ---------------------------------------------------------------------------
# READ < ACT < CONFIRM -- the whole ladder
# ---------------------------------------------------------------------------


def test_read_lt_act_lt_confirm():
    assert Tier.READ < Tier.ACT
    assert Tier.ACT < Tier.CONFIRM
    assert Tier.READ < Tier.CONFIRM


def test_ordering_is_strict_no_tier_is_less_than_itself():
    for tier in ALL_TIERS:
        assert not (tier < tier)
        assert not (tier > tier)


def test_le_and_ge_include_equality():
    for tier in ALL_TIERS:
        assert tier <= tier
        assert tier >= tier
    assert Tier.READ <= Tier.ACT
    assert Tier.CONFIRM >= Tier.ACT
    assert not (Tier.CONFIRM <= Tier.ACT)
    assert not (Tier.READ >= Tier.ACT)


def test_full_ordering_matrix_matches_read_act_confirm():
    """Every pair, both directions -- not just the three adjacent
    comparisons above."""
    rank = {Tier.READ: 0, Tier.ACT: 1, Tier.CONFIRM: 2}
    for a in ALL_TIERS:
        for b in ALL_TIERS:
            assert (a < b) == (rank[a] < rank[b])
            assert (a <= b) == (rank[a] <= rank[b])
            assert (a > b) == (rank[a] > rank[b])
            assert (a >= b) == (rank[a] >= rank[b])


def test_sorted_tiers_come_out_read_act_confirm():
    shuffled = [Tier.CONFIRM, Tier.READ, Tier.ACT]
    assert sorted(shuffled) == [Tier.READ, Tier.ACT, Tier.CONFIRM]


# ---------------------------------------------------------------------------
# max()/min() -- the exact operation Ladder.authorize()'s floor= composes
# with, and the exact operation that was unimplementable before this fix
# (docstring: `max(Tier.READ, Tier.CONFIRM)` raised TypeError).
# ---------------------------------------------------------------------------


def test_max_of_declared_and_floor_picks_the_higher_tier():
    assert max(Tier.READ, Tier.CONFIRM) is Tier.CONFIRM
    assert max(Tier.ACT, Tier.READ) is Tier.ACT
    assert max(Tier.CONFIRM, Tier.CONFIRM) is Tier.CONFIRM


def test_min_of_declared_and_floor_picks_the_lower_tier():
    assert min(Tier.READ, Tier.CONFIRM) is Tier.READ
    assert min(Tier.ACT, Tier.ACT) is Tier.ACT


# ---------------------------------------------------------------------------
# .value stays the string every audit record already reads -- deliberately
# NOT an IntEnum, per the module docstring.
# ---------------------------------------------------------------------------


def test_value_is_still_the_lowercase_string_not_an_int():
    assert Tier.READ.value == "read"
    assert Tier.ACT.value == "act"
    assert Tier.CONFIRM.value == "confirm"


def test_tier_is_not_an_intenum_ordering_does_not_leak_into_value_or_equality():
    for tier in ALL_TIERS:
        assert not isinstance(tier.value, int)
        assert isinstance(tier.value, str)
    # If this were an IntEnum, Tier.READ == 0 would be True. It must not be.
    assert Tier.READ != 0
    assert Tier.ACT != 1
    assert Tier.CONFIRM != 2


# ---------------------------------------------------------------------------
# Comparing a Tier to a non-Tier raises TypeError -- it must not silently
# coerce ("act" < 3, or a bare string, sorting by some accidental rule).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("other", [0, 1, 2, "read", "act", None, 3.5, object()])
def test_comparing_tier_to_a_non_tier_raises_type_error(other):
    with pytest.raises(TypeError):
        Tier.READ < other
    with pytest.raises(TypeError):
        Tier.READ <= other
    with pytest.raises(TypeError):
        Tier.READ > other
    with pytest.raises(TypeError):
        Tier.READ >= other


def test_comparing_tier_to_non_tier_raises_symmetrically():
    """The comparison must fail the same way regardless of which side is
    the Tier -- Python falls back to the other operand's reflected method,
    which for a bare int/str is also NotImplemented, so this must still
    raise rather than silently succeeding via some other protocol."""
    with pytest.raises(TypeError):
        1 < Tier.ACT  # noqa: B015 -- deliberately unused comparison result
    with pytest.raises(TypeError):
        "confirm" > Tier.READ  # noqa: B015


# ---------------------------------------------------------------------------
# Non-vacuity anchor: the three members are exactly READ/ACT/CONFIRM, in
# that rank order -- if a fourth tier is ever added without an assigned
# rank, this suite (and _RANK's own KeyError) should be the first thing to
# notice, not a silent misordering.
# ---------------------------------------------------------------------------


def test_exactly_three_tiers_exist():
    assert {t.value for t in Tier} == {"read", "act", "confirm"}
