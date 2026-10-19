"""The single sanctioned doorway from skills to Windows (2026-10-15).

Skills never import subprocess/os.startfile themselves. They call these
functions, which keep three promises:

1. **No user text inside a command line.** PowerShell scripts are constants
   written in the skill's source; anything derived from speech is passed as
   JSON data and read inside the script as `$NovaArgs.<name>` (lower-
   case). Values are length-capped and control characters are refused, so
   nothing a person says can become code.
2. **Only https URLs reach the browser**, and only ones whose host is a
   plain DNS name (no credentials, no IP literals, no local/UNC tricks).
3. **Everything is a thin, mockable function** -- skill tests monkeypatch
   these and never touch the real machine.

Windows-only at run time; on other platforms each call raises
`WinOpsUnavailable`, which skills turn into an honest spoken refusal.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import time

from nova.log import get_logger

_log = get_logger("system.winops")

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
_MAX_ARG = 300
_ARG_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,30}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})+$")


class WinOpsUnavailable(RuntimeError):
    """Not on Windows (or the needed Windows API is missing)."""


class WinOpsError(RuntimeError):
    """The OS call ran and failed."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise WinOpsUnavailable("this only works on Windows")


def _clean_arg(name: str, value: object) -> str:
    if not _ARG_NAME.match(name):
        raise ValueError(f"bad argument name {name!r}")
    text = str(value)
    if len(text) > _MAX_ARG:
        raise ValueError(f"argument {name} too long")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError(f"argument {name} contains control characters")
    return text


def powershell(script: str, *, timeout: float = 15.0, **args: object) -> str:
    """Run a constant PowerShell `script` (never built from user text).
    `args` arrive inside it as `$NovaArgs.<name>`. Returns stdout (stripped).
    Raises WinOpsError on non-zero exit or timeout."""
    _require_windows()
    payload = json.dumps({k.lower(): _clean_arg(k.upper(), v) for k, v in args.items()})
    encoded = base64.b64encode(build_ps_command(script, payload).encode("utf-16-le")).decode("ascii")
    argv = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WinOpsError("PowerShell timed out") from exc
    except OSError as exc:
        raise WinOpsError(f"PowerShell could not start: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise WinOpsError(err[-1] if err else f"PowerShell exit {proc.returncode}")
    return (proc.stdout or "").strip()


def build_ps_command(script: str, payload: str) -> str:
    """The full script PowerShell runs (-EncodedCommand, so multi-line
    scripts parse exactly like a .ps1 file). The JSON args sit in a single-
    quoted here-string, which expands nothing; json.dumps output is one line
    starting with "{", so it can never contain the closing "'@" line."""
    if "\n" in payload or "\r" in payload:
        raise ValueError("payload must be one line")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$NovaArgs = @'\n" + payload + "\n'@ | ConvertFrom-Json\n"
        + script + "\n"
    )


def _system32() -> str:
    buf = ctypes.create_unicode_buffer(260)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260):
        raise WinOpsError("GetSystemDirectoryW failed")
    return buf.value


def run_program(exe: str, *program_args: str, timeout: float = 15.0) -> str:
    """Run a Windows system program from System32 by bare name (e.g.
    "shutdown.exe") with fixed arguments. Never a shell."""
    _require_windows()
    if not re.fullmatch(r"[a-z0-9_-]+\.exe", exe):
        raise ValueError(f"not a bare program name: {exe!r}")
    system32 = _system32()
    path = os.path.realpath(os.path.join(system32, exe))
    if os.path.dirname(path).lower() != os.path.realpath(system32).lower() or not os.path.isfile(path):
        raise WinOpsError(f"{exe} is not a System32 program")
    for a in program_args:
        _clean_arg("ARG", a)
    try:
        proc = subprocess.run(
            [path, *program_args], capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WinOpsError(f"{exe} failed: {exc}") from exc
    if proc.returncode != 0:
        raise WinOpsError((proc.stderr or proc.stdout or f"{exe} exit {proc.returncode}").strip())
    return (proc.stdout or "").strip()


_URL = re.compile(r"^https://(?P<host>[^/?#:@\\]+)(?::443)?(?P<rest>[/?#][^\\\s]*)?$", re.IGNORECASE)


def safe_https_url(url: str) -> str | None:
    """`url` if it is an https URL to a plain public-looking hostname, else None.
    (Parsed with a strict regex: urllib is a transport module and is kept
    out of everything but nova/security/egress.py.)"""
    text = (url or "").strip()
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in text):
        return None
    m = _URL.match(text)
    if m is None:
        return None
    host = m.group("host").lower()
    if not _HOST.match(host) or host.endswith((".local", ".internal", ".lan", ".localhost")):
        return None
    if re.fullmatch(r"[0-9.]+", host):
        return None
    # Browsers (WHATWG URL "ends in a number") parse a host whose last label
    # is decimal or 0x-hex as IPv4: "0x7f.0x0.0x0.0x1" is 127.0.0.1.
    if re.fullmatch(r"0x[0-9a-f]*|[0-9]+", host.rsplit(".", 1)[-1]):
        return None
    return text


