"""Contract: nova/security/confirm.py — CONFIRM approval channels.

The module's own docstring names ScriptedConfirmChannel as "what nova-tester
drives to prove the ladder's CONFIRM branches ... without a human anywhere
near the loop" — this file is that use, plus direct coverage of the
prompt-redaction promise (docs/THREAT_MODEL.md §2.2.6: consent must be to
the actual redacted disclosure, not a generic toggle) and
ConsoleConfirmChannel's real timeout behavior.
"""
from __future__ import annotations

import io
import time

import pytest

from nova.security.confirm import (
    ConfirmCall,
    ConsoleConfirmChannel,
    ScriptedConfirmChannel,
    format_confirm_prompt,
)
from nova.security.ladder import ConfirmTimeout, Decision, Ladder
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillSpec


async def _dummy_handler(ctx, **params):
    return Reply(text="ok")


CONFIRM_SKILL = SkillSpec(
    name="dispatch_claude", tier=Tier.CONFIRM, examples=("build me an app",),
    trusts_input=False, handler=_dummy_handler,
)


class _FakeAudit:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, event):
        self.records.append(event)


# ---------------------------------------------------------------------------
# ScriptedConfirmChannel
# ---------------------------------------------------------------------------


async def test_scripted_channel_returns_outcomes_in_order():
    channel = ScriptedConfirmChannel([True, False])
    assert await channel.request(CONFIRM_SKILL, {"a": 1}) is True
    assert await channel.request(CONFIRM_SKILL, {"a": 2}) is False


async def test_scripted_channel_timeout_sentinel_raises_confirm_timeout():
    channel = ScriptedConfirmChannel([ScriptedConfirmChannel.TIMEOUT])
    with pytest.raises(ConfirmTimeout):
        await channel.request(CONFIRM_SKILL, {})


async def test_scripted_channel_exception_entry_is_raised_as_is():
    boom = RuntimeError("simulated broken channel")
    channel = ScriptedConfirmChannel([boom])
    with pytest.raises(RuntimeError) as exc_info:
        await channel.request(CONFIRM_SKILL, {})
    assert exc_info.value is boom


async def test_scripted_channel_records_every_call_regardless_of_outcome():
    channel = ScriptedConfirmChannel([True, False, ScriptedConfirmChannel.TIMEOUT])
    await channel.request(CONFIRM_SKILL, {"app": "x"})
    await channel.request(CONFIRM_SKILL, {"app": "y"})
    try:
        await channel.request(CONFIRM_SKILL, {"app": "z"})
    except ConfirmTimeout:
        pass

    assert channel.calls == [
        ConfirmCall(skill_name="dispatch_claude", params={"app": "x"}),
        ConfirmCall(skill_name="dispatch_claude", params={"app": "y"}),
        ConfirmCall(skill_name="dispatch_claude", params={"app": "z"}),
    ]


async def test_scripted_channel_raises_a_clear_error_when_the_script_is_exhausted():
    channel = ScriptedConfirmChannel([True])
    await channel.request(CONFIRM_SKILL, {})
    with pytest.raises(RuntimeError, match="exhausted"):
        await channel.request(CONFIRM_SKILL, {})


# ---------------------------------------------------------------------------
# End-to-end: Ladder + the real ScriptedConfirmChannel (not a test-only
# mock of the channel protocol) proves the seam is actually wired, not just
# that Decision looks plausible.
# ---------------------------------------------------------------------------


async def test_ladder_end_to_end_with_a_real_channel_that_approves():
    audit = _FakeAudit()
    channel = ScriptedConfirmChannel([True])
    ladder = Ladder(audit, confirm_channel=channel)

    decision = await ladder.authorize(CONFIRM_SKILL, {"app": "notepad"})

    assert decision == Decision.allow()
    assert channel.calls == [ConfirmCall(skill_name="dispatch_claude", params={"app": "notepad"})]
    assert audit.records[-1]["decision"] == "allow"


async def test_ladder_end_to_end_with_a_real_channel_that_times_out():
    audit = _FakeAudit()
    channel = ScriptedConfirmChannel([ScriptedConfirmChannel.TIMEOUT])
    ladder = Ladder(audit, confirm_channel=channel)

    decision = await ladder.authorize(CONFIRM_SKILL, {})

    assert decision.allowed is False
    assert decision.reason == "confirm_timed_out"
    assert len(channel.calls) == 1  # the channel really was reached


