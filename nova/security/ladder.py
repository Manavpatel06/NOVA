"""`Ladder.authorize()` — the permission ladder's enforcement half
(docs/CONTRACTS.md T0.5, docs/THREAT_MODEL.md §1.3/§1.4/§4).

Two rules the constitution and threat model make non-negotiable:

1. **Audit failure means deny.** If the audit write fails on an ACT or
   CONFIRM decision, `authorize()` denies rather than proceeding on a
   record it cannot prove happened — "we could not log that this left" must
   mean "it did not leave." READ is the one exception, by design: it is
   defined as free/reversible, so a write failure there degrades (a loud
   WARNING naming the gap) instead of blocking the user's own clock lookup.
2. **CONFIRM fails closed, audibly.** A CONFIRM-tier action with no
   confirmation channel registered returns an *audited* `Decision.deny`
   with reason `"no_confirm_channel"` — never a silent allow, never a hang.
   This is exercised in M0 even though M0 ships zero CONFIRM skills: the
   retired build's actual failure was not a missing dialog (it had two) but
   that CONFIRM was quietly unapprovable for most of its life with no loud
   signal. Wiring this now means M1 attaches a channel; it does not
   discover a silent bypass.

Tier model, pinned here because docs/THREAT_MODEL.md §4 flagged the contract
as silent on it: `authorize()` re-derives the tier from the skill's own
declared `SkillSpec.tier` — never from `params`. M0 has no per-call/
param-derived tier signal at all, so there is nothing for a skill (or
injected content routed into its params) to lower. If a future milestone
adds one, it must be combined as `max(declared, computed)` — a call can only
end up *more* restricted than its declaration, never less.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from nova.log import get_logger
from nova.security.audit import AuditLog, AuditWriteError
from nova.security.tiers import Tier

if TYPE_CHECKING:
    # Deferred to break the ladder.py <-> registry.py import cycle now that
    # SkillCtx (registry.py) needs to hold a Ladder: this module only ever
    # uses SkillSpec in type annotations (never isinstance-checks it), and
    # `from __future__ import annotations` above makes every annotation a
    # string at runtime, so a real import here is never needed.
    from nova.skills.registry import SkillSpec

logger = get_logger("security.ladder")


class ConfirmChannel(Protocol):
    """A working, human-facing approval channel (docs/THREAT_MODEL.md §1.4).
    M0 registers none — that is the point: `Ladder.authorize()` must fail
    closed for every CONFIRM-tier action until M1 wires a real one in, not
    discover a gap later. Real implementations live in
    nova/security/confirm.py.
    """

    async def request(self, skill: SkillSpec, params: dict[str, Any]) -> bool:
        """Ask a human to approve `skill.name(**params)`. Return True only on
        an explicit approval; return False for an explicit decline. A
        channel that cannot get a clear answer in its own time budget must
        raise `ConfirmTimeout` rather than returning False — the ladder
        audits that as `"confirm_timed_out"`, a distinct reason from an
        explicit decline, because the two mean different things to whoever
        reads the log later. Any other exception (a broken channel) is
        treated the same as an explicit decline — never let an unclear
        outcome read as approval, and never hang indefinitely.
        """
        ...


class ConfirmTimeout(Exception):
    """Raised by a `ConfirmChannel.request()` implementation when no answer
    arrived within its own time budget. A distinct type, not a plain
    `return False`, so `Ladder.authorize()` can audit `"confirm_timed_out"`
    separately from `"confirm_declined"` (docs/THREAT_MODEL.md §1.4 amendment
    — a timeout and an explicit decline are different facts for whoever
    reads the audit log later, and must not collapse into the same reason).
    """


@dataclass(frozen=True)
class Decision:
    """The outcome of `Ladder.authorize()`. Construct via `.allow()` /
    `.deny(reason)`, not the constructor directly, so a reason is never
    accidentally omitted from a denial."""

    allowed: bool
    reason: str | None = None

    @classmethod
    def allow(cls) -> "Decision":
        return cls(allowed=True, reason=None)

    @classmethod
    def deny(cls, reason: str) -> "Decision":
        if not reason:
            raise ValueError("Decision.deny(reason=...) must not be empty — an unaudited-looking deny is not allowed")
        return cls(allowed=False, reason=reason)


class Ladder:
    """The permission ladder. One instance, constructed at boot with the
    process's `AuditLog` and (once one exists — not in M0) a `ConfirmChannel`.
    """

    def __init__(self, audit: AuditLog, *, confirm_channel: ConfirmChannel | None = None) -> None:
        self._audit = audit
        self._confirm_channel = confirm_channel

    async def authorize(self, skill: SkillSpec, params: dict[str, Any]) -> Decision:
        tier = skill.tier

        if tier is Tier.CONFIRM:
            if self._confirm_channel is None:
                return self._deny_audited(skill, params, tier, "no_confirm_channel")
            # Three distinct causes, three distinct audited reasons — a
            # human declining and a channel that never asked anyone are not
            # the same fact, and collapsing them would put a false
            # statement in the log (docs/THREAT_MODEL.md §1.4 amendment,
            # lead's ruling 2026-08-18).
            try:
                approved = bool(await self._confirm_channel.request(skill, params))
            except ConfirmTimeout as exc:
                logger.warning("confirm channel timed out for skill '%s': %s", skill.name, exc)
                return self._deny_audited(skill, params, tier, "confirm_timed_out")
            except Exception as exc:  # noqa: BLE001 — any other broken-channel failure must deny, never crash the ladder or hang
                logger.error("confirm channel raised during authorize(skill=%s): %s", skill.name, exc)
                return self._deny_audited(skill, params, tier, "confirm_channel_error")
            if not approved:
                return self._deny_audited(skill, params, tier, "confirm_declined")
            return self._allow_with_required_audit(skill, params, tier)

        if tier is Tier.ACT:
            return self._allow_with_required_audit(skill, params, tier)

        if tier is Tier.READ:
            return self._allow_with_degrading_audit(skill, params, tier)

        raise TypeError(f"Ladder.authorize(): unknown tier {tier!r} for skill {skill.name!r}")

    def _allow_with_required_audit(self, skill: SkillSpec, params: dict[str, Any], tier: Tier) -> Decision:
        """ACT/CONFIRM path: an audit-write failure here is a deny, not a
        swallowed error and not a proceed-anyway."""
        try:
            self._record(skill, params, tier, "allow")
        except AuditWriteError as exc:
            logger.error(
                "audit write failed for %s-tier skill '%s' — denying rather than proceeding on an unrecorded action: %s",
                tier.value, skill.name, exc,
            )
            return Decision.deny("audit_write_failed")
        return Decision.allow()

    def _allow_with_degrading_audit(self, skill: SkillSpec, params: dict[str, Any], tier: Tier) -> Decision:
        """READ path: still audited every time ("free" means UX cost, not
        unaudited), but a write failure degrades — logs a WARNING naming the
        gap and proceeds — because READ is defined as free/reversible."""
        try:
            self._record(skill, params, tier, "allow")
        except AuditWriteError as exc:
            logger.warning(
                "audit write failed for READ-tier skill '%s' — proceeding anyway (READ degrades, it does not deny): %s",
                skill.name, exc,
            )
        return Decision.allow()

    def _deny_audited(self, skill: SkillSpec, params: dict[str, Any], tier: Tier, reason: str) -> Decision:
        """A denial must itself be audited and loud — a denied CONFIRM with
        no audit trail is invisible to the one person who could notice the
        pattern. If the audit write for the denial itself fails, the
        decision is still a deny (it already was one); we can only log the
        gap, not retry into a rule that just proved it can't write."""
        try:
            self._record(skill, params, tier, "deny", reason=reason)
        except AuditWriteError as exc:
            logger.error(
                "audit write failed while recording a '%s' denial for skill '%s': %s",
                reason, skill.name, exc,
            )
        return Decision.deny(reason)

    def _record(self, skill: SkillSpec, params: dict[str, Any], tier: Tier, decision: str, *, reason: str | None = None) -> None:
        event: dict[str, Any] = {
            "kind": "ladder.authorize",
            "skill": skill.name,
            "tier": tier.value,
            "decision": decision,
            "params": params,
        }
        if reason is not None:
            event["reason"] = reason
        self._audit.record(event)  # raises AuditWriteError on failure — propagates to the caller above
