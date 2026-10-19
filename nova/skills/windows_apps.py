"""Window & app control (2026-10-15): close / minimize / maximize / switch to
an app, act on the active window, show the desktop.

All ACT tier. Closing is always *graceful*: WM_CLOSE is posted to the app's
visible top-level windows, exactly what clicking the X does, so an app with
unsaved work gets to ask "Save changes?". Nothing here ever kills or
terminates a process.

Spoken names are matched to running windows in three steps, most specific
first:
  1. a small alias table ("word" -> WINWORD.EXE, "vs code" -> Code.exe,
     "file explorer" -> explorer.exe but ONLY folder windows of class
     CabinetWClass, never the taskbar/desktop shell);
  2. the process name itself ("spotify" -> spotify.exe, "obs" -> obs64.exe);
  3. every spoken word appearing in a window's title ("calculator" for the
     Store app hosted by ApplicationFrameHost.exe).

Protected, never closed or touched: NOVA's own process (and any python /
pythonw process running nova), and critical Windows processes.

Windows access (pywin32 / psutil / ctypes) lives in the small `_list_windows`
/ `_foreground_window` / `_post_close` / `_show` / `_activate` functions,
imported lazily, so tests monkeypatch those and never touch the real OS.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

logger = get_logger("skills.windows_apps")


@dataclass(frozen=True)
class WinInfo:
    hwnd: int
    pid: int
    exe: str          # lower-case process image name, e.g. "chrome.exe"
    title: str
    cls: str          # window class name
    protected: bool   # NOVA itself or a critical system process
    minimized: bool = False


# --- name matching -----------------------------------------------------------

# spoken name -> (display name, process names, required window class or None)
_ALIASES: dict[str, tuple[str, tuple[str, ...], str | None]] = {}


def _alias(names: str, display: str, exes: tuple[str, ...], cls: str | None = None) -> None:
    for n in names.split("|"):
        _ALIASES[n] = (display, exes, cls)


_alias("chrome|google chrome|google chrome browser", "Chrome", ("chrome.exe",))
_alias("edge|microsoft edge|edge browser", "Edge", ("msedge.exe",))
_alias("firefox|mozilla firefox|mozilla", "Firefox", ("firefox.exe",))
_alias("brave|brave browser", "Brave", ("brave.exe",))
_alias("opera|opera gx", "Opera", ("opera.exe",))
_alias("browser|web browser|the browser|internet", "the browser",
       ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"))
_alias("word|microsoft word|ms word|winword", "Word", ("winword.exe",))
_alias("excel|microsoft excel|ms excel", "Excel", ("excel.exe",))
_alias("powerpoint|power point|microsoft powerpoint|ms powerpoint", "PowerPoint", ("powerpnt.exe",))
_alias("outlook|microsoft outlook|new outlook", "Outlook", ("outlook.exe", "olk.exe"))
_alias("onenote|one note", "OneNote", ("onenote.exe",))
_alias("teams|microsoft teams|ms teams", "Teams", ("ms-teams.exe", "teams.exe"))
_alias("vs code|vscode|visual studio code|code", "VS Code", ("code.exe",))
_alias("visual studio", "Visual Studio", ("devenv.exe",))
_alias("pycharm", "PyCharm", ("pycharm64.exe", "pycharm.exe"))
_alias("file explorer|explorer|windows explorer|folder|folders|the folder|file manager",
       "File Explorer", ("explorer.exe",), "CabinetWClass")
_alias("task manager", "Task Manager", ("taskmgr.exe",))
_alias("notepad", "Notepad", ("notepad.exe",))
_alias("notepad plus plus|notepad++|notepad ++", "Notepad++", ("notepad++.exe",))
_alias("paint|ms paint|microsoft paint", "Paint", ("mspaint.exe",))
_alias("terminal|windows terminal", "Terminal", ("windowsterminal.exe",))
_alias("command prompt|cmd", "Command Prompt", ("cmd.exe",))
_alias("powershell|power shell", "PowerShell", ("powershell.exe", "pwsh.exe"))
_alias("spotify", "Spotify", ("spotify.exe",))
_alias("discord", "Discord", ("discord.exe",))
_alias("slack", "Slack", ("slack.exe",))
_alias("zoom", "Zoom", ("zoom.exe",))
_alias("whatsapp|whats app", "WhatsApp", ("whatsapp.exe", "whatsapp.root.exe"))
_alias("steam", "Steam", ("steam.exe", "steamwebhelper.exe"))
_alias("obs|obs studio", "OBS", ("obs64.exe", "obs32.exe", "obs.exe"))
_alias("vlc|vlc player|vlc media player", "VLC", ("vlc.exe",))
_alias("acrobat|adobe acrobat|acrobat reader|adobe reader", "Acrobat", ("acrobat.exe", "acrord32.exe"))
_alias("snipping tool", "Snipping Tool", ("snippingtool.exe",))
_alias("calculator|calc", "Calculator", ("calculatorapp.exe", "calc.exe"))
_alias("settings|windows settings", "Settings", ("systemsettings.exe",))
_alias("photos", "Photos", ("photos.exe", "microsoft.photos.exe"))

_PRETTY_EXE: dict[str, str] = {}
for _display, _exes, _cls in _ALIASES.values():
    for _e in _exes:
        _PRETTY_EXE.setdefault(_e, _display)

# Spoken names that mean NOVA itself.
_SELF_NAMES = {"nova", "yourself", "you", "your self", "nova app", "the assistant", "assistant"}

# Never closed, minimized or focused, whatever the user says.
_CRITICAL_EXES = frozenset({
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "sihost.exe",
    "ctfmon.exe", "taskhostw.exe", "runtimebroker.exe", "searchhost.exe", "searchapp.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "shellhost.exe", "lockapp.exe",
    "textinputhost.exe", "logonui.exe", "userinit.exe", "conhost.exe", "audiodg.exe",
    "securityhealthsystray.exe",
})
# The desktop/taskbar shell windows (explorer.exe) -- never touched.
_SHELL_CLASSES = frozenset({"shell_traywnd", "shell_secondarytraywnd", "progman", "workerw"})

_FILLER_TAIL = re.compile(r"(?: (?:app|application|program|window|windows|browser))+$")
_FILLER_HEAD = re.compile(r"^(?:the|my|our|that|this) ")
_WORDS = re.compile(r"[a-z0-9+]+")


def _clean_name(spoken: str) -> str:
    name = " ".join((spoken or "").lower().split())
    name = _FILLER_HEAD.sub("", name)
    stripped = _FILLER_TAIL.sub("", name).strip()
    return stripped or name


def _is_protected_window(w: WinInfo) -> bool:
    return w.protected or w.exe in _CRITICAL_EXES or w.cls.lower() in _SHELL_CLASSES


def _pretty(w: WinInfo) -> str:
    if w.exe in _PRETTY_EXE:
        return _PRETTY_EXE[w.exe]
    if w.exe == "explorer.exe":
        return "File Explorer"
    stem = w.exe[:-4] if w.exe.endswith(".exe") else w.exe
    if stem in ("applicationframehost", "") and w.title:
        return w.title
    return stem[:1].upper() + stem[1:] if stem else "that window"


@dataclass(frozen=True)
class _Target:
    display: str
    windows: tuple[WinInfo, ...]          # unprotected matches, in z-order (topmost first)
    protected_hit: bool = False           # something matched but it's protected
    self_hit: bool = False                # the user named NOVA itself
    ambiguous: bool = False               # title words matched several different apps


def _find(spoken: str, windows: list[WinInfo]) -> _Target:
    name = _clean_name(spoken)
    if name in _SELF_NAMES:
        return _Target(display="NOVA", windows=(), self_hit=True)

    alias = _ALIASES.get(name)
    if alias is not None:
        display, exes, cls = alias
        hits = [w for w in windows if w.exe in exes and (cls is None or w.cls == cls)]
        if hits or cls is not None:
            return _split(display, hits)
        # Store apps (Calculator, Settings, Photos) can show up under
        # ApplicationFrameHost.exe; only those may match a known name by title,
        # so "close chrome" never closes some other app whose title says "chrome".
        windows = [w for w in windows if w.exe == "applicationframehost.exe"]
    else:
        display = " ".join(word[:1].upper() + word[1:] for word in name.split()) or spoken

    compact = name.replace(" ", "")
    by_exe = [
        w for w in windows
        if (w.exe[:-4] if w.exe.endswith(".exe") else w.exe).replace(" ", "") in (compact, compact + "64")
    ]
    if by_exe:
        # explorer.exe: only folder windows, never the shell.
        by_exe = [w for w in by_exe if w.exe != "explorer.exe" or w.cls == "CabinetWClass"]
        return _split(display, by_exe)

    words = _WORDS.findall(name)
    if not words:
        return _Target(display=display, windows=())
    by_title = [w for w in windows if set(words) <= set(_WORDS.findall(w.title.lower()))]
    target = _split(display, by_title)
    if len({w.exe for w in target.windows}) > 1:
        return _Target(display=display, windows=target.windows, ambiguous=True)
    return target


def _split(display: str, hits: list[WinInfo]) -> _Target:
    ok = tuple(w for w in hits if not _is_protected_window(w))
    return _Target(display=display, windows=ok, protected_hit=bool(hits) and not ok)


# --- Windows access (lazy pywin32/psutil; monkeypatched in tests) ------------

def _win_modules():
    try:
        import psutil  # noqa: F401
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:  # not Windows, or pywin32 missing
        raise winops.WinOpsUnavailable("window control needs Windows with pywin32 and psutil") from exc
    import psutil

    return win32gui, win32con, win32process, psutil


def _is_cloaked(hwnd: int) -> bool:
    """Hidden-by-DWM windows (suspended Store apps, other virtual desktops)."""
    import ctypes

    try:
        val = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
        return val.value != 0
    except (AttributeError, OSError):
        return False


def _proc_info(pid: int, psutil, cache: dict) -> tuple[str, bool]:
    """(lower-case exe name, protected?) for a process id."""
    if pid in cache:
        return cache[pid]
    exe, protected = "", False
    try:
        proc = psutil.Process(pid)
        exe = proc.name().lower()
        if pid == os.getpid() or "nova" in exe:
            protected = True
        elif exe in ("python.exe", "pythonw.exe", "py.exe", "pyw.exe"):
            try:
                cmd = " ".join(proc.cmdline()).lower()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                cmd = ""
            # Can't see the command line -> assume it might be NOVA; leave it alone.
            protected = (not cmd) or "nova" in cmd
    except Exception:  # noqa: BLE001 -- process vanished / access denied: unknown, leave it alone
        exe, protected = "", True
    cache[pid] = (exe, protected)
    return exe, protected


def _describe(hwnd: int, win32gui, win32process, psutil, cache: dict) -> WinInfo:
    cls = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    if cls == "ApplicationFrameWindow":
        # Store apps: the frame belongs to ApplicationFrameHost.exe; the real
        # app owns the CoreWindow child (absent while minimized).
        found: list[int] = []

        def _child(ch, _):
            try:
                _t, cpid = win32process.GetWindowThreadProcessId(ch)
                if cpid != pid:
                    found.append(cpid)
            except Exception:  # noqa: BLE001
                pass
            return True

        try:
            win32gui.EnumChildWindows(hwnd, _child, None)
        except Exception:  # noqa: BLE001
            pass
        if found:
            pid = found[0]
    exe, protected = _proc_info(pid, psutil, cache)
    return WinInfo(hwnd=hwnd, pid=pid, exe=exe, title=title, cls=cls, protected=protected,
                   minimized=bool(win32gui.IsIconic(hwnd)))


def _list_windows() -> list[WinInfo]:
    """Visible, unowned, titled top-level windows, topmost first."""
    win32gui, win32con, win32process, psutil = _win_modules()
    out: list[WinInfo] = []
    cache: dict = {}

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                return True
            if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
                return True
            if not win32gui.GetWindowText(hwnd).strip() or _is_cloaked(hwnd):
                return True
            out.append(_describe(hwnd, win32gui, win32process, psutil, cache))
        except Exception:  # noqa: BLE001 -- a window closing mid-walk is normal
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def _foreground_window() -> WinInfo | None:
    win32gui, _win32con, win32process, psutil = _win_modules()
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    return _describe(hwnd, win32gui, win32process, psutil, {})


def _post_close(hwnd: int) -> None:
    """Ask a window to close (same as clicking X). Never terminates anything."""
    win32gui, win32con, _p, _ps = _win_modules()
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _show(hwnd: int, how: str) -> None:
    win32gui, win32con, _p, _ps = _win_modules()
    cmd = {"minimize": win32con.SW_MINIMIZE, "maximize": win32con.SW_MAXIMIZE,
           "restore": win32con.SW_RESTORE}[how]
    win32gui.ShowWindow(hwnd, cmd)


def _activate(hwnd: int) -> None:
    """Bring a window to the front. Windows limits focus stealing from a
    background process; a synthetic Alt tap is the standard way past it."""
    win32gui, win32con, _p, _ps = _win_modules()
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
        return
    except Exception:  # noqa: BLE001 -- pywintypes.error when focus stealing is blocked
        pass
    try:
        winops.press_keys("alt")
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)


# --- command shapes ------------------------------------------------------------

_RESERVED = (
    "this|that|it|this one|that one|them|everything|everything else|all|nothing|"
    "window|windows|the window|this window|that window|current window|the current window|"
    "active window|the active window|app|the app|this app|current app|the current app|"
    "application|this application|program|this program|"
    "computer|the computer|my computer|this computer|pc|the pc|my pc|this pc|laptop|the laptop|my laptop|"
    "machine|the machine|system|the system|desktop|the desktop|my desktop|screen|the screen|my screen|"
    "sleep|bed|home|back|full screen|fullscreen"
)
# Things other skills own: browser tabs, media, display modes, websites.
_FOREIGN = r"(?!.*\b(?:tabs?|mode|song|track|playlist|video|volume|brightness|wi ?fi|bluetooth|com|org|net|io|edu|gov|dev|dot|website|site|webpage)\b)"


def _app(tail: str = "") -> str:
    """The app-name group. `tail` is whatever the pattern requires after the
    name, so the reserved-word check ("this", "the computer", ...) applies to
    the name itself and not to the rest of the sentence."""
    return (
        rf"(?!(?:{_RESERVED}){tail}$)(?!all\b|everything\b){_FOREIGN}"
        r"(?P<app>[a-z0-9][a-z0-9 '+&-]{0,40}?)" + tail
    )


_APP = _app()
_BIGGER = r" (?:full ?screen|maximi[sz]ed)"
# Known website names belong to open_website ("go to youtube"); keep
# switch_to_app off them so the two never both match.
try:
    from nova.skills.web import SITES as _WEB_SITES
except Exception:  # noqa: BLE001 -- web skill absent/broken: just don't exclude
    _WEB_SITES = {}
_NOT_SITE = (
    rf"(?!(?:{'|'.join(re.escape(k) for k in sorted(_WEB_SITES, key=len, reverse=True))})"
    r"(?: website| site| page)?$)"
    if _WEB_SITES else ""
)
_THIS = r"(?:this|that|it|this one|that one)"
_THIS_WINDOW = (
    r"(?:(?:this|that|the current|the active|the|current|active|my) )?"
    r"(?:window|app|application|program)"
)


async def _windows() -> list[WinInfo]:
    return await asyncio.to_thread(_list_windows)


def _unavailable(verb: str) -> Reply:
    return Reply(text=f"I can't {verb} windows on this machine.", success=False)


def _not_found(t: _Target) -> Reply:
    if t.self_hit:
        return Reply(text="I can't do that to myself.", success=False)
    if t.protected_hit:
        return Reply(text=f"I'll leave {t.display} alone; Windows needs it.", success=False)
    return Reply(text=f"{t.display[:1].upper()}{t.display[1:]} isn't open.", success=False)


async def _target_for(app: str, verb: str) -> _Target | Reply:
    if not isinstance(app, str) or not app.strip():
        return Reply(text=f"Which app should I {verb}?", success=False)
    if re.search(r"\btabs?\b", app.lower()):
        return Reply(text="I can only handle whole apps and windows, not browser tabs.", success=False)
    try:
        wins = await _windows()
    except winops.WinOpsUnavailable:
        return _unavailable(verb)
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: listing windows failed: %s", verb, exc)
        return Reply(text=f"I couldn't {verb} that.", success=False)
    target = _find(app, wins)
    if not target.windows:
        return _not_found(target)
    if target.ambiguous:
        return Reply(text=f"A few different windows mention {target.display}. Which app do you mean?", success=False)
    return target


# --- skills: named app ------------------------------------------------------------

@skill(
    name="close_app",
    tier=Tier.ACT,
    examples=["close an app", "quit an app"],
    description=(
        "Close a running app's windows gracefully (like clicking X, so it can ask to save). "
        "One parameter: app (str), e.g. 'spotify', 'chrome', 'word'. Never force-kills."
    ),
    trusts_input=False,
    untrusted_output=True,
    patterns=[
        rf"(?:close|shut) (?:the |my )?{_app(' (?:down|up)')}",
        rf"(?:close out of|exit out of|get out of|shut down|close|quit|exit) (?:the |my )?{_APP}",
    ],
    confirm_phrase="close {app}",
)
async def close_app(ctx: SkillCtx, app: str) -> Reply:
    target = await _target_for(app, "close")
    if isinstance(target, Reply):
        return target
    try:
        for w in target.windows:
            await asyncio.to_thread(_post_close, w.hwnd)
    except winops.WinOpsUnavailable:
        return _unavailable("close")
    except Exception as exc:  # noqa: BLE001
        logger.error("close_app: WM_CLOSE failed: %s", exc)
        return Reply(text=f"I couldn't close {target.display}.", success=False)
    logger.info("close_app: asked %d window(s) of %s to close", len(target.windows), target.display)
    return Reply(text=f"Closing {target.display}.")


async def _show_app(app: str, how: str, verb: str, done: str) -> Reply:
    target = await _target_for(app, verb)
    if isinstance(target, Reply):
        return target
    windows = target.windows if how == "minimize" else target.windows[:1]
    try:
        for w in windows:
            await asyncio.to_thread(_show, w.hwnd, how)
        if how == "maximize":
            await asyncio.to_thread(_activate, windows[0].hwnd)
    except winops.WinOpsUnavailable:
        return _unavailable(verb)
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: ShowWindow failed: %s", verb, exc)
        return Reply(text=f"I couldn't {verb} {target.display}.", success=False)
    return Reply(text=f"{done} {target.display}.")


@skill(
    name="minimize_app",
    tier=Tier.ACT,
    examples=["minimize an app"],
    description="Minimize every window of a running app. One parameter: app (str), e.g. 'spotify'.",
    trusts_input=False,
    untrusted_output=True,
    patterns=[rf"minimi[sz]e (?:the |my )?{_APP}"],
    confirm_phrase="minimize {app}",
)
async def minimize_app(ctx: SkillCtx, app: str) -> Reply:
    return await _show_app(app, "minimize", "minimize", "Minimized")


@skill(
    name="maximize_app",
    tier=Tier.ACT,
    examples=["maximize an app"],
    description="Maximize a running app's front window and bring it forward. One parameter: app (str).",
    trusts_input=False,
    untrusted_output=True,
    patterns=[
        rf"(?:maximi[sz]e|expand|enlarge) (?:the |my )?{_APP}",
        rf"make (?:the |my )?{_app(_BIGGER)}",
    ],
    confirm_phrase="maximize {app}",
)
async def maximize_app(ctx: SkillCtx, app: str) -> Reply:
    return await _show_app(app, "maximize", "maximize", "Maximized")


@skill(
    name="switch_to_app",
    tier=Tier.ACT,
    examples=["switch to another app", "bring an app to the front"],
    description=(
        "Bring an already-running app's window to the front (does not launch anything). "
        "One parameter: app (str)."
    ),
    trusts_input=False,
    untrusted_output=True,
    patterns=[
        rf"(?:switch|swap|change|flip|tab|alt tab) (?:over |back )?to {_NOT_SITE}(?:the |my )?{_NOT_SITE}{_APP}",
        rf"(?:go|jump|take me|get me) (?:back |over )?to {_NOT_SITE}(?:the |my )?(?!settings$|.*\bsettings$){_NOT_SITE}{_APP}",
        rf"(?:bring up|bring back|bring forward|focus on|focus|activate|restore) {_NOT_SITE}(?:the |my )?{_NOT_SITE}{_APP}",
        rf"bring {_NOT_SITE}(?:the |my )?{_NOT_SITE}{_app(' (?:up|back|forward|to the front|to front)')}",
    ],
    confirm_phrase="switch to {app}",
)
async def switch_to_app(ctx: SkillCtx, app: str) -> Reply:
    target = await _target_for(app, "switch to")
    if isinstance(target, Reply):
        return target
    try:
        await asyncio.to_thread(_activate, target.windows[0].hwnd)
    except winops.WinOpsUnavailable:
        return _unavailable("switch")
    except Exception as exc:  # noqa: BLE001
        logger.error("switch_to_app: activate failed: %s", exc)
        return Reply(text=f"I couldn't bring up {target.display}.", success=False)
    return Reply(text=f"Here's {target.display}.")


# --- skills: the active window / desktop -------------------------------------------

async def _active(verb: str) -> WinInfo | Reply:
    try:
        w = await asyncio.to_thread(_foreground_window)
    except winops.WinOpsUnavailable:
        return _unavailable(verb)
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: reading the active window failed: %s", verb, exc)
        return Reply(text=f"I couldn't {verb} that window.", success=False)
    if w is None:
        return Reply(text="There's no active window.", success=False)
    if w.pid == os.getpid() or (w.protected and ("python" in w.exe or "nova" in w.exe)):
        return Reply(text="That's my own window, so I'll leave it.", success=False)
    if _is_protected_window(w):
        return Reply(text="That's part of Windows, so I'll leave it.", success=False)
    return w


@skill(
    name="close_window",
    tier=Tier.ACT,
    examples=["close the active window", "close the current window"],
    description="Close the currently active (foreground) window gracefully, like clicking X.",
    trusts_input=False,
    untrusted_output=True,
    patterns=[rf"(?:close|quit|exit) {_THIS}", rf"(?:close|quit|exit) {_THIS_WINDOW}"],
    confirm_phrase="close the active window",
)
async def close_window(ctx: SkillCtx) -> Reply:
    w = await _active("close")
    if isinstance(w, Reply):
        return w
    try:
        await asyncio.to_thread(_post_close, w.hwnd)
    except winops.WinOpsUnavailable:
        return _unavailable("close")
    except Exception as exc:  # noqa: BLE001
        logger.error("close_window: WM_CLOSE failed: %s", exc)
        return Reply(text="I couldn't close that window.", success=False)
    return Reply(text=f"Closing {_pretty(w)}.")


@skill(
    name="minimize_window",
    tier=Tier.ACT,
    examples=["minimize this window", "minimize this", "minimize the window"],
    description="Minimize the currently active (foreground) window.",
    trusts_input=False,
    untrusted_output=True,
    patterns=[rf"(?:minimi[sz]e|hide) {_THIS}", rf"(?:minimi[sz]e|hide) {_THIS_WINDOW}"],
)
async def minimize_window(ctx: SkillCtx) -> Reply:
    return await _show_active("minimize", "Minimized.")


@skill(
    name="maximize_window",
    tier=Tier.ACT,
    examples=["maximize this window", "maximize this", "make this full screen"],
    description="Maximize the currently active (foreground) window.",
    trusts_input=False,
    untrusted_output=True,
    patterns=[
        rf"(?:maximi[sz]e|expand|enlarge) {_THIS}",
        rf"(?:maximi[sz]e|expand|enlarge) {_THIS_WINDOW}",
        rf"make (?:{_THIS}|{_THIS_WINDOW}){_BIGGER}",
    ],
)
async def maximize_window(ctx: SkillCtx) -> Reply:
    return await _show_active("maximize", "Maximized.")


async def _show_active(how: str, done: str) -> Reply:
    w = await _active(how)
    if isinstance(w, Reply):
        return w
    try:
        await asyncio.to_thread(_show, w.hwnd, how)
    except winops.WinOpsUnavailable:
        return _unavailable(how)
    except Exception as exc:  # noqa: BLE001
        logger.error("%s_window: ShowWindow failed: %s", how, exc)
        return Reply(text=f"I couldn't {how} that window.", success=False)
    return Reply(text=done)


@skill(
    name="show_desktop",
    tier=Tier.ACT,
    examples=["show the desktop", "minimize everything", "minimize all windows"],
    description="Minimize all windows and show the desktop (Win+D; say it again to bring them back).",
    trusts_input=False,
    untrusted_output=True,
    patterns=[
        r"(?:minimi[sz]e|hide) (?:everything|all|all (?:the |my |of the |of my )?(?:windows|apps)|all of them)",
        r"show (?:me )?(?:the |my )?desktop",
        r"(?:go|take me|switch|get me) (?:back )?to (?:the |my )?desktop",
        r"clear (?:the |my )?(?:screen|desktop) of (?:everything|all windows)",
    ],
)
async def show_desktop(ctx: SkillCtx) -> Reply:
    try:
        await asyncio.to_thread(winops.press_keys, "win", "d")
    except winops.WinOpsUnavailable:
        return _unavailable("hide")
    except Exception as exc:  # noqa: BLE001
        logger.error("show_desktop: Win+D failed: %s", exc)
        return Reply(text="I couldn't show the desktop.", success=False)
    return Reply(text="Here's your desktop.")
