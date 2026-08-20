"""Contract: docs/THREAT_MODEL.md §1.3/§1.4/§4, docs/CONTRACTS.md T0.5 —
Ladder.authorize().

Two non-negotiable rules:
  1. Audit failure means deny for ACT/CONFIRM (READ degrades instead).
  2. CONFIRM with no channel registered fails closed with an AUDITED
     Decision.deny(reason="no_confirm_channel") — tested even though M0
     ships no CONFIRM skill, per the lead's and nova-security's explicit
     ask: the point is M1 attaches a channel rather than discovers a silent
     bypass.

Tier is re-derived from the skill's own declaration, never from params —
tested directly (a bogus 'tier' key smuggled into params must not change
which authorize() branch runs).
"""
from __future__ import annotations

import pytest

from nova.security.audit import AuditWriteError
from nova.security.ladder import ConfirmChannel, ConfirmTimeout, Decision, Ladder
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillSpec


async def _dummy_handler(ctx, **params):
    return Reply(text="ok")


def _make_skill(tier, name="dummy") -> SkillSpec:
    return SkillSpec(
        name=name, tier=tier, examples=("do the thing",), trusts_input=False, handler=_dummy_handler,
    )


class _FakeAudit:
    def __init__(self, raise_on_record: Exception | None = None):
        self.records: list[dict] = []
        self._raise = raise_on_record

    def record(self, event: dict) -> None:
        if self._raise is not None:
            raise self._raise
        self.records.append(event)


class _ApprovingChannel:
    async def request(self, skill, params):
        return True


class _DecliningChannel:
    async def request(self, skill, params):
        return False


class _RaisingChannel:
    async def request(self, skill, params):
        raise RuntimeError("dialog crashed")


class _TimingOutChannel:
    async def request(self, skill, params):
        raise ConfirmTimeout("no answer within the channel's own time budget")


# ---------------------------------------------------------------------------
# Decision construction
# ---------------------------------------------------------------------------


def test_decision_allow_has_no_reason():
    d = Decision.allow()
    assert d.allowed is True
    assert d.reason is None


def test_decision_deny_requires_a_reason():
    with pytest.raises(ValueError):
        Decision.deny("")


def test_decision_deny_carries_its_reason():
    d = Decision.deny("no_confirm_channel")
    assert d.allowed is False
    assert d.reason == "no_confirm_channel"


# ---------------------------------------------------------------------------
# READ: audited, degrades (never denies) on a write failure
# ---------------------------------------------------------------------------


async def test_read_tier_allows_and_is_audited():
    audit = _FakeAudit()
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.READ), {})

    assert decision == Decision.allow()
    assert audit.records[-1]["tier"] == "read"
    assert audit.records[-1]["decision"] == "allow"


async def test_read_tier_degrades_rather_than_denies_on_audit_failure(caplog):
    audit = _FakeAudit(raise_on_record=AuditWriteError("disk full"))
    ladder = Ladder(audit)
    with caplog.at_level("WARNING"):
        decision = await ladder.authorize(_make_skill(Tier.READ), {})

    assert decision == Decision.allow()  # "free" degrades, never denies
    assert any("READ" in rec.message or "read" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# ACT: audited, DENIES on a write failure (the deny path itself, not just
# the raise underneath it)
# ---------------------------------------------------------------------------


async def test_act_tier_allows_and_is_audited():
    audit = _FakeAudit()
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.ACT), {"app": "notepad"})

    assert decision == Decision.allow()
    assert audit.records[-1]["tier"] == "act"
    assert audit.records[-1]["params"] == {"app": "notepad"}


async def test_act_tier_denies_when_the_audit_write_fails():
    audit = _FakeAudit(raise_on_record=AuditWriteError("disk full"))
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.ACT), {})

    assert decision.allowed is False
    assert decision.reason == "audit_write_failed"


