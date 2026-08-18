"""The permission ladder's tiers (docs/CONTRACTS.md T0.5).

This module only defines the enum. `Ladder.authorize()` (pending — see
docs/THREAT_MODEL.md §1.4/§4) is what actually enforces the rules below; this
docstring records the contract it must satisfy so the two don't drift.
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
    """

    READ = "read"
    ACT = "act"
    CONFIRM = "confirm"