def open_url(url: str) -> None:
    """Open an https URL in the default browser (see safe_https_url)."""
    _require_windows()
    ok = safe_https_url(url)
    if ok is None:
        raise ValueError(f"refusing to open {url!r}: not a plain https URL")
    os.startfile(ok)  # noqa: S606 -- validated https URL only


#: spoken browser name -> its executable, looked up in the Windows
#: "App Paths" registry (where installers register them).
BROWSERS = {
    "brave": "brave.exe", "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe", "firefox": "firefox.exe",
    "opera": "opera.exe", "vivaldi": "vivaldi.exe",
}


def browser_exe(browser: str) -> str | None:
    """Full path of an installed browser, or None."""
    _require_windows()
    exe = BROWSERS.get(" ".join(browser.lower().split()))
    if exe is None:
        return None
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                path, _ = winreg.QueryValueEx(k, "")
        except OSError:
            continue
        path = os.path.realpath(str(path).strip('"'))
        if os.path.basename(path).lower() == exe and os.path.isfile(path):
            return path
    return None


def open_url_in(browser: str, url: str) -> None:
    """Open a validated https URL in a specific installed browser."""
    _require_windows()
    ok = safe_https_url(url)
    if ok is None:
        raise ValueError(f"refusing to open {url!r}: not a plain https URL")
    exe = browser_exe(browser)
    if exe is None:
        raise WinOpsError(f"{browser} isn't installed")
    try:
        subprocess.Popen([exe, ok], shell=False, creationflags=_NO_WINDOW)  # noqa: S603 -- fixed exe, validated URL
    except OSError as exc:
        raise WinOpsError(f"couldn't start {browser}: {exc}") from exc


def open_settings_page(page: str) -> None:
    """Open a Windows Settings page, e.g. "network-wifi" -> ms-settings:network-wifi."""
    _require_windows()
    if not re.fullmatch(r"[a-z0-9-]{1,60}", page):
        raise ValueError(f"bad settings page {page!r}")
    os.startfile(f"ms-settings:{page}")  # noqa: S606


_APP_ID = re.compile(r"^[A-Za-z0-9 ._!{}\\()+&,'~-]{1,300}$")


def launch_start_app(app_id: str) -> None:
    """Launch an installed app by the AppUserModelID Windows itself lists
    in Get-StartApps (shell:AppsFolder). The id always comes from that
    Windows list, never from speech; it is still checked for shape."""
    _require_windows()
    if not _APP_ID.match(app_id or "") or ".." in app_id or "://" in app_id or app_id.startswith("\\"):
        raise ValueError(f"not a Start-menu app id: {app_id!r}")
    os.startfile("shell:AppsFolder\\" + app_id)  # noqa: S606


# --- keyboard -------------------------------------------------------------

VK = {
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    "win": 0x5B, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "tab": 0x09, "d": 0x44, "s": 0x53,
    "snapshot": 0x2C, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "f4": 0x73,
}
_KEYUP = 0x0002


def press_keys(*names: str, repeat: int = 1) -> None:
    """Press a key chord (held in order, released in reverse), `repeat` times."""
    _require_windows()
    codes = [VK[n] for n in names]
    user32 = ctypes.windll.user32
    for _ in range(max(1, min(repeat, 100))):
        for c in codes:
            user32.keybd_event(c, 0, 0, 0)
        for c in reversed(codes):
            user32.keybd_event(c, 0, _KEYUP, 0)
        time.sleep(0.01)


def lock_workstation() -> None:
    _require_windows()
    if not ctypes.windll.user32.LockWorkStation():
        raise WinOpsError("LockWorkStation failed")


def sleep_computer() -> None:
    _require_windows()
    # SetSuspendState(hibernate=False, force=False, disableWakeEvents=False)
    if not ctypes.windll.powrprof.SetSuspendState(False, False, False):
        raise WinOpsError("SetSuspendState failed")