async def test_act_tier_denial_is_a_decision_object_not_a_raised_exception():
    """The ladder must convert the AuditWriteError into Decision.deny — it
    must not let the exception itself escape authorize()."""
    audit = _FakeAudit(raise_on_record=AuditWriteError("disk full"))
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.ACT), {})  # must not raise
    assert isinstance(decision, Decision)


# ---------------------------------------------------------------------------
# CONFIRM: fails closed and audibly with no channel
# ---------------------------------------------------------------------------


async def test_confirm_with_no_channel_denies_with_the_exact_reason():
    audit = _FakeAudit()
    ladder = Ladder(audit)  # no confirm_channel passed
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})

    assert decision.allowed is False
    assert decision.reason == "no_confirm_channel"


async def test_confirm_with_no_channel_is_audited_not_a_silent_deny():
    audit = _FakeAudit()
    ladder = Ladder(audit)
    await ladder.authorize(_make_skill(Tier.CONFIRM), {})

    assert len(audit.records) >= 1
    record = audit.records[-1]
    assert record["decision"] == "deny"
    assert record["reason"] == "no_confirm_channel"
    assert record["tier"] == "confirm"


async def test_confirm_with_no_channel_never_hangs_and_never_allows():
    """Loop over 20 calls as a cheap guard against any accidental await-
    forever path when no channel is registered."""
    audit = _FakeAudit()
    ladder = Ladder(audit)
    for _ in range(20):
        decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})
        assert decision.allowed is False


async def test_confirm_with_an_approving_channel_allows_and_audits():
    audit = _FakeAudit()
    ladder = Ladder(audit, confirm_channel=_ApprovingChannel())
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})

    assert decision == Decision.allow()
    assert audit.records[-1]["decision"] == "allow"


async def test_confirm_with_a_declining_channel_denies_with_a_distinct_reason():
    audit = _FakeAudit()
    ladder = Ladder(audit, confirm_channel=_DecliningChannel())
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})

    assert decision.allowed is False
    assert decision.reason == "confirm_declined"


async def test_confirm_channel_that_raises_denies_with_its_own_distinct_reason():
    """docs/THREAT_MODEL.md §1.4 amendment (2026-08-18): a broken channel and
    an explicit decline are different facts for whoever reads the audit log
    later and must not collapse into the same reason. Verified against the
    real code, not assumed from an older draft of this test."""
    audit = _FakeAudit()
    ladder = Ladder(audit, confirm_channel=_RaisingChannel())
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})  # must not raise

    assert decision.allowed is False
    assert decision.reason == "confirm_channel_error"


async def test_confirm_channel_timeout_denies_with_its_own_distinct_reason():
    audit = _FakeAudit()
    ladder = Ladder(audit, confirm_channel=_TimingOutChannel())
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})  # must not raise

    assert decision.allowed is False
    assert decision.reason == "confirm_timed_out"


async def test_the_four_confirm_denial_reasons_are_all_distinct():
    """no_confirm_channel / confirm_declined / confirm_timed_out /
    confirm_channel_error must never collapse into each other — each is
    audited as a different fact."""
    audit = _FakeAudit()
    skill = _make_skill(Tier.CONFIRM)

    no_channel = await Ladder(audit).authorize(skill, {})
    declined = await Ladder(audit, confirm_channel=_DecliningChannel()).authorize(skill, {})
    timed_out = await Ladder(audit, confirm_channel=_TimingOutChannel()).authorize(skill, {})
    channel_error = await Ladder(audit, confirm_channel=_RaisingChannel()).authorize(skill, {})

    reasons = {no_channel.reason, declined.reason, timed_out.reason, channel_error.reason}
    assert reasons == {"no_confirm_channel", "confirm_declined", "confirm_timed_out", "confirm_channel_error"}
    assert len(reasons) == 4  # no two collapsed into the same string


async def test_confirm_approved_but_audit_write_fails_still_denies():
    """The CONFIRM-approved path goes through the same required-audit rule
    as ACT — an unrecorded approval must not be treated as granted."""
    audit = _FakeAudit(raise_on_record=AuditWriteError("disk full"))
    ladder = Ladder(audit, confirm_channel=_ApprovingChannel())
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})

    assert decision.allowed is False
    assert decision.reason == "audit_write_failed"


