"""Power & session skills (nova/skills/power.py).

winops is monkeypatched throughout -- nothing here locks, sleeps or shuts
down anything. CONFIRM-tier skills run through a real Ladder with a
ScriptedConfirmChannel so the approval path is exercised end to end.
"""
from __future__ import annotations

import asyncio

import pytest

import nova.skills  # noqa: F401 -- populate the registry
import nova.skills.power as power
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.confirm import ScriptedConfirmChannel
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills.registry import SkillCtx, get_skill
from nova.system import winops


@pytest.fixture
def calls(monkeypatch):
    rec = {"programs": [], "lock": 0, "sleep": 0}

    def run_program(exe, *args, timeout=15.0):
        rec["programs"].append((exe, *args))
        if args == ("/a",) and rec.get("abort_error"):
            raise winops.WinOpsError(rec["abort_error"])
        return ""

    def lock():
        rec["lock"] += 1

    def sleep():
        rec["sleep"] += 1

    monkeypatch.setattr(winops, "run_program", run_program)
    monkeypatch.setattr(winops, "lock_workstation", lock)
    monkeypatch.setattr(winops, "sleep_computer", sleep)
    monkeypatch.setattr(power, "_windows_available", lambda: True)
    yield rec
    task = power._pending.pop("power", None)
    if task is not None:
        task.cancel()


def make_ctx(tmp_path, script=None):
    channel = ScriptedConfirmChannel(script) if script is not None else None
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log"), confirm_channel=channel))


async def run(ctx, name, **params):
    return await get_skill(name).invoke(ctx, **params)


# --- registration -----------------------------------------------------------------

@pytest.mark.parametrize("name, tier", [
    ("lock_computer", Tier.ACT),
    ("sleep_computer", Tier.CONFIRM),
    ("shutdown_computer", Tier.CONFIRM),
    ("restart_computer", Tier.CONFIRM),
    ("sign_out", Tier.CONFIRM),
    ("cancel_shutdown", Tier.ACT),
])
def test_tiers(name, tier):
    spec = get_skill(name)
    assert spec is not None and spec.tier is tier and spec.trusts_input is False


def test_confirm_phrases_read_naturally():
    assert get_skill("shutdown_computer").confirm_phrase == "shut down the computer"
    assert get_skill("restart_computer").confirm_phrase == "restart the computer"
    assert get_skill("sleep_computer").confirm_phrase == "put the computer to sleep"
    assert get_skill("sign_out").confirm_phrase == "sign you out of Windows"


# --- routing ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def router():
    return Router()


@pytest.mark.parametrize("sentence, skill", [
    ("lock the computer", "lock_computer"),
    ("lock my pc", "lock_computer"),
    ("lock screen", "lock_computer"),
    ("lock it", "lock_computer"),
    ("please lock the laptop", "lock_computer"),
    ("put the computer to sleep", "sleep_computer"),
    ("go to sleep", "sleep_computer"),
    ("sleep mode", "sleep_computer"),
    ("put my pc to sleep", "sleep_computer"),
    ("shut down the computer", "shutdown_computer"),
    ("shut down", "shutdown_computer"),
    ("shutdown", "shutdown_computer"),
    ("turn off my pc", "shutdown_computer"),
    ("power off the laptop", "shutdown_computer"),
    ("shut it down", "shutdown_computer"),
    ("restart", "restart_computer"),
    ("restart the computer", "restart_computer"),
    ("reboot my pc", "restart_computer"),
    ("sign out", "sign_out"),
    ("log me out", "sign_out"),
    ("log off windows", "sign_out"),
    ("cancel shutdown", "cancel_shutdown"),
    ("cancel the restart", "cancel_shutdown"),
    ("abort shutdown", "cancel_shutdown"),
    ("don't shut down", "cancel_shutdown"),
    ("stop the shutdown", "cancel_shutdown"),
])
def test_routes(router, sentence, skill):
    m = router.route(sentence)
    assert m.spec is not None, m.clarification
    assert m.spec.name == skill and m.params == {}


@pytest.mark.parametrize("sentence", [
    "turn off wifi", "turn off bluetooth", "shut down chrome", "close spotify", "restart spotify",
    "open notepad", "what time is it",
])
def test_power_skills_dont_steal(router, sentence):
    power_skills = {"lock_computer", "sleep_computer", "shutdown_computer", "restart_computer",
                    "sign_out", "cancel_shutdown"}
    m = router.route(sentence)
    assert m.spec is None or m.spec.name not in power_skills, (sentence, m.spec.name)


# --- lock --------------------------------------------------------------------------

async def test_lock(calls, tmp_path):
    reply = await run(make_ctx(tmp_path), "lock_computer")
    assert reply.success and reply.text == "Locked." and calls["lock"] == 1


