"""System volume -- set / step / mute / read the default playback device.

Talks to Windows Core Audio (IMMDeviceEnumerator -> default render endpoint
-> IAudioEndpointVolume) through PowerShell with ONE inline C# definition
(`_AUDIO_CS`), compiled by Add-Type and reused by every volume action. The
spoken level only ever reaches the script as `$NovaArgs.value` (an integer
string this module produced itself), never as script text.

Media transport ("next song", "pause") belongs to the media skills, and
"open ..." to open_app -- the patterns below deliberately never match those.

Also hosts `parse_level` / level-pattern fragments shared with display.py.
"""
from __future__ import annotations

import asyncio
import re

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

_log = get_logger("skills.volume")

DEFAULT_STEP = 10

# --- spoken numbers (shared with display.py) ------------------------------

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_MAX_WORDS = {"max", "maximum", "full", "a hundred", "one hundred", "hundred", "all the way up"}
_MIN_WORDS = {"min", "minimum", "zero", "all the way down"}

# A regex fragment (no group) for a spoken level: "40", "forty", "forty five",
# "max". Patterns wrap it in their own named group.
_WORD_NUM = (
    r"(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    r"|sixteen|seventeen|eighteen|nineteen|(?:a |one )?hundred)"
)
LEVEL = rf"(?:\d{{1,3}}|{_WORD_NUM})"
LEVEL_OR_EXTREME = rf"(?:{LEVEL}|max|maximum|full|min|minimum)"
PERCENT = r"(?: ?%| percent| per cent)?"


def parse_level(text: str | None) -> int | None:
    """'40' / '40%' / 'forty' / 'forty-five' / 'max' -> int, or None if it is
    not a number. Not clamped -- callers decide what range is valid."""
    if text is None:
        return None
    t = re.sub(r"\s*(?:%|percent|per cent)$", "", text.strip().lower()).strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if t in _MAX_WORDS:
        return 100
    if t in _MIN_WORDS:
        return 0
    if t in _ONES:
        return _ONES[t]
    parts = re.split(r"[ -]", t)
    if len(parts) == 1 and parts[0] in _TENS:
        return _TENS[parts[0]]
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ONES and 0 < _ONES[parts[1]] < 10:
        return _TENS[parts[0]] + _ONES[parts[1]]
    return None


def parse_step(amount: str | None, default: int = DEFAULT_STEP) -> int | None:
    """Step size for up/down. Missing/vague ('a bit', 'a little') -> default;
    a number -> that number; a number outside 1..100 -> None (refuse)."""
    if amount is None or not amount.strip():
        return default
    n = parse_level(amount)
    if n is None:
        return default if re.fullmatch(r"(?:a )?(?:bit|little|tad|touch|notch)(?: more)?", amount.strip()) else None
    return n if 1 <= n <= 100 else None


# --- Core Audio -----------------------------------------------------------

# One definition for every volume action. Interface method order is the
# vtable order from the Windows SDK headers (mmdeviceapi.h, endpointvolume.h);
# every slot up to the last one used is declared, with its real signature.
# HRESULTs are left to the CLR's default (PreserveSig=false) translation, so a
# failing call becomes an exception -> non-zero exit -> WinOpsError.
_AUDIO_CS = r"""
using System;
using System.Runtime.InteropServices;
namespace NovaAudio {
    [ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioEndpointVolume {
        void RegisterControlChangeNotify(IntPtr pNotify);
        void UnregisterControlChangeNotify(IntPtr pNotify);
        uint GetChannelCount();
        void SetMasterVolumeLevel(float fLevelDB, IntPtr pguidEventContext);
        void SetMasterVolumeLevelScalar(float fLevel, IntPtr pguidEventContext);
        float GetMasterVolumeLevel();
        float GetMasterVolumeLevelScalar();
        void SetChannelVolumeLevel(uint nChannel, float fLevelDB, IntPtr pguidEventContext);
        void SetChannelVolumeLevelScalar(uint nChannel, float fLevel, IntPtr pguidEventContext);
        float GetChannelVolumeLevel(uint nChannel);
        float GetChannelVolumeLevelScalar(uint nChannel);
        void SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, IntPtr pguidEventContext);
        [return: MarshalAs(UnmanagedType.Bool)] bool GetMute();
    }
    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        [return: MarshalAs(UnmanagedType.IUnknown)]
        object Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams);
    }
    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        IntPtr EnumAudioEndpoints(int dataFlow, int dwStateMask);
        IMMDevice GetDefaultAudioEndpoint(int dataFlow, int role);
    }
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    class MMDeviceEnumeratorComObject { }
    public static class Endpoint {
        static IAudioEndpointVolume Volume() {
            IMMDeviceEnumerator enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
            IMMDevice device = enumerator.GetDefaultAudioEndpoint(0, 1); // eRender, eMultimedia
            Guid iid = typeof(IAudioEndpointVolume).GUID;
            return (IAudioEndpointVolume)device.Activate(ref iid, 23, IntPtr.Zero); // CLSCTX_ALL
        }
        public static int GetLevel() { return (int)Math.Round(Volume().GetMasterVolumeLevelScalar() * 100.0); }
        public static void SetLevel(int percent) {
            Volume().SetMasterVolumeLevelScalar(Math.Max(0, Math.Min(100, percent)) / 100.0f, IntPtr.Zero);
        }
        public static bool GetMute() { return Volume().GetMute(); }
        public static void SetMute(bool mute) { Volume().SetMute(mute, IntPtr.Zero); }
    }
}
"""

