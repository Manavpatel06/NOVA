"""Contract: nova/interfaces/tray.py (T1.4) — system tray icon + single-
instance guard.

    def acquire_single_instance_lock(name: str = _MUTEX_NAME) -> bool
    class Tray:
        def __init__(self, bus=None, *, on_quit=None)
        def start(self, loop=None) -> None
        def stop(self) -> None
        def set_state(self, state: TrayState) -> None

Zero coverage before this file despite ~430 lines and being wired into
NovaApp's boot sequence. Three promises named explicitly:

  1. The single-instance mutex guard works INDEPENDENTLY of the tray icon
     backend existing at all -- "two NOVAs fighting over one microphone" is
     a correctness bug the guard must prevent even if the tray icon (a
     cosmetic feature) fails entirely. Pinned structurally: the guard only
     ever imports win32event/win32api/winerror, never win32gui/win32con, so
     a test that breaks win32gui/win32con while leaving the mutex packages
     intact and shows the guard still works proves the independence, not
     just asserts it.
  2. Bus-driven state transitions follow the documented mapping
     (ptt.start->LISTENING, turn.start->THINKING, speak.start->SPEAKING,
     speak.end->THINKING, turn.end->IDLE) and keep working even when the
     tray backend never came up (set_state() no-ops the native call but
     still updates self.state).
  3. A missing/failed pywin32 backend degrades loudly (WARNING naming the
     failure) and never blocks NOVA from continuing -- start() returns
     promptly with backend_available=False rather than raising or hanging.

pywin32 (win32gui/win32api/win32con/win32event/winerror) is a real,
installed dependency on this machine and every internal import in
tray.py is local (inside the function that needs it, never at module
top-level), so every test here injects fakes via `sys.modules` rather than
touching the real GUI -- same technique as
test_interfaces_hotkey.py's `fake_keyboard_module` fixture. `_wnd_proc` is
called directly with synthetic message args, the same way that file calls
`_on_event`/`_extract_combo_event` directly, rather than driving a
simulated OS message loop.

A small opt-in real-hardware section (NOVA_TEST_REAL_HARDWARE=1) exercises
the actual Win32 mutex and a real Tray start()/stop() cycle on this
machine's real interactive desktop session -- the module docstring notes
`GetDC(0)` succeeds here, unlike the retired build's headless dev box.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest

import nova.interfaces.tray as tray_mod
from nova.bus import EventBus
from nova.governor import TOPIC_TURN_END, TOPIC_TURN_START
from nova.interfaces.hotkey import TOPIC_PTT_START
from nova.interfaces.tray import (
    TOPIC_SPEAK_END,
    TOPIC_SPEAK_START,
    Tray,
    TrayState,
    acquire_single_instance_lock,
)


# ---------------------------------------------------------------------------
# Fakes for the single-instance mutex (win32event/win32api/winerror only --
# deliberately NOT win32gui/win32con, so tests can prove the guard's
# independence from the tray-icon packages by construction).
# ---------------------------------------------------------------------------


class _FakeWin32Event:
    def __init__(self, create_raises=None):
        self._create_raises = create_raises
        self.handle = object()
        self.create_calls: list[str] = []

    def CreateMutex(self, security, initial_owner, name):
        self.create_calls.append(name)
        if self._create_raises is not None:
            raise self._create_raises
        return self.handle


class _FakeWin32ApiForMutex:
    def __init__(self, last_error=0):
        self.last_error = last_error
        self.closed_handles: list = []

    def GetLastError(self):
        return self.last_error

    def CloseHandle(self, handle):
        self.closed_handles.append(handle)


class _FakeWinerror:
    ERROR_ALREADY_EXISTS = 183  # real Win32 value


@pytest.fixture
def fake_win32_mutex(monkeypatch):
    def _install(last_error=0, create_raises=None):
        win32event = _FakeWin32Event(create_raises=create_raises)
        win32api = _FakeWin32ApiForMutex(last_error=last_error)
        monkeypatch.setitem(sys.modules, "win32event", win32event)
        monkeypatch.setitem(sys.modules, "win32api", win32api)
        monkeypatch.setitem(sys.modules, "winerror", _FakeWinerror)
        return win32event, win32api

    yield _install
    tray_mod._release_single_instance_lock_for_tests()


# ---------------------------------------------------------------------------
# acquire_single_instance_lock()
# ---------------------------------------------------------------------------


def test_acquire_lock_succeeds_when_nothing_else_holds_it(fake_win32_mutex):
    fake_win32_mutex(last_error=0)
    assert acquire_single_instance_lock("TestMutexA") is True


def test_acquire_lock_fails_and_releases_the_duplicate_handle_when_already_held(fake_win32_mutex):
    win32event, win32api = fake_win32_mutex(last_error=_FakeWinerror.ERROR_ALREADY_EXISTS)

    result = acquire_single_instance_lock("TestMutexB")

    assert result is False
    assert win32api.closed_handles == [win32event.handle], (
        "must close the duplicate handle rather than leak it when refusing to start"
    )


def test_acquire_lock_fails_closed_when_mutex_creation_raises(fake_win32_mutex, caplog):
    fake_win32_mutex(create_raises=OSError("kernel object creation failed"))

    with caplog.at_level("ERROR"):
        result = acquire_single_instance_lock("TestMutexC")

    assert result is False
    assert any("could not create" in r.message.lower() for r in caplog.records)


def test_acquire_lock_fails_closed_when_pywin32_is_not_importable(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "win32event", None)  # forces ImportError

    with caplog.at_level("ERROR"):
        result = acquire_single_instance_lock("TestMutexD")

    assert result is False
    assert any("not importable" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Promise 1: the guard is independent of the tray icon backend existing.
# ---------------------------------------------------------------------------


def test_lock_still_works_when_the_tray_only_packages_are_completely_unavailable(monkeypatch, fake_win32_mutex):
    """win32gui/win32con are the tray-icon-only packages; win32event/
    win32api/winerror are the mutex-only ones. Breaking the former must not
    touch the latter -- this is the whole point of the guard living outside
    the Tray class."""
    monkeypatch.setitem(sys.modules, "win32gui", None)
    monkeypatch.setitem(sys.modules, "win32con", None)
    fake_win32_mutex(last_error=0)

    assert acquire_single_instance_lock("TestMutexF") is True


def test_lock_is_callable_without_ever_constructing_a_tray(fake_win32_mutex):
    """The most literal reading of 'independent of the tray icon existing
    at all': no Tray instance anywhere in this test."""
    fake_win32_mutex(last_error=0)
    assert acquire_single_instance_lock("TestMutexG") is True


