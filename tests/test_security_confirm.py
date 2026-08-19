"""Contract: nova/security/confirm.py — CONFIRM approval channels.

The module's own docstring names ScriptedConfirmChannel as "what nova-tester
drives to prove the ladder's CONFIRM branches ... without a human anywhere
near the loop" — this file is that use, plus direct coverage of the
prompt-redaction promise (docs/THREAT_MODEL.md §2.2.6: consent must be to
the actual redacted disclosure, not a generic toggle) and
ConsoleConfirmChannel's real timeout behavior.

## The swallowed-exception trap (read this before adding a test here)

An assertion that depends on an exception crossing a scheduled callback may
never see it. `post_callback`/`post_coroutine` (nova/threadsafe.py) hand a
result from a background thread to the asyncio loop via
`call_soon_threadsafe`/`run_coroutine_threadsafe`. If the scheduled callback
itself raises once it actually runs, that exception is caught and logged by
asyncio's own default handler — it does NOT propagate back to whatever
`await`ed the surrounding coroutine. A test that spies on such a callback by
recording "it was called" (or even "it was called with the right value") and
stops there can pass regardless of whether the callback's own body raised.

This bit an early version of
test_dialog_channel_end_to_end_a_late_click_after_timeout_is_silently_dropped
below: the spy appended to a list before calling through to the real
_settle_result, so removing the done-guard entirely still left the test
green — the resulting InvalidStateError was silently absorbed by asyncio,
never seen by `pytest.raises`/an assertion. Caught only by this file's own
rule (below), not by the test passing.

**The rule, applied to every test in this file:** assert *delivery*, not
merely the *absence* of a raised exception. Prefer an independent completion
signal (a `threading.Event` set from inside the real callback, a real
`asyncio.Future`/`concurrent.futures.Future` awaited or `.result(timeout=...)`ed,
or — if spying on a shared helper like `_settle_result` — explicitly
try/except around the passthrough call and assert on the captured exception,
not just that the spy was reached). And where a test's timeout value isn't
the thing under test, pin it short and explicit: three ConsoleConfirmChannel
tests here used to rely on the 60s *default* `timeout_s`, which meant a real
regression in delivery would still fail the test — just 60 seconds later
instead of near-instantly, which is nearly as bad as not failing at all for
anyone trying to notice quickly.
"""
from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import threading
import time

import pytest

