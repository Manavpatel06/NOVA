"""Power & session control (2026-10-15): lock, sleep, shut down, restart,
sign out, cancel.

Tiers: lock is ACT (harmless, one password away from undone). Sleep, shut
down, restart and sign out are CONFIRM -- NOVA asks and only "Nova, confirm"
approves. Cancel is ACT.

Shut down / restart / sign out wait 15 seconds *inside NOVA* and then call
`shutdown.exe /s|/r /t 0` (or `/l`), rather than `shutdown /s /t 15`:
Microsoft's docs for shutdown.exe say a /t greater than 0 implies /f, which
force-closes apps without letting them ask to save. With /t 0 Windows shows
its normal "these apps are preventing shutdown" screen for unsaved work.
`shutdown /l` also can't take /t at all, so a NOVA-side timer is the only way
to give sign-out the same grace period. "Nova, cancel shutdown" cancels the
timer and also runs `shutdown /a` in case a shutdown was scheduled some other
way.

Sleep runs a couple of seconds after the reply so NOVA can finish speaking.
"""
from __future__ import annotations

import asyncio
import sys

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

logger = get_logger("skills.power")

GRACE_SECONDS = 15
SLEEP_DELAY_SECONDS = 2.5

# action -> (shutdown.exe args, what we say it is doing, noun for cancel replies)
_ACTIONS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "shutdown": (("/s", "/t", "0"), "Shutting down", "shutdown"),
    "restart": (("/r", "/t", "0"), "Restarting", "restart"),
    "sign_out": (("/l",), "Signing you out", "sign-out"),
}

_pending: dict[str, asyncio.Task] = {}   # at most one entry: "power"
_background: set[asyncio.Task] = set()   # keep fire-and-forget tasks referenced


def _windows_available() -> bool:
    """Monkeypatched in tests. Checked up front so a non-Windows machine gets
    an honest "I can't" now rather than a silent failure 15 seconds later."""
    return sys.platform == "win32"


_DEVICE = r"(?:(?:the|my|this) )?(?:computer|pc|laptop|machine|system|workstation|windows)"


# --- lock ----------------------------------------------------------------------

@skill(
    name="lock_computer",
    tier=Tier.ACT,
    examples=["lock the computer", "lock my pc", "lock screen"],
    description="Lock the Windows session (same as Win+L).",
    trusts_input=False,
    patterns=[
        rf"lock(?: up)?(?: {_DEVICE})?",
        r"lock (?:it|everything|it up|up)",
        r"lock (?:the |my )?screen",
        r"lock (?:the |my )?(?:computer|pc|laptop) (?:screen|up)",
    ],
    confirm_phrase="lock the computer",
)
async def lock_computer(ctx: SkillCtx) -> Reply:
    try:
        await asyncio.to_thread(winops.lock_workstation)
    except winops.WinOpsUnavailable:
        return Reply(text="I can't lock this machine.", success=False)
    except winops.WinOpsError as exc:
        logger.error("lock_computer failed: %s", exc)
        return Reply(text="I couldn't lock the computer.", success=False)
    return Reply(text="Locked.")


# --- sleep ---------------------------------------------------------------------