# ---------------------------------------------------------------------------
# Tier is re-derived from the skill's declaration, never from params
# ---------------------------------------------------------------------------


async def test_params_cannot_smuggle_a_lower_tier():
    """An ACT-tier skill invoked with params that *claim* to be read-tier
    must still go through the ACT (required-audit, deny-on-failure) path."""
    audit = _FakeAudit(raise_on_record=AuditWriteError("disk full"))
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.ACT), {"tier": "read"})

    assert decision.allowed is False
    assert decision.reason == "audit_write_failed"  # proves the ACT branch ran, not READ's degrade


async def test_unknown_tier_value_raises_type_error():
    bogus = SkillSpec(
        name="bogus", tier="not-a-real-tier", examples=("x",), trusts_input=False,  # type: ignore[arg-type]
        handler=_dummy_handler,
    )
    audit = _FakeAudit()
    ladder = Ladder(audit)
    with pytest.raises(TypeError):
        await ladder.authorize(bogus, {})


# ---------------------------------------------------------------------------
# F45/T2.0: the shared `floor=` mechanism. Composes as max(declared, floor)
# via Tier's own real ordering (nova/security/tiers.py) -- can only RAISE
# the effective tier, never lower it -- and every audited record carries
# declared_tier/floor/tier_raised_by_floor so *why* an action needed
# confirming survives in the log. Neither F28 (presence floor) nor T2.4
# (Claude-provenance floor) is wired to this yet; these tests exercise the
# mechanism itself, exactly as the module docstring says.
# ---------------------------------------------------------------------------


async def test_no_floor_passed_behaves_exactly_as_before_and_omits_floor_keys():
    """Backward compatibility: omitting `floor` entirely must not add
    `floor`/`tier_raised_by_floor` to the audit record at all -- their
    absence is how a reader distinguishes 'no floor was involved' from a
    floor that happened not to change anything (module docstring)."""
    audit = _FakeAudit()
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.ACT), {})

    assert decision == Decision.allow()
    record = audit.records[-1]
    assert record["declared_tier"] == "act"
    assert record["tier"] == "act"
    assert "floor" not in record
    assert "tier_raised_by_floor" not in record


async def test_a_floor_raises_a_lower_declared_tier_to_confirm():
    """An ACT-tier skill under a CONFIRM floor must actually go through the
    CONFIRM branch -- proved here by the fact that with no confirm channel
    registered it fails closed with 'no_confirm_channel', which only the
    CONFIRM branch produces."""
    audit = _FakeAudit()
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.ACT), {}, floor=Tier.CONFIRM)

    assert decision.allowed is False
    assert decision.reason == "no_confirm_channel"
    record = audit.records[-1]
    assert record["declared_tier"] == "act"
    assert record["tier"] == "confirm"
    assert record["floor"] == "confirm"
    assert record["tier_raised_by_floor"] is True


async def test_a_floor_raised_confirm_can_still_be_approved_via_a_real_channel():
    audit = _FakeAudit()
    ladder = Ladder(audit, confirm_channel=_ApprovingChannel())
    decision = await ladder.authorize(_make_skill(Tier.ACT), {}, floor=Tier.CONFIRM)

    assert decision == Decision.allow()
    record = audit.records[-1]
    assert record["tier"] == "confirm"
    assert record["tier_raised_by_floor"] is True


async def test_a_floor_lower_than_the_declared_tier_never_lowers_it():
    """The floor can only raise, never lower: a CONFIRM-tier skill under an
    ACT floor must stay CONFIRM -- proved by still failing closed with
    'no_confirm_channel' rather than the ACT branch's audit-failure-denies
    behavior."""
    audit = _FakeAudit()
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {}, floor=Tier.ACT)

    assert decision.allowed is False
    assert decision.reason == "no_confirm_channel"
    record = audit.records[-1]
    assert record["declared_tier"] == "confirm"
    assert record["tier"] == "confirm"
    assert record["floor"] == "act"
    assert record["tier_raised_by_floor"] is False  # unchanged -- not "raised" to its own value


