"""Contract: docs/CONTRACTS.md T0.5 — the `time` skill (READ tier).

Time-independent per CLAUDE.md: asserts the *shape* of the reported time,
never a value pinned to when the test happened to run.
"""
from __future__ import annotations

import re

from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.skills.registry import SkillCtx, get_skill
from nova.skills.time import time_skill

_TIME_RE = re.compile(r"^It's \d{1,2}:\d{2} (AM|PM)\.$")


class _UnusedLadder:
    """Placeholder for tests that call the handler directly, bypassing
    SkillSpec.invoke() — ctx.ladder is never read on this path."""

    async def authorize(self, skill, params):
        raise AssertionError("ctx.ladder.authorize() was called but this test never expected it to be")


async def test_time_skill_reports_a_plausibly_shaped_time():
    ctx = SkillCtx(config=Config(), ladder=_UnusedLadder())
    reply = await time_skill(ctx)

    assert reply.success is True
    assert _TIME_RE.match(reply.text), f"unexpected shape: {reply.text!r}"


async def test_time_skill_has_no_side_effects_and_takes_no_params():
    """READ tier, no params per its own docstring — calling it twice in a
    row must not raise or require any state."""
    ctx = SkillCtx(config=Config(), ladder=_UnusedLadder())
    first = await time_skill(ctx)
    second = await time_skill(ctx)
    assert _TIME_RE.match(first.text)
    assert _TIME_RE.match(second.text)


async def test_time_skill_end_to_end_through_the_ladder_is_allowed_and_audited(tmp_path):
    """The real path: SkillSpec.invoke() -> Ladder.authorize() -> handler.
    READ tier must be allowed and still produce an audit record — 'free'
    means no confirmation friction, not unaudited."""
    audit = AuditLog(path=tmp_path / "audit.log")
    ladder = Ladder(audit)
    ctx = SkillCtx(config=Config(), ladder=ladder)
    spec = get_skill("time")
    assert spec is not None

    reply = await spec.invoke(ctx)

    assert reply.success is True
    assert _TIME_RE.match(reply.text)
    status = audit.verify()
    assert status.ok is True
    assert status.entries == 1