async def _sleep_later(delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await asyncio.to_thread(winops.sleep_computer)
    except Exception as exc:  # noqa: BLE001 -- nobody is listening any more; log it
        logger.error("sleep_computer failed: %s", exc)


@skill(
    name="sleep_computer",
    tier=Tier.CONFIRM,
    examples=["put the computer to sleep", "go to sleep", "sleep the pc", "sleep mode"],
    description="Put the computer to sleep (asks for confirmation first).",
    trusts_input=False,
    patterns=[
        rf"(?:put|send) (?:{_DEVICE}|it|everything) to sleep",
        r"(?:go|goto) (?:to )?sleep(?: mode)?",
        rf"sleep(?: mode)?(?: {_DEVICE})?",
        rf"(?:enter|activate|start) sleep(?: mode)?",
        rf"{_DEVICE} (?:go to )?sleep(?: mode)?",
        rf"make {_DEVICE} (?:go to )?sleep",
    ],
    confirm_phrase="put the computer to sleep",
)
async def sleep_computer(ctx: SkillCtx) -> Reply:
    if not _windows_available():
        return Reply(text="I can't put this machine to sleep.", success=False)
    task = asyncio.get_running_loop().create_task(_sleep_later(SLEEP_DELAY_SECONDS))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return Reply(text="Going to sleep.")


# --- shut down / restart / sign out ------------------------------------------------

async def _power_later(action: str, delay: float) -> None:
    args, _say, _noun = _ACTIONS[action]
    try:
        await asyncio.sleep(delay)
        await asyncio.to_thread(winops.run_program, "shutdown.exe", *args)
        logger.info("power: %s issued", action)
    except asyncio.CancelledError:
        logger.info("power: %s cancelled", action)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("power: %s failed: %s", action, exc)
    finally:
        if _pending.get("power") is asyncio.current_task():
            _pending.pop("power", None)


async def _schedule(action: str) -> Reply:
    if not _windows_available():
        return Reply(text="I can't do that on this machine.", success=False)
    _args, say, _noun = _ACTIONS[action]
    old = _pending.pop("power", None)
    if old is not None and not old.done():
        old.cancel()
    _pending["power"] = asyncio.get_running_loop().create_task(_power_later(action, GRACE_SECONDS))
    logger.info("power: %s scheduled in %ss", action, GRACE_SECONDS)
    return Reply(text=f"{say} in {GRACE_SECONDS} seconds. Say \"Nova, cancel shutdown\" to stop it.")


@skill(
    name="shutdown_computer",
    tier=Tier.CONFIRM,
    examples=["shut down the computer", "turn off my pc", "power off the laptop", "shutdown"],
    description="Shut the computer down after a 15-second grace period (asks for confirmation first).",
    trusts_input=False,
    patterns=[
        rf"(?:shut ?down|power (?:off|down))(?: {_DEVICE})?",
        rf"(?:turn|switch) off {_DEVICE}",
        rf"(?:turn|switch) {_DEVICE} off",
        rf"shut (?:it|everything|{_DEVICE}) down",
    ],
    confirm_phrase="shut down the computer",
)
async def shutdown_computer(ctx: SkillCtx) -> Reply:
    return await _schedule("shutdown")


@skill(
    name="restart_computer",
    tier=Tier.CONFIRM,
    examples=["restart the computer", "reboot my pc", "restart"],
    description="Restart the computer after a 15-second grace period (asks for confirmation first).",
    trusts_input=False,
    patterns=[rf"(?:restart|reboot)(?: {_DEVICE})?", rf"(?:restart|reboot) (?:it|everything)"],
    confirm_phrase="restart the computer",
)
async def restart_computer(ctx: SkillCtx) -> Reply:
    return await _schedule("restart")


@skill(
    name="sign_out",
    tier=Tier.CONFIRM,
    examples=["sign out", "log me out", "log off windows"],
    description="Sign out of Windows after a 15-second grace period (asks for confirmation first).",
    trusts_input=False,
    patterns=[rf"(?:sign|log) (?:me )?(?:out|off)(?: of (?:{_DEVICE}|my account))?", r"(?:sign|log) ?(?:out|off)"],
    confirm_phrase="sign you out of Windows",
)
async def sign_out(ctx: SkillCtx) -> Reply:
    return await _schedule("sign_out")


@skill(
    name="cancel_shutdown",
    tier=Tier.ACT,
    examples=["cancel shutdown", "cancel the restart", "abort shutdown", "don't shut down"],
    description="Cancel a pending shutdown, restart or sign-out.",
    trusts_input=False,
    patterns=[
        r"(?:cancel|abort|stop|undo|call off|halt) (?:the |that |my )?(?:shut ?down|restart|reboot|sign ?out|log ?off|log ?out|power off)",
        r"(?:don't|do not|dont) (?:shut ?down|restart|reboot|sign (?:me )?out|log (?:me )?(?:out|off)|turn off)(?: {_DEVICE})?".replace("{_DEVICE}", _DEVICE),
        r"(?:never mind|nevermind) (?:the )?(?:shut ?down|restart|reboot|sign ?out)",
    ],
)
async def cancel_shutdown(ctx: SkillCtx) -> Reply:
    task = _pending.pop("power", None)
    cancelled_ours = task is not None and not task.done()
    if cancelled_ours:
        task.cancel()
    cancelled_windows = False
    try:
        await asyncio.to_thread(winops.run_program, "shutdown.exe", "/a")
        cancelled_windows = True
    except winops.WinOpsUnavailable:
        pass
    except winops.WinOpsError as exc:
        # exit 1116 = no shutdown was in progress; anything else is logged.
        if "1116" not in str(exc):
            logger.info("cancel_shutdown: shutdown /a said %s", exc)
    if cancelled_ours or cancelled_windows:
        return Reply(text="Cancelled. Nothing's shutting down.")
    return Reply(text="There's nothing to cancel.", success=False)
