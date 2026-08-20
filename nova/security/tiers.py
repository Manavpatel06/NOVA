"""The permission ladder's tiers (docs/CONTRACTS.md T0.5).

This module only defines the enum. `Ladder.authorize()` (docs/THREAT_MODEL.md
§1.3/§1.4/§4) is what actually enforces the rules below; this docstring
records the contract it must satisfy so the two don't drift.
"""

from __future__ import annotations

from enum import Enum


class Tier(Enum):
    """
    READ    — no confirmation friction, no rate limit. "Free" means UX cost,
              not "unchecked": every READ invocation still goes through
              registry validation and still gets an AuditLog record.
    ACT     — has a side effect. Logged. An audit-write failure for an ACT
              action must be treated as a deny, not swallowed.
    CONFIRM — has a side effect that must not proceed without an explicit,
              human-approved decision through a real UI channel. With no
              channel registered, authorize() fails closed: Decision.deny,
              reason "no_confirm_channel" — never a silent allow, never a
              hang.

    F45 (nova-security, T2.0): Tier also carries a real rank order — READ <
    ACT < CONFIRM, defined by `__lt__`/`__le__`/`__gt__`/`__ge__` below and
    the `_RANK` table they read — so that composing a skill's declared tier
    with a floor (`Ladder.authorize()`'s `floor=` keyword) is a meaningful
    `max(declared, floor)`, not an accident of the order these three lines
    happen to be written in. This was previously missing: `docs/M2_OPEN_
    BRAIN_PLAN.md`'s T2.4 design called for exactly this `max(...)` rule,
    and it was literally unimplementable — `Tier` was a plain `Enum`, and
    `max(Tier.READ, Tier.CONFIRM)` raised `TypeError: '>' not supported
    between instances of 'Tier' and 'Tier'`. Comparison is defined only
    between two `Tier` members: comparing a `Tier` to anything else returns
    `NotImplemented` (Python then raises its own `TypeError`), the same as
    `int` and `str` refuse to compare across types, rather than silently
    coercing something into an ordering that was never decided on purpose.

    This enum intentionally does NOT subclass `IntEnum`: `.value` is a
    string ("read"/"act"/"confirm") that audit records, config, and tests
    already read and compare against — an `IntEnum` would silently change
    `.value`'s type and break all of that. Rank is a separate, private
    lookup table (`_RANK`, module level, below), not the enum's `.value`.
    """

    READ = "read"
    ACT = "act"
    CONFIRM = "confirm"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return _RANK[self] < _RANK[other]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return _RANK[self] <= _RANK[other]

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return _RANK[self] > _RANK[other]

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return _RANK[self] >= _RANK[other]


# Defined after the class body (referenced only inside methods, so it is
# resolved at call time, once the module has finished loading, not at class-
# body-execution time) rather than folded into the enum's own `.value` —
# see the F45 docstring paragraph above for why `.value` had to stay a
# string. READ < ACT < CONFIRM is the entire ladder; a fourth tier would
# need a rank assigned here deliberately, not inferred.
_RANK: dict[Tier, int] = {Tier.READ: 0, Tier.ACT: 1, Tier.CONFIRM: 2}
