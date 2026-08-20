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
declared `SkillSpec.tier` — never from `params`.

The declared tier can still only be RAISED, never lowered: the optional
`floor` keyword (F45, T2.0) composes as `max(declared, floor)`, using the
real rank order READ < ACT < CONFIRM now defined on `Tier` itself
(nova/security/tiers.py) rather than an accident of enum definition order.
This is the one shared seam two different callers need and neither gets a
one-off for: F28 wants a *presence* floor (a turn armed by the wake word
rather than push-to-talk should not reach ACT unescorted) and T2.4 wants a
*provenance* floor (`max(declared_tier, CONFIRM)` for any action Claude
proposes rather than a locally-routed skill — docs/M2_OPEN_BRAIN_PLAN.md's
own wording for the rule, previously unimplementable because `Tier` had no
ordering at all). Building one mechanism means a floor is just a `Tier`
value handed to `authorize()` — this module never knows or cares whether it
came from arming state or from Claude's dispatcher.

**Neither caller is wired here.** F28 is still Manav's decision and T2.4 is
blocked behind two more findings (docs/THREAT_MODEL.md, T2.4 review) — this
change is the seam only, exercised so far by nothing but its own tests.

F47 (accepted): READ is exempt from any floor, unconditionally. A floor
exists to force more scrutiny onto an action that would otherwise proceed
with less than the situation calls for; READ has no confirmation friction
to add and no side effect for a floor to protect against, so raising it
buys no security and is exactly the kind of friction that gets a control
switched off. This is a property of the mechanism, not of one caller's
floor: a *presence* floor forcing "what time is it" through a confirm
dialog because the wake word armed the turn would be exactly as pointless
as a *provenance* floor doing it because Claude proposed the question
instead of routing it locally — so the exemption lives in `authorize()`
itself, unconditional on which floor is asking.