# ---------------------------------------------------------------------------
# format_confirm_prompt / _redact_preview: consent to a specific disclosure
# ---------------------------------------------------------------------------


def test_prompt_names_the_skill_and_confirm_tier():
    prompt = format_confirm_prompt(CONFIRM_SKILL, {"app": "notepad"})
    assert "dispatch_claude" in prompt
    assert "CONFIRM" in prompt


def test_prompt_redacts_a_home_path_in_params():
    prompt = format_confirm_prompt(CONFIRM_SKILL, {"path": r"C:\Users\manav\notes.txt"})
    assert "manav" not in prompt
    assert "[USER]" in prompt


def test_prompt_redacts_nested_dict_and_list_values():
    params = {
        "outer": {"inner_path": r"C:\Users\manav\a.txt"},
        "items": [r"C:\Users\manav\b.txt", "plain"],
    }
    prompt = format_confirm_prompt(CONFIRM_SKILL, params)
    assert "manav" not in prompt
    assert prompt.count("[USER]") == 2


def test_prompt_leaves_non_string_values_unchanged():
    prompt = format_confirm_prompt(CONFIRM_SKILL, {"count": 5, "enabled": True})
    assert "5" in prompt
    assert "True" in prompt


def test_prompt_applies_configured_redact_terms():
    prompt = format_confirm_prompt(CONFIRM_SKILL, {"note": "ask Manav about it"}, redact_terms=("Manav",))
    assert "Manav" not in prompt
    assert "[REDACTED]" in prompt


# ---------------------------------------------------------------------------
# ConsoleConfirmChannel: real (injected) stdin/stdout, real wall-clock timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", "YES\n", " y \n"])
async def test_console_channel_approves_on_recognized_affirmative_input(answer):
    out = io.StringIO()
    channel = ConsoleConfirmChannel(stream_in=io.StringIO(answer), stream_out=out)
    approved = await channel.request(CONFIRM_SKILL, {"app": "notepad"})
    assert approved is True
    assert "approved" in out.getvalue()


@pytest.mark.parametrize("answer", ["n\n", "no\n", "\n", "maybe\n", "yesno\n"])
async def test_console_channel_declines_on_anything_else(answer):
    out = io.StringIO()
    channel = ConsoleConfirmChannel(stream_in=io.StringIO(answer), stream_out=out)
    approved = await channel.request(CONFIRM_SKILL, {})
    assert approved is False
    assert "declined" in out.getvalue()


def test_console_channel_shows_the_redacted_prompt_before_reading():
    out = io.StringIO()
    channel = ConsoleConfirmChannel(
        stream_in=io.StringIO("n\n"), stream_out=out,
    )
    import asyncio

    asyncio.run(channel.request(CONFIRM_SKILL, {"path": r"C:\Users\manav\x.txt"}))
    assert "manav" not in out.getvalue()
    assert "[USER]" in out.getvalue()


class _NeverAnswersStream:
    """Simulates a human who never types anything — readline() blocks far
    longer than the channel's own timeout, so the test proves the asyncio
    wait_for timeout actually fires rather than waiting for the reader."""

    def readline(self):
        time.sleep(5)
        return "y\n"


async def test_console_channel_raises_confirm_timeout_when_nothing_is_typed_in_time():
    out = io.StringIO()
    channel = ConsoleConfirmChannel(timeout_s=0.1, stream_in=_NeverAnswersStream(), stream_out=out)

    start = time.monotonic()
    with pytest.raises(ConfirmTimeout):
        await channel.request(CONFIRM_SKILL, {})
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, "timeout did not fire promptly — waited for the abandoned reader thread"
    assert "no response in time" in out.getvalue()


async def test_console_channel_timeout_wired_through_the_real_ladder_denies():
    """The full real stack: Ladder -> ConsoleConfirmChannel -> a stream that
    never answers -> a timely, correctly-reasoned, audited deny."""
    audit = _FakeAudit()
    channel = ConsoleConfirmChannel(timeout_s=0.1, stream_in=_NeverAnswersStream(), stream_out=io.StringIO())
    ladder = Ladder(audit, confirm_channel=channel)

    decision = await ladder.authorize(CONFIRM_SKILL, {})

    assert decision.allowed is False
    assert decision.reason == "confirm_timed_out"
    assert audit.records[-1]["reason"] == "confirm_timed_out"