def test_tray_construction_touches_no_win32_api_at_all(monkeypatch):
    """Module docstring: 'Construction is cheap and touches no Win32 API.'
    Every pywin32 submodule made unimportable, and Tray(bus) must still
    succeed -- proof the constructor never even tries."""
    for name in ("win32gui", "win32api", "win32con", "win32event", "winerror"):
        monkeypatch.setitem(sys.modules, name, None)

    bus = EventBus()
    tray = Tray(bus)

    assert tray.backend_available is False
    assert tray.state == TrayState.IDLE


# ---------------------------------------------------------------------------
# Promise 2: bus-driven state transitions.
# ---------------------------------------------------------------------------


async def test_state_transitions_follow_the_documented_bus_topic_mapping():
    bus = EventBus()
    tray = Tray(bus)
    assert tray.state == TrayState.IDLE

    await bus.publish(TOPIC_PTT_START, None)
    assert tray.state == TrayState.LISTENING

    await bus.publish(TOPIC_TURN_START, None)
    assert tray.state == TrayState.THINKING

    await bus.publish(TOPIC_SPEAK_START, None)
    assert tray.state == TrayState.SPEAKING

    await bus.publish(TOPIC_SPEAK_END, None)
    assert tray.state == TrayState.THINKING

    await bus.publish(TOPIC_TURN_END, None)
    assert tray.state == TrayState.IDLE


async def test_state_transitions_work_even_though_the_tray_backend_never_started():
    """Tray(bus) subscribes in __init__, never in start() -- state must
    track correctly even if start() is never called at all."""
    bus = EventBus()
    tray = Tray(bus)

    await bus.publish(TOPIC_PTT_START, None)

    assert tray.state == TrayState.LISTENING
    assert tray.backend_available is False  # no icon, but the state is still right