Every audited record now distinguishes `declared_tier` (what the skill
itself declares) from `tier` (the effective tier `authorize()` actually
acted on); a `floor` key and `tier_raised_by_floor` boolean appear whenever
a caller passes a floor, whether or not it ended up changing anything. "The
skill said READ, a floor said CONFIRM" and "the skill said CONFIRM, no
floor was involved" are different facts for whoever reads the log later,
and collapsing them into a single `tier` field would erase exactly the
distinction a floor exists to make visible.
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

    async def authorize(
        self,
        skill: SkillSpec,
        params: dict[str, Any],
        *,
        floor: Tier | None = None,
        confirm_display: dict[str, Any] | None = None,
    ) -> Decision:
        declared_tier = skill.tier
        tier = declared_tier

        # F45/T2.0: compose the floor in, `max(declared, floor)` — never
        # able to lower what the skill itself declared, only raise it. Two
        # guards before we touch `_RANK` (via Tier's own comparison
        # operators): `floor is None` is overwhelmingly the common case
        # today (no caller is wired yet — see module docstring), and
        # `isinstance(declared_tier, Tier)` protects the unknown-tier path
        # below (a bogus, non-Tier `skill.tier` must still reach the loud
        # TypeError at the bottom of this method, not a confusing KeyError
        # out of `_RANK` first).
        #
        # F47 (accepted): READ is exempt from any floor, unconditionally —
        # see module docstring for why this belongs in the mechanism itself
        # rather than in either caller.
        if floor is not None and isinstance(declared_tier, Tier) and declared_tier is not Tier.READ:
            tier = max(declared_tier, floor)

        if tier is Tier.CONFIRM:
            if self._confirm_channel is None:
                return self._deny_audited(skill, params, declared_tier, tier, floor, "no_confirm_channel")
            # F46/T2.0: what the human actually SEES is `params` overlaid
            # with `confirm_display` (a skill's `resolve_for_confirm`
            # result, threaded in by SkillSpec.invoke() — computed before
            # this call, since it must reflect the state of the world at
            # the moment the ladder is about to ask, not after). Only the
            # display copy is overlaid; `params` itself — what's audited
            # and, once approved, what the handler actually receives —
            # is untouched. A skill with no resolve_for_confirm passes
            # confirm_display=None and this is a no-op, same behavior as
            # before F46.
            shown_params = {**params, **confirm_display} if confirm_display else params
            # Three distinct causes, three distinct audited reasons — a
            # human declining and a channel that never asked anyone are not
            # the same fact, and collapsing them would put a false
            # statement in the log (docs/THREAT_MODEL.md §1.4 amendment,
            # lead's ruling 2026-08-18).
            try:
                approved = bool(await self._confirm_channel.request(skill, shown_params))
            except ConfirmTimeout as exc:
                logger.warning("confirm channel timed out for skill '%s': %s", skill.name, exc)
                return self._deny_audited(skill, params, declared_tier, tier, floor, "confirm_timed_out")
            except Exception as exc:  # noqa: BLE001 — any other broken-channel failure must deny, never crash the ladder or hang
                logger.error("confirm channel raised during authorize(skill=%s): %s", skill.name, exc)
                return self._deny_audited(skill, params, declared_tier, tier, floor, "confirm_channel_error")
            if not approved:
                return self._deny_audited(skill, params, declared_tier, tier, floor, "confirm_declined")
            return self._allow_with_required_audit(skill, params, declared_tier, tier, floor)

        if tier is Tier.ACT:
            return self._allow_with_required_audit(skill, params, declared_tier, tier, floor)

        if tier is Tier.READ:
            return self._allow_with_degrading_audit(skill, params, declared_tier, tier, floor)

        raise TypeError(f"Ladder.authorize(): unknown tier {tier!r} for skill {skill.name!r}")

    def _allow_with_required_audit(
        self, skill: SkillSpec, params: dict[str, Any], declared_tier: Tier, tier: Tier, floor: Tier | None,
    ) -> Decision:
        """ACT/CONFIRM path: an audit-write failure here is a deny, not a
        swallowed error and not a proceed-anyway."""
        try:
            self._record(skill, params, declared_tier, tier, floor, "allow")
        except AuditWriteError as exc:
            logger.error(
                "audit write failed for %s-tier skill '%s' — denying rather than proceeding on an unrecorded action: %s",
                tier.value, skill.name, exc,
            )
            return Decision.deny("audit_write_failed")
        return Decision.allow()

    def _allow_with_degrading_audit(
        self, skill: SkillSpec, params: dict[str, Any], declared_tier: Tier, tier: Tier, floor: Tier | None,
    ) -> Decision:
        """READ path: still audited every time ("free" means UX cost, not
        unaudited), but a write failure degrades — logs a WARNING naming the
        gap and proceeds — because READ is defined as free/reversible."""
        try:
            self._record(skill, params, declared_tier, tier, floor, "allow")
        except AuditWriteError as exc:
            logger.warning(
                "audit write failed for READ-tier skill '%s' — proceeding anyway (READ degrades, it does not deny): %s",
                skill.name, exc,
            )
        return Decision.allow()

    def _deny_audited(
        self, skill: SkillSpec, params: dict[str, Any], declared_tier: Tier, tier: Tier, floor: Tier | None, reason: str,
    ) -> Decision:
        """A denial must itself be audited and loud — a denied CONFIRM with
        no audit trail is invisible to the one person who could notice the
        pattern. If the audit write for the denial itself fails, the
        decision is still a deny (it already was one); we can only log the
        gap, not retry into a rule that just proved it can't write."""
        try:
            self._record(skill, params, declared_tier, tier, floor, "deny", reason=reason)
        except AuditWriteError as exc:
            logger.error(
                "audit write failed while recording a '%s' denial for skill '%s': %s",
                reason, skill.name, exc,
            )
        return Decision.deny(reason)

    def record_denial(self, skill: SkillSpec, params: dict[str, Any], reason: str) -> None:
        """Audit a refusal decided OUTSIDE authorize() itself — today, only
        SkillSpec.invoke()'s post-authorize resolve_for_confirm consistency
        check (F46/T2.0): a skill's own resolved detail disagreeing with
        itself between what a CONFIRM prompt showed and the moment the
        ladder actually returned is refused before the handler ever runs,
        and that refusal deserves exactly the audit trail a Decision.deny
        gets (docs/THREAT_MODEL.md §4: "free" or "already decided" never
        means "unaudited"). Deliberately public: this is the seam a caller
        reaches for when IT is the one refusing, not the ladder's own
        `authorize()` decision — the ladder still owns the audit record's
        shape (`_record`), the caller does not get to invent its own.

        Best-effort like `_deny_audited`: the refusal already happened and
        cannot be retroactively changed by a write failure here, so a
        failure is logged, not raised or escalated.
        """
        try:
            self._record(skill, params, skill.tier, skill.tier, None, "deny", reason=reason)
        except AuditWriteError as exc:
            logger.error(
                "audit write failed while recording a '%s' refusal for skill '%s': %s",
                reason, skill.name, exc,
            )

    def _record(
        self,
        skill: SkillSpec,
        params: dict[str, Any],
        declared_tier: Tier,
        tier: Tier,
        floor: Tier | None,
        decision: str,
        *,
        reason: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "kind": "ladder.authorize",
            "skill": skill.name,
            "declared_tier": declared_tier.value,
            "tier": tier.value,
            "decision": decision,
            "params": params,
        }
        # Present only when a caller actually passed a floor — its absence
        # means "no floor was involved in this call" without needing a
        # third, ambiguous state alongside True/False (F45/T2.0).
        if floor is not None:
            event["floor"] = floor.value
            event["tier_raised_by_floor"] = tier != declared_tier
        if reason is not None:
            event["reason"] = reason
        self._audit.record(event)  # raises AuditWriteError on failure — propagates to the caller above
