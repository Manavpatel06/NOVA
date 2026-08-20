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


class ArmedBy(Enum):
    """F28 (Manav's decision, 2026-08-20): how the turn currently running
    was armed — the signal `nova/skills/registry.py`'s `SkillSpec.invoke()`
    turns into `Ladder.authorize()`'s `ceiling=` keyword (see
    `nova/security/ladder.py`'s own docstring for the ceiling mechanism
    itself; this enum only names the source, it does not decide anything).

    PTT  — a human pressed the hotkey. No ceiling: every tier the skill
           itself declares (subject to floor/CONFIRM as normal) is reachable.
    WAKE — the wake word armed the turn with no keypress at all. Ceiling of
           READ: nobody proved a human is actually present and asking on
           purpose (docs/THREAT_MODEL_ALWAYS_ON.md §4's whole concern, and
           WakeConfig.threshold's own comment names this as the reason
           lowering that number widens more than false-accept risk) — ACT
           and CONFIRM require push-to-talk, unconditionally, until this
           enum grows a third source with its own explicit ruling.

    Co-located with `Tier` rather than in `nova/audio/wake.py` or `nova/
    app.py`: this is a security primitive `Ladder.authorize()`'s docstring
    references directly, the same reason `Tier` itself lives here rather
    than in the module that first needed it.
    """

    PTT = "ptt"
    WAKE = "wake"


class Origin(Enum):
    """T2.4 (nova-security's threat model, docs/THREAT_MODEL_CLAUDE_
    FALLTHROUGH.md finding 2): who proposed this call — the signal
    `nova/skills/registry.py`'s `SkillSpec.invoke()` turns into `Ladder.
    authorize()`'s `floor=` keyword, mirroring exactly how `ArmedBy` above
    becomes `ceiling=`. This enum only names the source; it does not decide
    anything, same separation of concerns as `ArmedBy`.

    ROUTER  — the local `Router` matched this call directly against a
              skill's own declared examples. No floor: Manav asked for
              this by voice and the router understood him with enough
              confidence to act — that's what ACT/CONFIRM's "logged, not
              confirmed" calibration was already priced against.
    CLAUDE  — `nova.brain.claude_arm.ask()` proposed this call after the
              router missed or scored below its confidence floor. Floor of
              CONFIRM: the same action Manav asked for by voice and one a
              model *inferred* from that voice are different events (the
              lead's framing) — "Claude proposes, NOVA disposes" is only
              true if a proposal costs strictly more scrutiny than a
              direct match, never the same amount. F47 (READ exempt from
              any floor) applies here exactly as it does to `ArmedBy` — a
              Claude-proposed `time` call is not gated behind a dialog,
              because nothing about *who* asked changes what the world
              looks like afterward.

    Co-located with `Tier`/`ArmedBy` for the same reason both live here:
    a security primitive `Ladder.authorize()`'s docstring references
    directly belongs where the ladder is defined, not in whichever module
    first needed it.
    """

    ROUTER = "router"
    CLAUDE = "claude"