async def test_state_transitions_keep_working_after_a_failed_start(monkeypatch):
    for name in ("win32gui", "win32api", "win32con"):
        monkeypatch.setitem(sys.modules, name, None)
    bus = EventBus()
    tray = Tray(bus)
    tray.start(loop=asyncio.get_running_loop())
    assert tray.backend_available is False  # failed start, as expected

    await bus.publish(TOPIC_TURN_START, None)

    assert tray.state == TrayState.THINKING


def test_a_tray_with_no_bus_never_subscribes_and_never_raises():
    tray = Tray(bus=None)
    assert tray.state == TrayState.IDLE  # constructible and usable standalone


# ---------------------------------------------------------------------------
# Promise 3: a missing backend degrades loudly, never blocks NOVA.
# ---------------------------------------------------------------------------


def test_start_with_missing_pywin32_degrades_loudly_and_does_not_raise(monkeypatch, caplog):
    for name in ("win32gui", "win32api", "win32con"):
        monkeypatch.setitem(sys.modules, name, None)
    tray = Tray(bus=None)

    with caplog.at_level("WARNING"):
        tray.start()  # must not raise

    assert tray.backend_available is False
    assert any(
        "not importable" in r.message.lower() or "pywin32" in r.message.lower()
        for r in caplog.records
    )


def test_start_with_missing_pywin32_returns_promptly_not_hanging(monkeypatch):
    """start() blocks on self._ready.wait(timeout=5.0) -- an ImportError
    inside the thread must set _ready almost immediately, not make every
    caller pay the full 5s timeout."""
    for name in ("win32gui", "win32api", "win32con"):
        monkeypatch.setitem(sys.modules, name, None)
    tray = Tray(bus=None)

    import time

    start_ts = time.monotonic()
    tray.start()
    elapsed = time.monotonic() - start_ts

    assert elapsed < 2.0, f"start() took {elapsed:.2f}s to degrade -- looks like it waited for the timeout"


def test_start_is_idempotent(monkeypatch):
    for name in ("win32gui", "win32api", "win32con"):
        monkeypatch.setitem(sys.modules, name, None)
    tray = Tray(bus=None)

    tray.start()
    first_thread = tray._thread
    tray.start()  # second call must not spawn a new thread

    assert tray._thread is first_thread


def test_start_with_no_running_loop_and_none_provided_degrades_without_crashing(monkeypatch):
    """Plain (non-async) test -- deliberately no running asyncio loop."""
    for name in ("win32gui", "win32api", "win32con"):
        monkeypatch.setitem(sys.modules, name, None)
    tray = Tray(bus=None)

    tray.start()  # no loop passed, none running -- must not raise

    assert tray._loop is None


def test_stop_before_start_does_not_raise():
    tray = Tray(bus=None)
    tray.stop()  # must not raise


def test_set_state_before_start_does_not_raise_and_still_updates_state():
    tray = Tray(bus=None)
    tray.set_state(TrayState.SPEAKING)
    assert tray.state == TrayState.SPEAKING


def test_set_state_after_a_failed_start_does_not_raise_and_still_updates_state(monkeypatch):
    for name in ("win32gui", "win32api", "win32con"):
        monkeypatch.setitem(sys.modules, name, None)
    tray = Tray(bus=None)
    tray.start()

    tray.set_state(TrayState.LISTENING)  # must not raise despite no real icon

    assert tray.state == TrayState.LISTENING


# ---------------------------------------------------------------------------
# _invoke_on_quit: routes through post_callback (F30) when a loop is known,
# calls directly otherwise, and never lets on_quit's own exception escape.
# ---------------------------------------------------------------------------


def test_invoke_on_quit_routes_through_post_callback_when_a_loop_is_present(monkeypatch):
    calls = []
    monkeypatch.setattr(tray_mod, "post_callback", lambda loop, fn: calls.append((loop, fn)))

    tray = Tray(bus=None, on_quit=lambda: None)
    sentinel_loop = object()
    tray._loop = sentinel_loop

    tray._invoke_on_quit()

    assert len(calls) == 1
    assert calls[0][0] is sentinel_loop
    assert calls[0][1] is tray._on_quit


def test_invoke_on_quit_calls_directly_when_no_loop_is_known(monkeypatch):
    monkeypatch.setattr(
        tray_mod, "post_callback",
        lambda *a, **kw: pytest.fail("post_callback must not be used when no loop is known"),
    )
    called = []
    tray = Tray(bus=None, on_quit=lambda: called.append(1))
    tray._loop = None

    tray._invoke_on_quit()

    assert called == [1]


