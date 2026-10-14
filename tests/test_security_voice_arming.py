"""Contract: nova/security/tiers.py ArmedBy.VOICE / ArmedBy.TYPED (Manav,
2026-10-14) through nova/skills/registry.py's SkillSpec.invoke() into
Ladder.authorize().

VOICE (sentence started with "Nova" while the window was open) and TYPED
(typed into NOVA's window) carry NO ceiling: an ACT-tier skill runs.
WAKE is unchanged: capped at READ, refused with "presence_required".
Every positive case asserts the handler actually ran; every negative case
asserts it did not -- a decision-level check alone would pass if a refusal
still let the side effect happen. Style mirrors tests/test_security_ladder.py.
"""
from __future__ import annotations

import pytest

from nova.config import Config
from nova.security.ladder import Ladder
from nova.security.tiers import ArmedBy, Tier
from nova.skills.registry import Reply, SkillCtx, SkillSpec


class _FakeAudit:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, event: dict) -> None:
        self.records.append(event)


def _make_skill(tier, ran: list) -> SkillSpec:
    async def handler(ctx, **params):
        ran.append(ctx.armed_by)
        return Reply(text="done")

    return SkillSpec(
        description="test skill", name="dummy", tier=tier,
        examples=("do the thing",), trusts_input=False, handler=handler,
    )


def test_new_members_exist_with_stable_values():
    assert ArmedBy.VOICE.value == "voice"
    assert ArmedBy.TYPED.value == "typed"


@pytest.mark.parametrize("armed_by", [ArmedBy.VOICE, ArmedBy.TYPED, ArmedBy.PTT])
async def test_voice_and_typed_reach_act_tier(armed_by):
    audit = _FakeAudit()
    ran: list = []
    ctx = SkillCtx(config=Config(), ladder=Ladder(audit), armed_by=armed_by)

    reply = await _make_skill(Tier.ACT, ran).invoke(ctx)

    assert ran == [armed_by], "ACT-tier handler did not run"
    assert reply.text == "done"
    record = audit.records[-1]
    assert record["decision"] == "allow"
    assert record.get("reason") != "presence_required"
    # no READ ceiling was applied
    assert record.get("ceiling") in (None,)
    assert "ceiling_exceeded" not in record or record["ceiling_exceeded"] is False


async def test_wake_is_still_refused_with_presence_required():
    audit = _FakeAudit()
    ran: list = []
    ctx = SkillCtx(config=Config(), ladder=Ladder(audit), armed_by=ArmedBy.WAKE)

    reply = await _make_skill(Tier.ACT, ran).invoke(ctx)

    assert ran == [], "wake-armed ACT handler ran despite the READ ceiling"
    assert reply.text != "done"
    record = audit.records[-1]
    assert record["decision"] == "deny"
    assert record["reason"] == "presence_required"
    assert record["ceiling"] == "read"
    assert record["ceiling_exceeded"] is True


@pytest.mark.parametrize("armed_by", [ArmedBy.VOICE, ArmedBy.TYPED, ArmedBy.WAKE])
async def test_read_tier_runs_for_every_arming_source(armed_by):
    audit = _FakeAudit()
    ran: list = []
    ctx = SkillCtx(config=Config(), ladder=Ladder(audit), armed_by=armed_by)
    await _make_skill(Tier.READ, ran).invoke(ctx)
    assert ran == [armed_by]


@pytest.mark.parametrize("armed_by", [ArmedBy.VOICE, ArmedBy.TYPED])
async def test_voice_and_typed_confirm_tier_still_needs_a_human_click(armed_by):
    """No ceiling is not 'no confirm': a CONFIRM-tier skill with no working
    confirm channel must not run, and must not be refused for presence."""
    audit = _FakeAudit()
    ran: list = []
    ctx = SkillCtx(config=Config(), ladder=Ladder(audit), armed_by=armed_by)

    await _make_skill(Tier.CONFIRM, ran).invoke(ctx)

    assert ran == []
    assert audit.records[-1]["decision"] == "deny"
    assert audit.records[-1].get("reason") != "presence_required"


@pytest.mark.parametrize("armed_by", [ArmedBy.VOICE, ArmedBy.TYPED])
async def test_voice_and_typed_confirm_tier_runs_when_human_approves(armed_by):
    class _Approve:
        def __init__(self):
            self.asked = 0

        async def request(self, skill, params):
            self.asked += 1
            return True

    channel = _Approve()
    audit = _FakeAudit()
    ran: list = []
    ctx = SkillCtx(config=Config(), ladder=Ladder(audit, confirm_channel=channel), armed_by=armed_by)

    await _make_skill(Tier.CONFIRM, ran).invoke(ctx)

    assert channel.asked == 1
    assert ran == [armed_by]