async def test_a_floor_equal_to_the_declared_tier_is_not_marked_as_raised():
    audit = _FakeAudit()
    ladder = Ladder(audit)
    decision = await ladder.authorize(_make_skill(Tier.ACT), {}, floor=Tier.ACT)

    assert decision == Decision.allow()
    record = audit.records[-1]
    assert record["floor"] == "act"
    assert record["tier_raised_by_floor"] is False


async def test_f47_read_is_exempt_from_any_floor():
    """F47 (accepted): READ is exempt from any floor, unconditionally. If
    this exemption were not real, a CONFIRM floor on a READ-tier skill with
    no confirm channel registered would fail closed -- proving the actual
    outcome (an unaudited-friction-free allow) is the only way to show the
    exemption does something, not just that the code has an isinstance
    check that happens to be unreachable."""
    audit = _FakeAudit()
    ladder = Ladder(audit)  # no confirm channel -- if READ were floored to CONFIRM, this would deny
    decision = await ladder.authorize(_make_skill(Tier.READ), {}, floor=Tier.CONFIRM)

    assert decision == Decision.allow()
    record = audit.records[-1]
    assert record["declared_tier"] == "read"
    assert record["tier"] == "read"  # NOT raised to confirm
    assert record["floor"] == "confirm"  # the floor was passed...
    assert record["tier_raised_by_floor"] is False  # ...but had no effect, and that's recorded


async def test_f47_read_exemption_holds_even_with_an_approving_channel_present():
    """Not merely 'it fails closed because there's no channel' -- READ must
    stay READ (audited once, allowed, no confirm round-trip at all) even
    when a channel exists and would have approved."""
    audit = _FakeAudit()
    channel = _ApprovingChannel()
    ladder = Ladder(audit, confirm_channel=channel)
    decision = await ladder.authorize(_make_skill(Tier.READ), {}, floor=Tier.CONFIRM)

    assert decision == Decision.allow()
    assert audit.records[-1]["tier"] == "read"


async def test_floor_composition_uses_the_real_rank_table_not_declaration_order():
    """Guards against a regression back to F45's original bug: without
    Tier's real ordering, max(declared, floor) either raises TypeError or
    silently picks whichever operand happens to come first. Three
    independent pairings, cross-checked against the documented rank."""
    audit = _FakeAudit()
    ladder = Ladder(audit)

    read_floored_by_act = await ladder.authorize(_make_skill(Tier.READ), {}, floor=Tier.ACT)
    assert audit.records[-1]["tier"] == "read"  # READ exempt regardless of floor

    act_floored_by_read = await ladder.authorize(_make_skill(Tier.ACT), {}, floor=Tier.READ)
    assert audit.records[-1]["tier"] == "act"  # floor lower than declared: no change

    confirm_floored_by_act = await ladder.authorize(_make_skill(Tier.CONFIRM), {}, floor=Tier.ACT)
    assert audit.records[-1]["tier"] == "confirm"  # floor lower than declared: no change


async def test_a_non_tier_declared_tier_with_a_floor_still_raises_type_error_not_a_keyerror():
    """The isinstance(declared_tier, Tier) guard around the max() call must
    protect the unknown-tier path -- a bogus, non-Tier skill.tier with a
    floor passed must still reach the loud TypeError at the bottom of
    authorize(), not a confusing KeyError out of the Tier rank table when
    floor composition tries to look it up."""
    bogus = SkillSpec(
        name="bogus", tier="not-a-real-tier", examples=("x",), trusts_input=False,  # type: ignore[arg-type]
        handler=_dummy_handler,
    )
    audit = _FakeAudit()
    ladder = Ladder(audit)
    with pytest.raises(TypeError):
        await ladder.authorize(bogus, {}, floor=Tier.CONFIRM)