async def test_lock_failure_is_honest(tmp_path, monkeypatch):
    def fail():
        raise winops.WinOpsError("LockWorkStation failed")
    monkeypatch.setattr(winops, "lock_workstation", fail)
    reply = await run(make_ctx(tmp_path), "lock_computer")
    assert not reply.success and reply.text == "I couldn't lock the computer."


async def test_lock_unavailable(tmp_path, monkeypatch):
    def fail():
        raise winops.WinOpsUnavailable("not windows")
    monkeypatch.setattr(winops, "lock_workstation", fail)
    reply = await run(make_ctx(tmp_path), "lock_computer")
    assert not reply.success


# --- sleep -------------------------------------------------------------------------

async def test_sleep_needs_confirmation_and_sleeps_after_the_reply(calls, tmp_path, monkeypatch):
    monkeypatch.setattr(power, "SLEEP_DELAY_SECONDS", 0)
    channel_ctx = make_ctx(tmp_path, [True])
    reply = await run(channel_ctx, "sleep_computer")
    assert reply.success and reply.text == "Going to sleep."
    assert channel_ctx.ladder._confirm_channel.calls[0].skill_name == "sleep_computer"
    await asyncio.gather(*list(power._background))
    assert calls["sleep"] == 1


async def test_sleep_declined_does_nothing(calls, tmp_path, monkeypatch):
    monkeypatch.setattr(power, "SLEEP_DELAY_SECONDS", 0)
    reply = await run(make_ctx(tmp_path, [False]), "sleep_computer")
    assert not reply.success
    await asyncio.sleep(0.01)
    assert calls["sleep"] == 0


async def test_sleep_without_a_confirm_channel_is_refused(calls, tmp_path):
    reply = await run(make_ctx(tmp_path), "sleep_computer")
    assert not reply.success and calls["sleep"] == 0


async def test_sleep_not_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(power, "_windows_available", lambda: False)
    reply = await run(make_ctx(tmp_path, [True]), "sleep_computer")
    assert not reply.success


# --- shutdown / restart / sign out -----------------------------------------------

@pytest.mark.parametrize("name, args, opener", [
    ("shutdown_computer", ("/s", "/t", "0"), "Shutting down"),
    ("restart_computer", ("/r", "/t", "0"), "Restarting"),
    ("sign_out", ("/l",), "Signing you out"),
])
async def test_power_action_after_grace_period(calls, tmp_path, monkeypatch, name, args, opener):
    reply = await run(make_ctx(tmp_path, [True]), name)
    assert reply.success
    assert reply.text.startswith(f"{opener} in 15 seconds")
    assert "cancel shutdown" in reply.text
    assert calls["programs"] == []          # nothing yet: still in the grace period
    # Speed the clock: run the pending task's command now.
    power._pending["power"].cancel()
    await asyncio.sleep(0)
    monkeypatch.setattr(power, "GRACE_SECONDS", 0)
    await run(make_ctx(tmp_path, [True]), name)
    await power._pending["power"]
    assert calls["programs"] == [("shutdown.exe", *args)]
    assert "power" not in power._pending


async def test_shutdown_declined_schedules_nothing(calls, tmp_path):
    reply = await run(make_ctx(tmp_path, [False]), "shutdown_computer")
    assert not reply.success
    assert "power" not in power._pending and calls["programs"] == []


async def test_shutdown_not_windows(calls, tmp_path, monkeypatch):
    monkeypatch.setattr(power, "_windows_available", lambda: False)
    reply = await run(make_ctx(tmp_path, [True]), "shutdown_computer")
    assert not reply.success and "power" not in power._pending


async def test_cancel_stops_a_pending_shutdown(calls, tmp_path):
    await run(make_ctx(tmp_path, [True]), "shutdown_computer")
    task = power._pending["power"]
    reply = await run(make_ctx(tmp_path), "cancel_shutdown")
    assert reply.success and reply.text.startswith("Cancelled")
    await asyncio.sleep(0)
    assert task.cancelled()
    assert calls["programs"] == [("shutdown.exe", "/a")]   # /s never ran


async def test_second_request_replaces_the_first(calls, tmp_path):
    await run(make_ctx(tmp_path, [True]), "shutdown_computer")
    first = power._pending["power"]
    await run(make_ctx(tmp_path, [True]), "restart_computer")
    await asyncio.sleep(0)
    assert first.cancelled() and power._pending["power"] is not first


async def test_cancel_with_nothing_pending(calls, tmp_path):
    calls["abort_error"] = "Unable to abort the system shutdown because no shutdown was in progress.(1116)"
    reply = await run(make_ctx(tmp_path), "cancel_shutdown")
    assert not reply.success and reply.text == "There's nothing to cancel."


async def test_cancel_aborts_a_windows_scheduled_shutdown(calls, tmp_path):
    reply = await run(make_ctx(tmp_path), "cancel_shutdown")
    assert reply.success and calls["programs"] == [("shutdown.exe", "/a")]