# op: get | set (value = 0..100) | step (value = signed delta) | mute (value = 1/0)
# Prints "<level> <muted 0|1>". Raising the volume (set/step up) also unmutes,
# like the Windows volume flyout does.
# Layout note: winops feeds scripts on stdin (`-Command -`), where PowerShell
# reads statements like an interactive console -- so no blank line ever sits
# inside a multi-line construct, and one follows each multi-line statement.
_VOLUME_PS = (
    "Add-Type -TypeDefinition @'" + _AUDIO_CS + "'@\n\n"
    r"""$op = [string]$NovaArgs.op
$value = 0
if ($NovaArgs.value) { $value = [int]$NovaArgs.value }
switch ($op) {
    'get'  { }
    'set'  { [NovaAudio.Endpoint]::SetLevel($value); if ($value -gt 0) { [NovaAudio.Endpoint]::SetMute($false) } }
    'step' {
        $new = [Math]::Max(0, [Math]::Min(100, [NovaAudio.Endpoint]::GetLevel() + $value))
        [NovaAudio.Endpoint]::SetLevel($new)
        if ($value -gt 0) { [NovaAudio.Endpoint]::SetMute($false) }
    }
    'mute' { [NovaAudio.Endpoint]::SetMute($value -ne 0) }
    default { throw "unknown op $op" }
}

$muted = 0
if ([NovaAudio.Endpoint]::GetMute()) { $muted = 1 }
Write-Output ("{0} {1}" -f [NovaAudio.Endpoint]::GetLevel(), $muted)
"""
)


def _parse_state(out: str) -> tuple[int, bool]:
    m = re.search(r"(\d{1,3}) ([01])\s*$", out or "")
    if m is None:
        raise winops.WinOpsError(f"unexpected volume output {out[-80:]!r}")
    return int(m.group(1)), m.group(2) == "1"


async def _volume(op: str, value: int = 0) -> tuple[int, bool]:
    out = await asyncio.to_thread(winops.powershell, _VOLUME_PS, timeout=20.0, op=op, value=value)
    return _parse_state(out)


def _fail(action: str, exc: Exception) -> Reply:
    _log.warning("volume %s failed: %s", action, exc)
    return Reply(text="I couldn't change the volume." if action != "get" else "I couldn't read the volume.",
                 success=False)


def _say(level: int, muted: bool) -> str:
    return f"Volume's at {level}, but it's muted." if muted else f"Volume's at {level}."


_VOL = r"(?:the |my )?(?:volume|sound|audio)"
_IT = r"(?:it|the volume|the sound|the audio|volume|sound)"
_BIT = r"(?: (?:a )?(?:bit|little|tad|touch|notch)(?: more)?)?"
_BY = rf"(?: by (?P<amount>{LEVEL}){PERCENT})?"


@skill(
    name="set_volume",
    tier=Tier.ACT,
    examples=["set volume to 40", "volume 70 percent", "set the volume to 50", "max volume"],
    description="Set the system volume to an exact level. One parameter: level (0-100).",
    trusts_input=False,
    patterns=[
        rf"(?:set|change|put|turn|make|bring) {_VOL} (?:to |at |up to |down to )?(?P<level>{LEVEL_OR_EXTREME}){PERCENT}",
        rf"(?:the )?volume (?:to |at )?(?P<level>{LEVEL_OR_EXTREME}){PERCENT}",
        rf"(?P<level>max|maximum|full|min|minimum) (?:volume|sound)",
    ],
)
async def set_volume(ctx: SkillCtx, level: str) -> Reply:
    n = parse_level(level)
    if n is None or not 0 <= n <= 100:
        return Reply(text="Volume goes from 0 to 100 -- what level do you want?", success=False)
    try:
        now, muted = await _volume("set", n)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        return _fail("set", exc)
    return Reply(text=_say(now, muted))


