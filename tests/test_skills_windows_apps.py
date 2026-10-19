"""Window & app control skills (nova/skills/windows_apps.py).

Every Windows call is replaced: `_list_windows`, `_foreground_window`,
`_post_close`, `_show`, `_activate` and `winops.press_keys` are monkeypatched,
so nothing here touches a real window. Skills run through `spec.invoke()` so
the permission ladder and audit log are exercised too.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

import nova.skills  # noqa: F401 -- populate the registry
import nova.skills.windows_apps as wa
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills.registry import SkillCtx, get_skill
from nova.system import winops

W = wa.WinInfo
OWN = os.getpid()

DESKTOP = [
    W(hwnd=1, pid=100, exe="chrome.exe", title="Inbox - Google Chrome", cls="Chrome_WidgetWin_1", protected=False),
    W(hwnd=2, pid=200, exe="notepad.exe", title="todo.txt - Notepad", cls="Notepad", protected=False),
    W(hwnd=3, pid=100, exe="chrome.exe", title="GitHub - Google Chrome", cls="Chrome_WidgetWin_1", protected=False),
    W(hwnd=4, pid=300, exe="winword.exe", title="Report.docx - Word", cls="OpusApp", protected=False),
    W(hwnd=5, pid=400, exe="explorer.exe", title="Downloads", cls="CabinetWClass", protected=False),
    W(hwnd=6, pid=400, exe="explorer.exe", title="Taskbar", cls="Shell_TrayWnd", protected=False),
    W(hwnd=7, pid=500, exe="code.exe", title="app.py - nova - Visual Studio Code", cls="Chrome_WidgetWin_1",
      protected=False),
    W(hwnd=8, pid=600, exe="applicationframehost.exe", title="Calculator", cls="ApplicationFrameWindow",
      protected=False),
    W(hwnd=9, pid=OWN, exe="pythonw.exe", title="NOVA", cls="TkTopLevel", protected=True),
    W(hwnd=10, pid=700, exe="obs64.exe", title="OBS 30.1 - Profile: Untitled", cls="Qt5QWindowIcon", protected=False),
    W(hwnd=11, pid=800, exe="taskmgr.exe", title="Task Manager", cls="TaskManagerWindow", protected=False),
]


@pytest.fixture
def desk(monkeypatch):
    """A fake desktop. Records every close/show/activate call."""
    state = {"windows": list(DESKTOP), "fg": DESKTOP[1], "closed": [], "shown": [], "activated": [], "keys": []}
    monkeypatch.setattr(wa, "_list_windows", lambda: list(state["windows"]))
    monkeypatch.setattr(wa, "_foreground_window", lambda: state["fg"])
    monkeypatch.setattr(wa, "_post_close", lambda hwnd: state["closed"].append(hwnd))
    monkeypatch.setattr(wa, "_show", lambda hwnd, how: state["shown"].append((hwnd, how)))
    monkeypatch.setattr(wa, "_activate", lambda hwnd: state["activated"].append(hwnd))
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: state["keys"].append(k))
    return state


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


async def run(ctx, name, **params):
    spec = get_skill(name)
    assert spec is not None
    return await spec.invoke(ctx, **params)


# --- registration ----------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "close_app", "close_window", "minimize_app", "maximize_app", "switch_to_app",
    "minimize_window", "maximize_window", "show_desktop",
])
def test_registered_as_act_tier(name):
    spec = get_skill(name)
    assert spec is not None and spec.tier is Tier.ACT and spec.trusts_input is False


def test_close_app_confirm_phrase():
    assert get_skill("close_app").confirm_phrase.format(app="spotify") == "close spotify"


# --- routing -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def router():
    return Router()


@pytest.mark.parametrize("sentence, skill, params", [
    ("close notepad", "close_app", {"app": "notepad"}),
    ("Close Spotify.", "close_app", {"app": "spotify"}),
    ("quit chrome", "close_app", {"app": "chrome"}),
    ("exit word", "close_app", {"app": "word"}),
    ("can you close spotify for me please", "close_app", {"app": "spotify"}),
    ("exit out of word", "close_app", {"app": "word"}),
    ("close chrome down", "close_app", {"app": "chrome"}),
    ("shut down chrome", "close_app", {"app": "chrome"}),
    ("close file explorer", "close_app", {"app": "file explorer"}),
    ("close vs code", "close_app", {"app": "vs code"}),
    ("close this", "close_window", {}),
    ("close it", "close_window", {}),
    ("close the window", "close_window", {}),
    ("close this window", "close_window", {}),
    ("close the current window", "close_window", {}),
    ("close this app", "close_window", {}),
    ("minimize spotify", "minimize_app", {"app": "spotify"}),
    ("minimise the chrome window", "minimize_app", {"app": "chrome window"}),
    ("maximize chrome", "maximize_app", {"app": "chrome"}),
    ("make chrome full screen", "maximize_app", {"app": "chrome"}),
    ("switch to notepad", "switch_to_app", {"app": "notepad"}),
    ("go to chrome", "switch_to_app", {"app": "chrome"}),
    ("bring up spotify", "switch_to_app", {"app": "spotify"}),
    ("bring spotify up", "switch_to_app", {"app": "spotify"}),
    ("focus on word", "switch_to_app", {"app": "word"}),
    ("switch over to visual studio code", "switch_to_app", {"app": "visual studio code"}),
    ("minimize this window", "minimize_window", {}),
    ("minimize this", "minimize_window", {}),
    ("hide this window", "minimize_window", {}),
    ("maximize this", "maximize_window", {}),
    ("make this full screen", "maximize_window", {}),
    ("minimize everything", "show_desktop", {}),
    ("minimize all windows", "show_desktop", {}),
    ("show desktop", "show_desktop", {}),
    ("show me the desktop", "show_desktop", {}),
    ("go to the desktop", "show_desktop", {}),
])
def test_routes(router, sentence, skill, params):
    m = router.route(sentence)
    assert m.spec is not None, m.clarification
    assert m.spec.name == skill
    assert m.params == params


@pytest.mark.parametrize("sentence, owner", [
    ("open notepad", "open_app"),
    ("go to youtube", "open_website"),
    ("go to my email", "open_website"),
    ("go to sleep", "sleep_computer"),
    ("shut down", "shutdown_computer"),
    ("shut it down", "shutdown_computer"),
    ("shut down the computer", "shutdown_computer"),
    ("lock the computer", "lock_computer"),
    ("what time is it", "time"),
])
def test_near_misses_go_to_their_own_skill(router, sentence, owner):
    m = router.route(sentence)
    assert m.spec is not None, m.clarification
    assert m.spec.name == owner


@pytest.mark.parametrize("sentence", [
    "open spotify", "launch chrome", "go to the youtube website", "show me the weather",
    "close the computer", "turn off wifi", "next track",
])
def test_never_stolen_by_window_skills(router, sentence):
    mine = {"close_app", "close_window", "minimize_app", "maximize_app", "switch_to_app",
            "minimize_window", "maximize_window", "show_desktop"}
    m = router.route(sentence)
    assert m.spec is None or m.spec.name not in mine, (sentence, m.spec.name, m.params)


# --- matching ---------------------------------------------------------------------

@pytest.mark.parametrize("spoken, hwnds, display", [
    ("chrome", [1, 3], "Chrome"),
    ("google chrome", [1, 3], "Chrome"),
    ("word", [4], "Word"),
    ("the word app", [4], "Word"),
    ("file explorer", [5], "File Explorer"),       # never the taskbar (hwnd 6)
    ("explorer", [5], "File Explorer"),
    ("vs code", [7], "VS Code"),
    ("visual studio code", [7], "VS Code"),
    ("calculator", [8], "Calculator"),            # Store app, matched by title
    ("obs", [10], "OBS"),
    ("notepad", [2], "Notepad"),
    ("task manager", [11], "Task Manager"),
])
def test_find_matches_the_right_windows(spoken, hwnds, display):
    t = wa._find(spoken, list(DESKTOP))
    assert [w.hwnd for w in t.windows] == hwnds
    assert t.display == display


def test_find_by_process_name_when_not_in_alias_table():
    wins = [W(hwnd=1, pid=1, exe="obsidian.exe", title="Vault", cls="X", protected=False)]
    t = wa._find("obsidian", wins)
    assert [w.hwnd for w in t.windows] == [1] and t.display == "Obsidian"


def test_known_app_name_never_matches_another_apps_title():
    wins = [W(hwnd=1, pid=1, exe="code.exe", title="chrome extension - Visual Studio Code", cls="C", protected=False)]
    assert wa._find("chrome", wins).windows == ()


def test_find_by_title_words():
    t = wa._find("report docx", list(DESKTOP))
    assert [w.hwnd for w in t.windows] == [4]


def test_title_match_across_several_apps_is_ambiguous():
    wins = [
        W(hwnd=1, pid=1, exe="chrome.exe", title="Budget - Google Chrome", cls="C", protected=False),
        W(hwnd=2, pid=2, exe="excel.exe", title="Budget.xlsx - Excel", cls="XLMAIN", protected=False),
    ]
    assert wa._find("budget", wins).ambiguous is True


def test_explorer_shell_is_never_a_target():
    shell_only = [w for w in DESKTOP if w.hwnd == 6]
    t = wa._find("explorer", shell_only)
    assert t.windows == ()


@pytest.mark.parametrize("spoken", ["nova", "yourself", "you"])
def test_nova_itself_is_never_a_target(spoken):
    assert wa._find(spoken, list(DESKTOP)).self_hit is True


def test_protected_process_matched_by_name_is_refused():
    t = wa._find("pythonw", list(DESKTOP))
    assert t.windows == () and t.protected_hit is True


def test_critical_process_is_refused_even_if_not_flagged():
    wins = [W(hwnd=1, pid=1, exe="winlogon.exe", title="Winlogon", cls="X", protected=False)]
    t = wa._find("winlogon", wins)
    assert t.windows == () and t.protected_hit is True


# --- close_app ------------------------------------------------------------------------

async def test_close_app_posts_wm_close_to_every_window_of_the_app(desk, ctx):
    reply = await run(ctx, "close_app", app="chrome")
    assert reply.success and reply.text == "Closing Chrome."
    assert desk["closed"] == [1, 3]


async def test_close_app_not_open(desk, ctx):
    reply = await run(ctx, "close_app", app="spotify")
    assert not reply.success and reply.text == "Spotify isn't open."
    assert desk["closed"] == []


async def test_close_app_refuses_nova(desk, ctx):
    reply = await run(ctx, "close_app", app="nova")
    assert not reply.success and "myself" in reply.text
    assert desk["closed"] == []


async def test_close_app_refuses_protected(desk, ctx):
    reply = await run(ctx, "close_app", app="python w")
    assert desk["closed"] == []
    assert not reply.success


async def test_close_app_ambiguous_asks(desk, ctx):
    desk["windows"] = [
        W(hwnd=1, pid=1, exe="chrome.exe", title="Budget - Google Chrome", cls="C", protected=False),
        W(hwnd=2, pid=2, exe="excel.exe", title="Budget.xlsx - Excel", cls="XLMAIN", protected=False),
    ]
    reply = await run(ctx, "close_app", app="budget")
    assert not reply.success and "Which app" in reply.text
    assert desk["closed"] == []


@pytest.mark.parametrize("sentence", ["close this tab", "close the tab", "close tab"])
def test_closing_a_tab_never_closes_the_window(router, sentence):
    m = router.route(sentence)
    assert m.spec is None or m.spec.name != "close_window"


async def test_close_app_refuses_tabs(desk, ctx):
    reply = await run(ctx, "close_app", app="all tabs")
    assert not reply.success and "tabs" in reply.text
    assert desk["closed"] == []


async def test_close_the_browser_closes_any_browser(desk, ctx):
    reply = await run(ctx, "close_app", app="browser")
    assert reply.success and desk["closed"] == [1, 3]


async def test_close_app_empty_name(desk, ctx):
    reply = await run(ctx, "close_app", app="  ")
    assert not reply.success and desk["closed"] == []


async def test_close_app_post_failure_is_spoken_honestly(desk, ctx, monkeypatch):
    def boom(hwnd):
        raise OSError("access denied")
    monkeypatch.setattr(wa, "_post_close", boom)
    reply = await run(ctx, "close_app", app="notepad")
    assert not reply.success and reply.text == "I couldn't close Notepad."


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows fallback")
async def test_close_app_without_windows_apis_says_so(ctx):
    reply = await run(ctx, "close_app", app="notepad")
    assert not reply.success and reply.text == "I can't close windows on this machine."


async def test_close_app_is_audited(desk, ctx, tmp_path):
    await run(ctx, "close_app", app="chrome")
    status = AuditLog(path=tmp_path / "audit.log").verify()
    assert status.ok and status.entries == 1


# --- close_window --------------------------------------------------------------------

async def test_close_window_closes_the_foreground_window(desk, ctx):
    reply = await run(ctx, "close_window")
    assert reply.success and reply.text == "Closing Notepad."
    assert desk["closed"] == [2]


async def test_close_window_refuses_novas_own_window(desk, ctx):
    desk["fg"] = DESKTOP[8]
    reply = await run(ctx, "close_window")
    assert not reply.success and "my own window" in reply.text
    assert desk["closed"] == []


async def test_close_window_refuses_the_taskbar(desk, ctx):
    desk["fg"] = DESKTOP[5]
    reply = await run(ctx, "close_window")
    assert not reply.success and desk["closed"] == []


async def test_close_window_with_nothing_active(desk, ctx):
    desk["fg"] = None
    reply = await run(ctx, "close_window")
    assert not reply.success and desk["closed"] == []


# --- minimize / maximize / switch ---------------------------------------------------

async def test_minimize_app_minimizes_all_its_windows(desk, ctx):
    reply = await run(ctx, "minimize_app", app="chrome")
    assert reply.text == "Minimized Chrome."
    assert desk["shown"] == [(1, "minimize"), (3, "minimize")]


async def test_maximize_app_maximizes_the_front_window_and_focuses_it(desk, ctx):
    reply = await run(ctx, "maximize_app", app="chrome")
    assert reply.text == "Maximized Chrome."
    assert desk["shown"] == [(1, "maximize")]
    assert desk["activated"] == [1]


async def test_switch_to_app_activates_topmost_window(desk, ctx):
    reply = await run(ctx, "switch_to_app", app="vs code")
    assert reply.success and reply.text == "Here's VS Code."
    assert desk["activated"] == [7]


async def test_switch_to_app_not_running(desk, ctx):
    reply = await run(ctx, "switch_to_app", app="discord")
    assert not reply.success and reply.text == "Discord isn't open."
    assert desk["activated"] == []


async def test_switch_to_app_activation_failure(desk, ctx, monkeypatch):
    def boom(hwnd):
        raise RuntimeError("SetForegroundWindow failed")
    monkeypatch.setattr(wa, "_activate", boom)
    reply = await run(ctx, "switch_to_app", app="notepad")
    assert not reply.success and reply.text == "I couldn't bring up Notepad."


async def test_minimize_and_maximize_active_window(desk, ctx):
    assert (await run(ctx, "minimize_window")).text == "Minimized."
    assert (await run(ctx, "maximize_window")).text == "Maximized."
    assert desk["shown"] == [(2, "minimize"), (2, "maximize")]


async def test_show_desktop_presses_win_d(desk, ctx):
    reply = await run(ctx, "show_desktop")
    assert reply.success and reply.text == "Here's your desktop."
    assert desk["keys"] == [("win", "d")]


async def test_show_desktop_unavailable(ctx, monkeypatch):
    def unavailable(*a, **k):
        raise winops.WinOpsUnavailable("no")
    monkeypatch.setattr(winops, "press_keys", unavailable)
    reply = await run(ctx, "show_desktop")
    assert not reply.success


# --- process info: NOVA is recognised ---------------------------------------------------

class _FakeProc:
    def __init__(self, pid, name, cmd):
        self.pid, self._name, self._cmd = pid, name, cmd

    def name(self):
        return self._name

    def cmdline(self):
        if self._cmd is None:
            raise _FakePsutil.AccessDenied()
        return self._cmd


class _FakePsutil:
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    procs: dict = {}

    @classmethod
    def Process(cls, pid):  # noqa: N802 -- mirrors psutil
        if pid not in cls.procs:
            raise cls.NoSuchProcess()
        return cls.procs[pid]


@pytest.mark.parametrize("pid, name, cmd, protected", [
    (OWN, "pythonw.exe", ["pythonw", "-m", "nova"], True),
    (11, "pythonw.exe", ["pythonw", "D:\\NOVA\\run.py"], True),
    (12, "python.exe", ["python", "C:\\scripts\\other.py"], False),
    (13, "python.exe", None, True),             # can't see cmdline: leave it alone
    (14, "spotify.exe", ["spotify"], False),
    (15, "nova.exe", ["nova"], True),
])
def test_proc_info_flags_nova(pid, name, cmd, protected):
    _FakePsutil.procs = {pid: _FakeProc(pid, name, cmd)}
    exe, prot = wa._proc_info(pid, _FakePsutil, {})
    assert exe == name and prot is protected


def test_proc_info_vanished_process_is_protected():
    _FakePsutil.procs = {}
    assert wa._proc_info(999, _FakePsutil, {}) == ("", True)


# --- source guarantees -----------------------------------------------------------------

def test_module_never_terminates_or_kills_processes():
    src = Path(wa.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"terminate", "kill", "TerminateProcess", "taskkill"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (attrs | names) & banned
    assert "taskkill" not in src.lower()
