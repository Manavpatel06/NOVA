"""open_settings (ACT): open a Windows Settings page by its spoken name.

A closed map of spoken name -> ms-settings page. Anything not in the map is
an honest "I don't know that settings page", never a guess -- the spoken
words never become the URI.
"""
from __future__ import annotations

import asyncio
import re

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

_log = get_logger("skills.settings_pages")

# spoken name -> (ms-settings page, how NOVA says it)
SETTINGS_PAGES: dict[str, tuple[str, str]] = {
    "wifi": ("network-wifi", "Wi-Fi"),
    "wireless": ("network-wifi", "Wi-Fi"),
    "network": ("network-status", "network"),
    "internet": ("network-status", "network"),
    "ethernet": ("network-ethernet", "Ethernet"),
    "vpn": ("network-vpn", "VPN"),
    "airplane mode": ("network-airplanemode", "airplane mode"),
    "hotspot": ("network-mobilehotspot", "mobile hotspot"),
    "mobile hotspot": ("network-mobilehotspot", "mobile hotspot"),
    "bluetooth": ("bluetooth", "Bluetooth"),
    "devices": ("bluetooth", "Bluetooth and devices"),
    "bluetooth and devices": ("bluetooth", "Bluetooth and devices"),
    "printer": ("printers", "printer"),
    "printers": ("printers", "printer"),
    "mouse": ("mousetouchpad", "mouse"),
    "touchpad": ("devices-touchpad", "touchpad"),
    "sound": ("sound", "sound"),
    "audio": ("sound", "sound"),
    "volume": ("sound", "sound"),
    "display": ("display", "display"),
    "screen": ("display", "display"),
    "monitor": ("display", "display"),
    "brightness": ("display", "display"),
    "night light": ("nightlight", "night light"),
    "battery": ("batterysaver", "battery"),
    "battery saver": ("batterysaver", "battery saver"),
    "power": ("powersleep", "power and sleep"),
    "power and sleep": ("powersleep", "power and sleep"),
    "sleep": ("powersleep", "power and sleep"),
    "storage": ("storagesense", "storage"),
    "notifications": ("notifications", "notifications"),
    "notification": ("notifications", "notifications"),
    "multitasking": ("multitasking", "multitasking"),
    "about": ("about", "about"),
    "personalization": ("personalization", "personalization"),
    "background": ("personalization-background", "background"),
    "wallpaper": ("personalization-background", "background"),
    "themes": ("themes", "themes"),
    "theme": ("themes", "themes"),
    "colors": ("colors", "colors"),
    "lock screen": ("lockscreen", "lock screen"),
    "taskbar": ("taskbar", "taskbar"),
    "apps": ("appsfeatures", "apps"),
    "installed apps": ("appsfeatures", "apps"),
    "default apps": ("defaultapps", "default apps"),
    "startup apps": ("startupapps", "startup apps"),
    "startup": ("startupapps", "startup apps"),
    "accounts": ("yourinfo", "accounts"),
    "account": ("yourinfo", "accounts"),
    "sign in": ("signinoptions", "sign-in options"),
    "sign in options": ("signinoptions", "sign-in options"),
    "date and time": ("dateandtime", "date and time"),
    "time": ("dateandtime", "date and time"),
    "language": ("regionlanguage", "language"),
    "keyboard": ("typing", "typing"),
    "typing": ("typing", "typing"),
    "privacy": ("privacy", "privacy"),
    "camera": ("privacy-webcam", "camera privacy"),
    "microphone": ("privacy-microphone", "microphone privacy"),
    "mic": ("privacy-microphone", "microphone privacy"),
    "location": ("privacy-location", "location"),
    "windows update": ("windowsupdate", "Windows Update"),
    "update": ("windowsupdate", "Windows Update"),
    "updates": ("windowsupdate", "Windows Update"),
    "gaming": ("gaming-gamebar", "gaming"),
    "game mode": ("gaming-gamemode", "game mode"),
    "accessibility": ("easeofaccess", "accessibility"),
}


def _key(page: str) -> str:
    t = page.strip().lower().replace("-", " ").replace("&", " and ")
    t = re.sub(r"^(?:the|my|windows)\s+", "", t) if t != "windows update" else t
    t = re.sub(r"\s+", " ", t).strip()
    return {"wi fi": "wifi", "blue tooth": "bluetooth"}.get(t, t)


async def _resolve_for_confirm(ctx: SkillCtx, params: dict) -> dict:
    entry = SETTINGS_PAGES.get(_key(str(params.get("page", ""))))
    return {"page": f"ms-settings:{entry[0]}" if entry else f"{params.get('page')!r} (unknown, would be refused)"}


@skill(
    name="open_settings",
    tier=Tier.ACT,
    examples=["open wifi settings", "open bluetooth settings", "open sound settings", "open display settings"],
    description=(
        "Open a Windows Settings page by name (wifi, bluetooth, sound, display, battery, "
        "windows update, ...). One parameter: page. Unknown pages are refused."
    ),
    trusts_input=False,
    resolve_for_confirm=_resolve_for_confirm,
    confirm_phrase="open {page} settings",
    patterns=[
        r"(?:open|show me|show|bring up|pull up|go to|take me to) (?:the |my )?(?:windows )?(?P<page>[a-z][a-z' &-]{0,30}?) settings(?: page)?",
        r"(?:open|show me|show|bring up|pull up|go to|take me to) (?:the )?settings (?:for|page for) (?:the |my )?(?P<page>[a-z][a-z' &-]{0,30}?)",
    ],
)
async def open_settings(ctx: SkillCtx, page: str) -> Reply:
    entry = SETTINGS_PAGES.get(_key(page))
    if entry is None:
        return Reply(text="I don't know that settings page.", success=False)
    uri_page, spoken = entry
    try:
        await asyncio.to_thread(winops.open_settings_page, uri_page)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError, OSError) as exc:
        _log.warning("open settings %s failed: %s", uri_page, exc)
        return Reply(text=f"I couldn't open {spoken} settings.", success=False)
    return Reply(text=f"Opening {spoken} settings.")