def test_invoke_on_quit_with_no_on_quit_registered_is_a_safe_noop():
    tray = Tray(bus=None, on_quit=None)
    tray._invoke_on_quit()  # must not raise


def test_invoke_on_quit_swallows_an_exception_from_on_quit_itself(caplog):
    def boom():
        raise RuntimeError("on_quit exploded")

    tray = Tray(bus=None, on_quit=boom)
    tray._loop = None

    with caplog.at_level("WARNING"):
        tray._invoke_on_quit()  # must not raise

    assert any("on_quit" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# _wnd_proc: pure message-handling logic, called directly (same technique
# as test_interfaces_hotkey.py's _on_event tests) rather than via a
# simulated OS message loop.
# ---------------------------------------------------------------------------


class _FakeWin32GuiForWndProc:
    NIM_DELETE = 2  # arbitrary but internally consistent -- real code reads win32gui.NIM_DELETE

    def __init__(self):
        self.notify_calls: list[tuple] = []
        self.destroyed_windows: list = []
        self.posted_messages: list[tuple] = []
        self.quit_posted = False
        self.menu_shown_for: list = []

    def Shell_NotifyIcon(self, action, data):
        self.notify_calls.append((action, data))

    def DestroyWindow(self, hwnd):
        self.destroyed_windows.append(hwnd)

    def PostMessage(self, hwnd, msg, wparam, lparam):
        self.posted_messages.append((hwnd, msg, wparam, lparam))

    def PostQuitMessage(self, code):
        self.quit_posted = True

    def DefWindowProc(self, hwnd, msg, wparam, lparam):
        return 0


class _FakeWin32ApiForWndProc:
    def LOWORD(self, x):
        return x & 0xFFFF


@pytest.fixture
def wnd_proc_env(monkeypatch):
    """Installs fakes for the win32 submodules _wnd_proc imports locally,
    plus the REAL win32con for its constants (pure constants, safe to use
    unmocked -- see this file's module docstring)."""
    import win32con as real_win32con

    fake_gui = _FakeWin32GuiForWndProc()
    fake_api = _FakeWin32ApiForWndProc()
    monkeypatch.setitem(sys.modules, "win32gui", fake_gui)
    monkeypatch.setitem(sys.modules, "win32api", fake_api)
    monkeypatch.setitem(sys.modules, "win32con", real_win32con)
    return fake_gui, fake_api, real_win32con


def test_wnd_proc_right_click_on_the_tray_icon_shows_the_menu(wnd_proc_env, monkeypatch):
    fake_gui, fake_api, win32con = wnd_proc_env
    tray = Tray(bus=None)
    menu_calls = []
    monkeypatch.setattr(tray, "_show_menu", lambda hwnd: menu_calls.append(hwnd))

    result = tray._wnd_proc(111, tray_mod._WM_TRAYICON, 0, win32con.WM_RBUTTONUP)

    assert menu_calls == [111]
    assert result == 0


def test_wnd_proc_left_click_on_the_tray_icon_also_shows_the_menu(wnd_proc_env, monkeypatch):
    fake_gui, fake_api, win32con = wnd_proc_env
    tray = Tray(bus=None)
    menu_calls = []
    monkeypatch.setattr(tray, "_show_menu", lambda hwnd: menu_calls.append(hwnd))

    tray._wnd_proc(111, tray_mod._WM_TRAYICON, 0, win32con.WM_LBUTTONUP)

    assert menu_calls == [111]


def test_wnd_proc_wm_command_quit_posts_wm_close_not_a_direct_quit(wnd_proc_env):
    """Quit routes through WM_CLOSE (same teardown path as stop()) rather
    than tearing down directly from inside the menu-click callback."""
    fake_gui, fake_api, win32con = wnd_proc_env
    tray = Tray(bus=None)

    tray._wnd_proc(222, win32con.WM_COMMAND, tray_mod._ID_QUIT, 0)

    assert fake_gui.posted_messages == [(222, win32con.WM_CLOSE, 0, 0)]


def test_wnd_proc_wm_command_with_an_unrelated_id_does_nothing():
    fake_gui = _FakeWin32GuiForWndProc()
    fake_api = _FakeWin32ApiForWndProc()
    import sys as _sys

    _sys.modules["win32gui"] = fake_gui
    _sys.modules["win32api"] = fake_api
    try:
        import win32con

        tray = Tray(bus=None)
        tray._wnd_proc(222, win32con.WM_COMMAND, 99999, 0)
        assert fake_gui.posted_messages == []
    finally:
        del _sys.modules["win32gui"]
        del _sys.modules["win32api"]


def test_wnd_proc_wm_close_deletes_the_notify_icon_and_destroys_the_window(wnd_proc_env):
    fake_gui, fake_api, win32con = wnd_proc_env
    tray = Tray(bus=None)

    tray._wnd_proc(333, win32con.WM_CLOSE, 0, 0)

    assert fake_gui.destroyed_windows == [333]
    assert len(fake_gui.notify_calls) == 1


def test_wnd_proc_wm_close_destroys_the_window_even_if_notify_icon_delete_fails(wnd_proc_env, caplog):
    fake_gui, fake_api, win32con = wnd_proc_env

    def _raising_notify(action, data):
        raise RuntimeError("NIM_DELETE failed")

    fake_gui.Shell_NotifyIcon = _raising_notify
    tray = Tray(bus=None)

    with caplog.at_level("WARNING"):
        tray._wnd_proc(333, win32con.WM_CLOSE, 0, 0)  # must not raise

    assert fake_gui.destroyed_windows == [333]  # window teardown still happens


def test_wnd_proc_wm_destroy_clears_hwnd_and_posts_quit(wnd_proc_env):
    """The WM_DESTROY handler clears _hwnd from the thread that owns the
    window -- see the module's own comment on why: a menu-driven Quit
    destroys the window from inside this same callback chain, racing
    stop() calling in from another thread."""
    fake_gui, fake_api, win32con = wnd_proc_env
    tray = Tray(bus=None)
    tray._hwnd = 444

    tray._wnd_proc(444, win32con.WM_DESTROY, 0, 0)

    assert tray._hwnd is None
    assert fake_gui.quit_posted is True


def test_wnd_proc_unrecognized_message_falls_through_to_def_window_proc(wnd_proc_env):
    fake_gui, fake_api, win32con = wnd_proc_env
    tray = Tray(bus=None)

    result = tray._wnd_proc(555, 0x9999, 1, 2)  # an arbitrary message id nothing here handles

    assert result == 0  # DefWindowProc's fake return


# ---------------------------------------------------------------------------
# Real hardware (opt-in): set NOVA_TEST_REAL_HARDWARE=1 to exercise the
# actual Win32 mutex and a real Tray start()/stop() cycle on this machine's
# real interactive desktop session.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_TEST_REAL_HARDWARE") != "1",
    reason="set NOVA_TEST_REAL_HARDWARE=1 to create a real Win32 mutex on this machine",
)
class TestRealSingleInstanceMutex:
    def test_second_acquire_of_the_same_name_is_refused(self):
        name = "NovaTestMutexRealHardwareProbe"
        try:
            first = acquire_single_instance_lock(name)
            assert first is True

            second = acquire_single_instance_lock(name)
            assert second is False, "a second acquire of the same mutex name must be refused"
        finally:
            tray_mod._release_single_instance_lock_for_tests()

    def test_lock_is_reacquirable_after_release(self):
        name = "NovaTestMutexRealHardwareProbe2"
        try:
            assert acquire_single_instance_lock(name) is True
        finally:
            tray_mod._release_single_instance_lock_for_tests()

        assert acquire_single_instance_lock(name) is True
        tray_mod._release_single_instance_lock_for_tests()


@pytest.mark.skipif(
    os.environ.get("NOVA_TEST_REAL_HARDWARE") != "1",
    reason="set NOVA_TEST_REAL_HARDWARE=1 to create a real tray icon on this machine",
)
async def test_real_tray_start_stop_cycle_creates_and_tears_down_a_real_icon():
    bus = EventBus()
    tray = Tray(bus)

    tray.start(loop=asyncio.get_running_loop())
    try:
        assert tray.backend_available is True
        assert tray._hwnd is not None
    finally:
        tray.stop()

    assert tray.backend_available is False
