"""Wi-Fi / Bluetooth on and off via WinRT Windows.Devices.Radios (no admin).

PowerShell 5.1 can't `await` a WinRT IAsyncOperation, so the script uses the
well-known reflection bridge: find System.WindowsRuntimeSystemExtensions.
AsTask<T>(IAsyncOperation<T>), close it over the result type, and Wait().
If the radio API refuses (access denied, Bluetooth service off, no such
radio), NOVA opens the matching Settings page instead and says so.
"""
from __future__ import annotations

import asyncio

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

_log = get_logger("skills.radios")

# $NovaArgs.radio: wifi|bluetooth, $NovaArgs.state: on|off.
# Prints ok | denied | none. Same stdin layout rule as volume.py.
_RADIO_PS = r"""Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) { $t = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask)); $null = $t.Wait(-1); $t.Result }
$null = [Windows.Devices.Radios.Radio, Windows.System.Devices, ContentType=WindowsRuntime]
$null = [Windows.Devices.Radios.RadioAccessStatus, Windows.System.Devices, ContentType=WindowsRuntime]
$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
if ("$access" -ne 'Allowed') { Write-Output 'denied'; exit 0 }
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$kind = 'WiFi'
if ($NovaArgs.radio -eq 'bluetooth') { $kind = 'Bluetooth' }
$state = 'Off'
if ($NovaArgs.state -eq 'on') { $state = 'On' }
$targets = @($radios | Where-Object { "$($_.Kind)" -eq $kind })
if ($targets.Count -eq 0) { Write-Output 'none'; exit 0 }
foreach ($r in $targets) { $res = Await ($r.SetStateAsync($state)) ([Windows.Devices.Radios.RadioAccessStatus]); if ("$res" -ne 'Allowed') { Write-Output 'denied'; exit 0 } }
Write-Output 'ok'
"""

_NAMES = {"wifi": "Wi-Fi", "bluetooth": "Bluetooth"}
_PAGES = {"wifi": "network-wifi", "bluetooth": "bluetooth"}
_ON = {"on": "on", "enable": "on", "activate": "on", "start": "on",
       "off": "off", "disable": "off", "deactivate": "off", "stop": "off", "kill": "off"}

_RADIO = r"(?:the |my )?(?P<radio>wi-?fi|wi fi|wireless|bluetooth|blue tooth)"


def _radio_key(radio: str) -> str | None:
    r = radio.strip().lower().replace(" ", "").replace("-", "")
    if r in ("wifi", "wireless"):
        return "wifi"
    if r == "bluetooth":
        return "bluetooth"
    return None


@skill(
    name="set_radio",
    tier=Tier.ACT,
    examples=["turn off wifi", "turn on bluetooth", "disable wi-fi", "enable bluetooth"],
    description="Turn the Wi-Fi or Bluetooth radio on or off. Parameters: radio (wifi|bluetooth), state (on|off).",
    trusts_input=False,
    patterns=[
        rf"(?:turn|switch) (?P<state>on|off) {_RADIO}",
        rf"(?:turn|switch) {_RADIO} (?P<state>on|off)",
        rf"(?P<state>enable|disable|activate|deactivate|kill) {_RADIO}",
        rf"{_RADIO} (?P<state>on|off)",
    ],
)
async def set_radio(ctx: SkillCtx, radio: str, state: str) -> Reply:
    key = _radio_key(radio)
    want = _ON.get(state.strip().lower())
    if key is None or want is None:
        return Reply(text="I can turn Wi-Fi or Bluetooth on or off -- which one?", success=False)
    name = _NAMES[key]
    try:
        out = await asyncio.to_thread(winops.powershell, _RADIO_PS, timeout=20.0, radio=key, state=want)
        result = (out or "").strip().splitlines()[-1:] or [""]
        result = result[0].strip()
    except winops.WinOpsUnavailable as exc:
        _log.warning("radio %s %s unavailable: %s", key, want, exc)
        return Reply(text=f"I couldn't turn {name} {want}.", success=False)
    except (winops.WinOpsError, ValueError) as exc:
        _log.warning("radio %s %s failed: %s", key, want, exc)
        result = "error"
    if result == "ok":
        return Reply(text=f"{name}'s {want}.")
    _log.info("radio %s %s: api said %r, falling back to settings", key, want, result)
    try:
        await asyncio.to_thread(winops.open_settings_page, _PAGES[key])
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError, OSError) as exc:
        _log.warning("opening %s settings failed: %s", key, exc)
        return Reply(text=f"I couldn't turn {name} {want}.", success=False)
    return Reply(text=f"I couldn't switch {name} {want} directly, so I opened {name} settings for you.",
                 success=False)