import nova.security.confirm as confirm_mod
from nova.security.confirm import (
    ConfirmCall,
    ConsoleConfirmChannel,
    DialogConfirmChannel,
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
    # timeout_s pinned short (not the 60s default): this test isn't
    # exercising the timeout path, it's exercising real delivery through
    # the reader thread -> post_callback -> asyncio.Future handoff. A
    # regression that silently breaks that delivery must fail this test in
    # under a second, not after a genuine 60-second wait -- see this file's
    # header note on the swallowed-exception trap for why "eventually
    # fails" isn't good enough on its own.
    out = io.StringIO()
    channel = ConsoleConfirmChannel(timeout_s=5.0, stream_in=io.StringIO(answer), stream_out=out)
    approved = await channel.request(CONFIRM_SKILL, {"app": "notepad"})
    assert approved is True
    assert "approved" in out.getvalue()


@pytest.mark.parametrize("answer", ["n\n", "no\n", "\n", "maybe\n", "yesno\n"])
async def test_console_channel_declines_on_anything_else(answer):
    out = io.StringIO()
    channel = ConsoleConfirmChannel(timeout_s=5.0, stream_in=io.StringIO(answer), stream_out=out)
    approved = await channel.request(CONFIRM_SKILL, {})
    assert approved is False
    assert "declined" in out.getvalue()


def test_console_channel_shows_the_redacted_prompt_before_reading():
    out = io.StringIO()
    channel = ConsoleConfirmChannel(
        timeout_s=5.0, stream_in=io.StringIO("n\n"), stream_out=out,
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


# ---------------------------------------------------------------------------
# Regression: a timed-out confirm must not prevent process exit.
#
# nova-dev-sec's first draft used loop.run_in_executor(None, stream.readline)
# — the default ThreadPoolExecutor registers an atexit hook that joins every
# worker thread, so a reader blocked forever on a real readline() froze the
# whole interpreter at shutdown, with nothing pointing at the confirm path
# (it would present to Manav as "NOVA sometimes won't exit"). Fixed with a
# dedicated daemon=True thread instead. A real, deterministic test for "the
# process doesn't hang" has to actually exit a real process — an in-process
# test can't observe interpreter-shutdown behavior at all. Bounded and not
# flaky: the reader thread sleeps for 30s (never returns in any plausible
# test run), while the whole subprocess is asserted to exit within 10s —
# a regression to the non-daemon design would make this test fail by timing
# out at 10s, not by hanging for the full 30s.
# ---------------------------------------------------------------------------


def test_a_timed_out_confirm_does_not_prevent_process_exit():
    script = (
        "import asyncio, io, time\n"
        "from nova.security.confirm import ConsoleConfirmChannel\n"
        "from nova.security.ladder import ConfirmTimeout\n"
        "from nova.security.tiers import Tier\n"
        "from nova.skills.registry import Reply, SkillSpec\n"
        "\n"
        "async def handler(ctx):\n"
        "    return Reply(text='ok')\n"
        "\n"
        "skill = SkillSpec(name='x', tier=Tier.CONFIRM, examples=('x',), trusts_input=False, handler=handler)\n"
        "\n"
        "class NeverAnswers:\n"
        "    def readline(self):\n"
        "        time.sleep(30)\n"
        "        return 'y\\n'\n"
        "\n"
        "async def main():\n"
        "    channel = ConsoleConfirmChannel(timeout_s=0.1, stream_in=NeverAnswers(), stream_out=io.StringIO())\n"
        "    try:\n"
        "        await channel.request(skill, {})\n"
        "    except ConfirmTimeout:\n"
        "        pass\n"
        "\n"
        "asyncio.run(main())\n"
        "print('EXITED_CLEANLY')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=10,
    )
    assert "EXITED_CLEANLY" in result.stdout, (
        f"process did not print its exit marker; stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# DialogConfirmChannel — a real Win32 MessageBox, for when the human isn't
# watching a terminal. Zero coverage before this section despite being the
# live production confirm channel NovaApp constructs at boot (T1.4).
#
# `win32api`/`win32con` are imported locally inside DialogConfirmChannel's
# methods (never at module top-level), so every mocked test here injects
# fakes via sys.modules -- same technique test_interfaces_hotkey.py and
# test_interfaces_tray.py use, and no real MessageBox ever appears on
# screen during the default suite.
# ---------------------------------------------------------------------------


class _FakeWin32ConForDialog:
    MB_YESNO = 0x4
    MB_ICONQUESTION = 0x20
    MB_SYSTEMMODAL = 0x1000
    MB_TOPMOST = 0x40000
    IDOK = 1
    IDCANCEL = 2
    IDNO = 7
    IDYES = 6


class _FakeWin32ApiForDialog:
    def __init__(self, message_box_fn):
        self._message_box_fn = message_box_fn
        self.message_box_calls: list[tuple] = []

    def MessageBox(self, hwnd, prompt, title, flags):
        self.message_box_calls.append((hwnd, prompt, title, flags))
        return self._message_box_fn(prompt, title, flags)


@pytest.fixture
def fake_win32_dialog(monkeypatch):
    def _install(message_box_fn):
        win32con = _FakeWin32ConForDialog()
        win32api = _FakeWin32ApiForDialog(message_box_fn)
        monkeypatch.setitem(sys.modules, "win32con", win32con)
        monkeypatch.setitem(sys.modules, "win32api", win32api)
        return win32con, win32api

    return _install


async def _wait_until(predicate, timeout=2.0):
    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Explicit-Yes-only: anything that isn't IDYES is a decline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer_attr,expected",
    [("IDYES", True), ("IDNO", False), ("IDCANCEL", False), ("IDOK", False)],
)
async def test_dialog_channel_treats_only_explicit_yes_as_approval(fake_win32_dialog, answer_attr, expected):
    def message_box(prompt, title, flags):
        return getattr(_FakeWin32ConForDialog, answer_attr)

    fake_win32_dialog(message_box)
    channel = DialogConfirmChannel(timeout_s=5.0)

    result = await channel.request(CONFIRM_SKILL, {})

    assert result is expected


async def test_dialog_channel_uses_yesno_iconquestion_systemmodal_topmost_flags(fake_win32_dialog):
    """No Cancel button on purpose (class docstring): fewer ways to produce
    an ambiguous answer, not more."""
    captured_flags = []

    def message_box(prompt, title, flags):
        captured_flags.append(flags)
        return _FakeWin32ConForDialog.IDYES

    win32con, _ = fake_win32_dialog(message_box)
    channel = DialogConfirmChannel(timeout_s=5.0)

    await channel.request(CONFIRM_SKILL, {})

    expected = win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL | win32con.MB_TOPMOST
    assert captured_flags == [expected]


async def test_dialog_channel_shows_the_redacted_prompt_and_titles_the_skill(fake_win32_dialog):
    captured = []

    def message_box(prompt, title, flags):
        captured.append((prompt, title))
        return _FakeWin32ConForDialog.IDYES

    fake_win32_dialog(message_box)
    channel = DialogConfirmChannel(timeout_s=5.0)

    await channel.request(CONFIRM_SKILL, {"path": r"C:\Users\manav\x.txt"})

    prompt, title = captured[0]
    assert "manav" not in prompt
    assert "[USER]" in prompt
    assert CONFIRM_SKILL.name in title


# ---------------------------------------------------------------------------
# Timeout raises ConfirmTimeout distinctly, not a plain False.
# ---------------------------------------------------------------------------


async def test_dialog_channel_raises_confirm_timeout_when_nothing_is_answered_in_time(fake_win32_dialog):
    block = threading.Event()

    def message_box(prompt, title, flags):
        block.wait(timeout=5)  # blocks well past the channel's own 0.1s timeout
        return _FakeWin32ConForDialog.IDYES

    fake_win32_dialog(message_box)
    channel = DialogConfirmChannel(timeout_s=0.1)

    start = time.monotonic()
    with pytest.raises(ConfirmTimeout):
        await channel.request(CONFIRM_SKILL, {})
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, "timeout did not fire promptly -- waited for the abandoned dialog thread"
    block.set()  # release the abandoned thread so it doesn't linger past the test


async def test_dialog_channel_timeout_wired_through_the_real_ladder_denies(fake_win32_dialog):
    block = threading.Event()

    def message_box(prompt, title, flags):
        block.wait(timeout=5)
        return _FakeWin32ConForDialog.IDYES

    fake_win32_dialog(message_box)
    audit = _FakeAudit()
    channel = DialogConfirmChannel(timeout_s=0.1)
    ladder = Ladder(audit, confirm_channel=channel)

    decision = await ladder.authorize(CONFIRM_SKILL, {})

    assert decision.allowed is False
    assert decision.reason == "confirm_timed_out"
    assert audit.records[-1]["reason"] == "confirm_timed_out"
    block.set()


# ---------------------------------------------------------------------------
# A missing/failing pywin32 surfaces the real exception, not ConfirmTimeout
# -- class docstring: "A missing/failing pywin32 import would already have
# surfaced above, via _settle_exception."
# ---------------------------------------------------------------------------


async def test_dialog_channel_propagates_the_real_exception_when_pywin32_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32api", None)  # forces ImportError inside _show_dialog

    channel = DialogConfirmChannel(timeout_s=5.0)

    with pytest.raises(ImportError):
        await channel.request(CONFIRM_SKILL, {})


# ---------------------------------------------------------------------------
# F30 (nova-tester): a late click after timeout cannot flip an already-
# audited denial -- the done-guard in _settle_result/_settle_exception drops
# it. This is the "known limitation, documented rather than hidden" the
# class docstring names: on timeout the dialog thread is abandoned with the
# box still on-screen, and a human clicking it after the ladder has already
# denied the request must do nothing.
# ---------------------------------------------------------------------------


def test_settle_result_on_an_already_resolved_future_is_a_noop():
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        fut.set_result("first, real answer")

        confirm_mod._settle_result(fut, "late answer")  # must not raise, must not change the outcome

        assert fut.result() == "first, real answer"
    finally:
        loop.close()


def test_settle_exception_on_an_already_resolved_future_is_a_noop():
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        fut.set_result("already resolved")

        confirm_mod._settle_exception(fut, RuntimeError("late failure"))  # must not flip it into an exception

        assert fut.result() == "already resolved"
    finally:
        loop.close()


def test_settle_result_on_a_cancelled_future_is_a_noop():
    """The real shape after a timeout: asyncio.wait_for cancels the future
    on TimeoutError, which is also 'done' -- a late _settle_result must not
    un-cancel it or otherwise resurrect it."""
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        fut.cancel()

        confirm_mod._settle_result(fut, "late yes")

        assert fut.cancelled() is True
    finally:
        loop.close()


async def test_dialog_channel_end_to_end_a_late_click_after_timeout_is_silently_dropped(
    fake_win32_dialog, monkeypatch
):
    """Full path through DialogConfirmChannel.request(): a real timeout
    fires first, THEN the abandoned dialog thread's late click arrives.
    Spies on the real _settle_result (passthrough, not replaced) to prove
    two things about the SAME call: it really is the late click reaching
    the done-guard (future already done at call time), and going through
    the real guard doesn't raise or otherwise misbehave."""
    block = threading.Event()

    def message_box(prompt, title, flags):
        block.wait(timeout=5)
        return _FakeWin32ConForDialog.IDYES  # a real, late "Yes" click

    fake_win32_dialog(message_box)

    real_settle_result = confirm_mod._settle_result
    observed: list[tuple] = []

    def spy_settle_result(fut, value):
        # Capture whether the real guard raises, not just whether it was
        # called: a callback scheduled via call_soon_threadsafe that raises
        # gets caught and logged by asyncio's own default callback
        # exception handler, never propagated back to this test's await --
        # so a passthrough that let the InvalidStateError of a missing
        # done-guard escape would otherwise go unnoticed here.
        was_done = fut.done()
        try:
            real_settle_result(fut, value)
            error = None
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, this IS the assertion
            error = exc
        observed.append((value, was_done, error))

    monkeypatch.setattr(confirm_mod, "_settle_result", spy_settle_result)

    channel = DialogConfirmChannel(timeout_s=0.1)
    with pytest.raises(ConfirmTimeout):
        await channel.request(CONFIRM_SKILL, {})

    block.set()  # release the late click now that the timeout has already denied it
    await _wait_until(lambda: len(observed) == 1)

    late_value, was_already_done, error = observed[0]
    assert was_already_done is True, "test setup issue -- the future was not yet done when the late click arrived"
    assert late_value == _FakeWin32ConForDialog.IDYES  # the real late click, correctly dropped by the guard
    assert error is None, f"the done-guard let the late click raise instead of dropping it: {error!r}"


# ---------------------------------------------------------------------------
# Regression, mirroring the ConsoleConfirmChannel test above: a timed-out
# dialog must not prevent process exit either. Same daemon-thread design,
# same class of hazard, same real-subprocess proof -- an in-process test
# cannot observe interpreter-shutdown behavior at all.
# ---------------------------------------------------------------------------


def test_a_timed_out_dialog_does_not_prevent_process_exit():
    script = (
        "import asyncio, sys, time, types\n"
        "fake_win32con = types.SimpleNamespace(\n"
        "    MB_YESNO=4, MB_ICONQUESTION=0x20, MB_SYSTEMMODAL=0x1000, MB_TOPMOST=0x40000, IDYES=6,\n"
        ")\n"
        "class FakeWin32Api:\n"
        "    def MessageBox(self, hwnd, prompt, title, flags):\n"
        "        time.sleep(30)\n"
        "        return 6\n"
        "sys.modules['win32con'] = fake_win32con\n"
        "sys.modules['win32api'] = FakeWin32Api()\n"
        "\n"
        "from nova.security.confirm import DialogConfirmChannel\n"
        "from nova.security.ladder import ConfirmTimeout\n"
        "from nova.security.tiers import Tier\n"
        "from nova.skills.registry import Reply, SkillSpec\n"
        "\n"
        "async def handler(ctx):\n"
        "    return Reply(text='ok')\n"
        "\n"
        "skill = SkillSpec(name='x', tier=Tier.CONFIRM, examples=('x',), trusts_input=False, handler=handler)\n"
        "\n"
        "async def main():\n"
        "    channel = DialogConfirmChannel(timeout_s=0.1)\n"
        "    try:\n"
        "        await channel.request(skill, {})\n"
        "    except ConfirmTimeout:\n"
        "        pass\n"
        "\n"
        "asyncio.run(main())\n"
        "print('EXITED_CLEANLY')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=10,
    )
    assert "EXITED_CLEANLY" in result.stdout, (
        f"process did not print its exit marker; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