@skill(
    name="volume_up",
    tier=Tier.ACT,
    examples=["turn it up", "louder", "volume up", "turn up the volume", "volume up by 20"],
    description="Raise the system volume by a step (default 10). Optional parameter: amount.",
    trusts_input=False,
    patterns=[
        rf"(?:turn|crank|pump) {_IT} up{_BIT}{_BY}",
        rf"(?:turn|crank|pump) up {_VOL}{_BIT}{_BY}",
        rf"(?:the )?(?:volume|sound) up{_BIT}{_BY}",
        rf"(?:raise|increase|boost) {_VOL}{_BIT}{_BY}",
        rf"(?:make it |make the sound |make the audio |a bit |a little |little )?louder{_BIT}{_BY}",
    ],
)
async def volume_up(ctx: SkillCtx, amount: str = "") -> Reply:
    step = parse_step(amount)
    if step is None:
        return Reply(text="I can raise it by 1 to 100 -- how much?", success=False)
    try:
        now, muted = await _volume("step", step)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        return _fail("up", exc)
    return Reply(text=_say(now, muted))


@skill(
    name="volume_down",
    tier=Tier.ACT,
    examples=["turn it down", "quieter", "volume down", "turn the volume down a bit", "lower the volume"],
    description="Lower the system volume by a step (default 10). Optional parameter: amount.",
    trusts_input=False,
    patterns=[
        rf"turn {_IT} down{_BIT}{_BY}",
        rf"turn down {_VOL}{_BIT}{_BY}",
        rf"(?:the )?(?:volume|sound) down{_BIT}{_BY}",
        rf"(?:lower|decrease|reduce|drop) {_VOL}{_BIT}{_BY}",
        rf"(?:make it |make the sound |make the audio |a bit |a little |little )?(?:quieter|softer){_BIT}{_BY}",
        rf"(?:it'?s |that'?s )?too loud",
    ],
)
async def volume_down(ctx: SkillCtx, amount: str = "") -> Reply:
    step = parse_step(amount)
    if step is None:
        return Reply(text="I can lower it by 1 to 100 -- how much?", success=False)
    try:
        now, muted = await _volume("step", -step)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        return _fail("down", exc)
    return Reply(text=_say(now, muted))


@skill(
    name="mute",
    tier=Tier.ACT,
    examples=["mute", "mute the sound", "mute the volume"],
    description="Mute the system audio output.",
    trusts_input=False,
    patterns=[
        rf"mute(?: {_IT}| (?:the |my )?(?:computer|laptop|speakers|pc|audio|sound|volume))?",
        r"(?:turn|switch) (?:the )?(?:sound|audio) off",
        r"(?:turn|switch) off (?:the )?(?:sound|audio)",
        r"silence",
    ],
)
async def mute(ctx: SkillCtx) -> Reply:
    try:
        await _volume("mute", 1)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        return _fail("mute", exc)
    return Reply(text="Muted.")


@skill(
    name="unmute",
    tier=Tier.ACT,
    examples=["unmute", "unmute the sound", "turn the sound back on"],
    description="Unmute the system audio output.",
    trusts_input=False,
    patterns=[
        rf"un-?mute(?: {_IT}| (?:the |my )?(?:computer|laptop|speakers|pc|audio|sound|volume))?",
        r"(?:turn|switch) (?:the )?(?:sound|audio) (?:back )?on",
        r"(?:turn|switch) (?:the )?(?:sound|audio) on again",
        r"(?:turn|switch) on (?:the )?(?:sound|audio)",
    ],
)
async def unmute(ctx: SkillCtx) -> Reply:
    try:
        level, _ = await _volume("mute", 0)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        return _fail("unmute", exc)
    return Reply(text=f"Unmuted. Volume's at {level}.")


@skill(
    name="get_volume",
    tier=Tier.READ,
    examples=["what's the volume", "what is the volume at", "current volume", "how loud is it"],
    description="Report the current system volume level and whether it's muted.",
    trusts_input=False,
    patterns=[
        rf"(?:what'?s|what is|whats) {_VOL}(?: level)?(?: at| set to)?(?: right now)?",
        rf"(?:what|which) (?:volume|level) is (?:it|the volume|the sound) (?:at|set to|on)",
        r"(?:current |the )?volume level",
        r"(?:the )?current volume",
        r"how loud is (?:it|the volume|the sound|my computer|my laptop)(?: right now)?",
        r"is (?:the )?(?:sound|audio|volume|it|my computer|my laptop) muted",
    ],
)
async def get_volume(ctx: SkillCtx) -> Reply:
    try:
        level, muted = await _volume("get")
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        return _fail("get", exc)
    return Reply(text=_say(level, muted))
