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
from nova.security.ladder import ConfirmChannel, Decision, Ladder
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


async def test_confirm_channel_that_raises_is_treated_as_a_decline_not_a_crash():
    audit = _FakeAudit()
    ladder = Ladder(audit, confirm_channel=_RaisingChannel())
    decision = await ladder.authorize(_make_skill(Tier.CONFIRM), {})  # must not raise

    assert decision.allowed is False
    assert decision.reason == "confirm_declined"


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
