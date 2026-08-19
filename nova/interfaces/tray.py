"""System tray icon + single-instance guard (T1.4).

Two independent things live here, both requested together but load-bearing
for different reasons:

1. `acquire_single_instance_lock()` — a named Win32 mutex that stops a
   second `python -m nova` from ever running alongside a first one. This has
   nothing to do with the tray icon rendering; it must work even if the tray
   backend below fails entirely, because "two NOVAs fighting over one
   microphone" is a correctness bug, not a cosmetic one. Call it once, early
   in boot, before anything opens a microphone.
2. `Tray` — a notification-area icon that shows one of four states (idle /
   listening / thinking / speaking) and a right-click menu with Quit.

Backend: raw `pywin32` (`win32gui`/`win32api`/`win32event`), not `pystray`.
`pywin32` is already a pinned dependency (used today for SAPI TTS fallback
and PTT combo checks); `pystray` would add itself plus `Pillow` as two new
direct dependencies for something `pywin32` alone already does --
`Shell_NotifyIcon` for the icon, hand-rolled 16x16 solid-color icons via GDI
(no image files, no Pillow) for the four states, and a dedicated message-loop
thread for the popup menu. Given CLAUDE.md's "free/open-source only" is
about cost, not headcount of dependencies, the real reason to prefer this is
the RAM/footprint rule and the standing warning in requirements.txt that a
missing *transitive* dependency has already taken the suite red once --
adding pystray+Pillow's own transitive closures for a 16x16 coloured square
is not a good trade. If a future skill needs real icon art (not solid
colours), that's the moment to revisit this call, not before.

**Degrades loudly, never silently**: if `pywin32`'s GUI pieces can't be
imported, or window/icon creation fails for any reason (no interactive
desktop session, policy-blocked, etc.), `Tray.start()` logs a WARNING naming
the failure and leaves `backend_available=False` -- NOVA keeps running with
no visible tray icon rather than crashing or blocking. Same shape as
`nova.interfaces.hotkey.Hotkey`'s degrade-loudly rule, deliberately mirrored.

**State feed**: `Tray` subscribes itself to the bus (in `__init__`, like
`Governor` does, not in `start()`) for four transitions:
  - `nova.interfaces.hotkey.TOPIC_PTT_START` -> LISTENING
  - `nova.governor.TOPIC_TURN_START` -> THINKING
  - `TOPIC_SPEAK_START` (defined *here*, not in `nova.app`) -> SPEAKING
  - `TOPIC_SPEAK_END` (ditto) -> THINKING
  - `nova.governor.TOPIC_TURN_END` -> IDLE
`TOPIC_SPEAK_START`/`TOPIC_SPEAK_END` are owned by this module even though
`nova.app.NovaApp._speak()` is what publishes them -- this inverts the
"producer owns the topic" convention `nova.interfaces.hotkey`/`nova.governor`
otherwise follow, and does so on purpose: `nova.app` already imports this
module to construct a `Tray`, so importing app.py *back* into tray.py for
two string constants would be the registry.py<->ladder.py import cycle
again, just with different names. `nova.governor` already sets the
precedent for this exact inversion -- `TOPIC_TURN_START`/`TOPIC_TURN_END`
are defined in governor.py but *published* by `nova.app.NovaApp.run_turn()`,
per governor.py's own module docstring. Same shape, applied consistently.

**Verification**: everything below the Win32 boundary (mutex semantics
across two real processes, icon handle creation/destruction, the
message-loop thread starting and shutting down cleanly on a simulated
WM_COMMAND, Shell_NotifyIcon add/modify/delete not raising) was verified
against the real Win32 APIs on this machine -- there is a real interactive
desktop session here (`GetDC(0)` succeeds), unlike the retired build's
headless dev box. What was **not**, and cannot be, verified by an agent: that
a human looking at the actual notification area sees a legible icon that
changes colour, that a right-click produces a menu a human can read and
click, and that clicking Quit is felt as "the icon disappeared and NOVA
stopped" rather than just "a thread exited". `scripts/probe_tray.py` is for
a human to run and look at.
"""

from __future__ import annotations

import asyncio
import threading
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from nova.log import get_logger

if TYPE_CHECKING:
    from nova.bus import EventBus

_log = get_logger(__name__)

#: Owned here, not in nova.app -- see module docstring for why.
TOPIC_SPEAK_START = "speak.start"
TOPIC_SPEAK_END = "speak.end"

#: Kernel object name for the single-instance mutex. No "Global\" prefix:
#: this is a personal, single-user desktop (CLAUDE.md), not a multi-session
#: terminal-services box, so session-local uniqueness is exactly the
#: property needed -- "Global\" namespace also requires a privilege that a
#: plain interactive user should not need to be granted just to run NOVA.
_MUTEX_NAME = "NovaSingleInstanceMutex"

_WM_TRAYICON = 0x8000 + 20  # WM_APP + 20 -- Shell_NotifyIcon's callback message id
_NOTIFY_ID = 1  # this process only ever registers one tray icon
_ID_QUIT = 1000  # menu command id


class TrayState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


#: Solid RGB fill per state -- deliberately not icon art (see module
#: docstring's dependency-avoidance rationale). Chosen for contrast against
#: both light and dark Windows taskbar themes, not for branding.
_STATE_COLOR: dict[TrayState, tuple[int, int, int]] = {
    TrayState.IDLE: (128, 128, 128),
    TrayState.LISTENING: (46, 204, 64),
    TrayState.THINKING: (255, 200, 0),
    TrayState.SPEAKING: (0, 116, 217),
}

_STATE_TOOLTIP: dict[TrayState, str] = {
    TrayState.IDLE: "NOVA — idle",
    TrayState.LISTENING: "NOVA — listening",
    TrayState.THINKING: "NOVA — thinking",
    TrayState.SPEAKING: "NOVA — speaking",
}


# ---------------------------------------------------------------------------
# Single-instance guard -- independent of the tray icon existing at all.
# ---------------------------------------------------------------------------

#: Kept alive for the process lifetime: closing/GC'ing the handle releases
#: the mutex early, which would defeat the whole point. Module-global by
#: design, mirroring nova.security.untrusted's _AUDIT -- one process, one
#: lock, set once at boot.
_mutex_handle: Any = None


def acquire_single_instance_lock(name: str = _MUTEX_NAME) -> bool:
    """Returns True if this process now holds the single-instance lock
    (no other NOVA instance is running), False otherwise. Call once, early
    in boot, before anything opens a microphone -- the ptt/wake capture path
    is exactly the resource two NOVAs must never contend for.

    Fails closed: if the mutex can't even be created (an unexpected Win32
    failure, not "someone else already holds it"), this returns False rather
    than True. We cannot tell the difference between "no other instance" and
    "unknown" in that case, and letting NOVA start on an unknown answer is
    exactly the risk this function exists to remove -- same fail-closed
    reasoning as CONFIRM's "no channel means deny", applied to the same
    class of problem one layer down.
    """
    global _mutex_handle
    try:
        import win32api
        import win32event
        import winerror
    except ImportError as exc:
        _log.error(
            "pywin32 (win32event) is not importable (%s) -- cannot verify "
            "single-instance status. Refusing to claim the lock.",
            exc,
        )
        return False

    try:
        handle = win32event.CreateMutex(None, False, name)
    except Exception as exc:
        _log.error(
            "Could not create the single-instance mutex %r (%s) -- cannot "
            "verify whether another NOVA instance is already running. "
            "Refusing to claim the lock.",
            name, exc,
        )
        return False

    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    if already_running:
        _log.error(
            "Another NOVA instance already holds the single-instance lock "
            "(mutex %r) -- refusing to start a second one. Two processes "
            "competing for one microphone is not a state this app supports.",
            name,
        )
        try:
            win32api.CloseHandle(handle)
        except Exception:
            pass  # best-effort; we're refusing to start either way
        return False

    _mutex_handle = handle
    _log.info("Single-instance lock acquired (mutex %r)", name)
    return True


def _release_single_instance_lock_for_tests() -> None:
    """Not part of the public contract. nova-tester needs a way to release
    the lock between test cases in the same process (two real OS processes
    is the realistic test shape, but a same-process "acquire twice" case
    needs this to avoid leaking a held handle across tests). Mirrors
    nova.skills.registry._reset_registry_for_tests exactly."""
    global _mutex_handle
    if _mutex_handle is not None:
        try:
            import win32api

            win32api.CloseHandle(_mutex_handle)
        except Exception:
            pass
        _mutex_handle = None


# ---------------------------------------------------------------------------
# Tray -- the notification-area icon.
# ---------------------------------------------------------------------------


class Tray:
    """Notification-area icon showing NOVA's current state, with a Quit
    menu item. Construction is cheap and touches no Win32 API -- only
    `start()` installs the window/icon; safe to construct even where no
    interactive desktop session exists (tests, CI).
    """

    def __init__(self, bus: "EventBus | None" = None, *, on_quit: Callable[[], None] | None = None) -> None:
        self._bus = bus
        self._on_quit = on_quit
        self._state = TrayState.IDLE
        self._lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hwnd: int | None = None
        self._class_name: str | None = None
        self._hinst: int | None = None
        self._icons: dict[TrayState, int] = {}
        self.backend_available = False

        if bus is not None:
            # Subscribed here, not in start(): cheap, no Win32 touched, and
            # mirrors nova.governor.Governor's own __init__-time subscribe.
            # A handler fires even if the tray backend never came up --
            # set_state() below no-ops the native call in that case, so a
            # missing tray icon costs nothing beyond itself.
            from nova.governor import TOPIC_TURN_END, TOPIC_TURN_START
            from nova.interfaces.hotkey import TOPIC_PTT_START

            bus.subscribe(TOPIC_PTT_START, self._on_ptt_start)
            bus.subscribe(TOPIC_TURN_START, self._on_turn_start)
            bus.subscribe(TOPIC_SPEAK_START, self._on_speak_start)
            bus.subscribe(TOPIC_SPEAK_END, self._on_speak_end)
            bus.subscribe(TOPIC_TURN_END, self._on_turn_end)

    @property
    def state(self) -> TrayState:
        return self._state

    # -- lifecycle ----------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Install the tray icon on a dedicated thread running its own
        Win32 message loop (Shell_NotifyIcon's click/menu messages need a
        real message pump; it cannot share asyncio's loop). Idempotent.
        Degrades loudly -- see module docstring -- never raises.
        """
        if self._thread is not None:
            return

        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None  # on_quit just won't be marshaled onto a loop; not fatal

        self._thread = threading.Thread(target=self._run_message_loop, daemon=True, name="nova-tray")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            _log.warning("Tray icon did not finish initializing within 5s; continuing without one.")
            return
        if self.backend_available:
            _log.info("Tray icon armed (state=%s)", self._state.value)
        # else: _run_message_loop already logged exactly why.

    def stop(self) -> None:
        """Tear down cleanly: ask the message-loop thread to close the
        window (which deletes the icon and exits PumpMessages) and join it.
        If the thread doesn't exit in time, it's abandoned -- it's a daemon
        thread, so this can never block process exit (the console CONFIRM
        channel's readline thread established this exact pattern; see
        nova.security.confirm.ConsoleConfirmChannel's docstring)."""
        with self._lock:
            hwnd = self._hwnd
        if hwnd is not None:
            try:
                import win32con
                import win32gui

                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception as exc:
                _log.warning("Tray: failed to post WM_CLOSE during stop() (%s)", exc)

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                _log.warning(
                    "Tray message-loop thread did not exit within 2s; abandoning it "
                    "(daemon thread -- will not block process exit)."
                )
        self.backend_available = False
        self._thread = None
        self._hwnd = None

    def set_state(self, state: TrayState) -> None:
        """Update the icon/tooltip to `state`. Safe to call from any thread
        (Shell_NotifyIcon does not require the calling thread to own the
        window -- verified directly, not assumed) and safe to call before
        start() or after the backend failed to come up (no-ops the native
        call, still updates self.state so a caller can inspect it)."""
        with self._lock:
            self._state = state
            if not self.backend_available or self._hwnd is None:
                return
            try:
                import win32gui

                win32gui.Shell_NotifyIcon(
                    win32gui.NIM_MODIFY,
                    (
                        self._hwnd,
                        _NOTIFY_ID,
                        win32gui.NIF_ICON | win32gui.NIF_TIP,
                        0,
                        self._icons[state],
                        _STATE_TOOLTIP[state],
                    ),
                )
            except Exception as exc:
                _log.warning("Tray: failed to update icon to state %r (%s)", state.value, exc)

    # -- bus handlers (must be async: nova.bus.EventBus.Handler) -----------

    async def _on_ptt_start(self, _payload: Any) -> None:
        self.set_state(TrayState.LISTENING)

    async def _on_turn_start(self, _payload: Any) -> None:
        self.set_state(TrayState.THINKING)

    async def _on_speak_start(self, _payload: Any) -> None:
        self.set_state(TrayState.SPEAKING)

    async def _on_speak_end(self, _payload: Any) -> None:
        self.set_state(TrayState.THINKING)

    async def _on_turn_end(self, _payload: Any) -> None:
        self.set_state(TrayState.IDLE)

    # -- Win32 message-loop thread ------------------------------------------

    def _run_message_loop(self) -> None:
        try:
            import win32api
            import win32con
            import win32gui
        except ImportError as exc:
            _log.warning(
                "Tray backend package 'pywin32' (win32gui) is not importable (%s). "
                "NOVA has no tray icon, but will keep running.",
                exc,
            )
            self._ready.set()
            return

        try:
            hinst = win32api.GetModuleHandle(None)
            class_name = f"NovaTrayWindowClass_{id(self)}"
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = class_name
            wc.lpfnWndProc = self._wnd_proc
            win32gui.RegisterClass(wc)
            hwnd = win32gui.CreateWindow(class_name, "NOVA", 0, 0, 0, 0, 0, 0, 0, hinst, None)

            self._icons = {state: self._make_icon(color) for state, color in _STATE_COLOR.items()}

            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD,
                (hwnd, _NOTIFY_ID, flags, _WM_TRAYICON, self._icons[self._state], _STATE_TOOLTIP[self._state]),
            )

            self._hwnd = hwnd
            self._class_name = class_name
            self._hinst = hinst
            self.backend_available = True
            self._ready.set()

            win32gui.PumpMessages()  # blocks until WM_QUIT (posted from WM_DESTROY below)
        except Exception as exc:
            _log.warning(
                "Tray icon failed to initialize (%s). NOVA has no tray icon, but "
                "will keep running. This can happen with no interactive desktop "
                "session (e.g. a service/RDP-disconnected context).",
                exc,
            )
            self.backend_available = False
            self._ready.set()
            return
        finally:
            self._cleanup_native()
            self.backend_available = False
            self._invoke_on_quit()

    def _cleanup_native(self) -> None:
        try:
            import win32gui

            for hicon in self._icons.values():
                win32gui.DestroyIcon(hicon)
            self._icons = {}
            if self._class_name is not None and self._hinst is not None:
                win32gui.UnregisterClass(self._class_name, self._hinst)
        except Exception as exc:
            _log.warning("Tray: cleanup after message loop exit raised (%s)", exc)

    def _invoke_on_quit(self) -> None:
        if self._on_quit is None:
            return
        try:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._on_quit)
            else:
                self._on_quit()
        except Exception as exc:
            _log.warning("Tray: on_quit callback raised while scheduling it (%s)", exc)

    def _make_icon(self, color: tuple[int, int, int]) -> int:
        """A solid `size`x`size` colour square as an HICON, built entirely
        with GDI calls -- no image files, no Pillow (see module docstring).
        `size` is queried from the system's actual small-icon metric rather
        than hardcoded, so this renders correctly at whatever DPI/theme the
        taskbar is using rather than assuming 16px."""
        import win32api
        import win32con
        import win32gui

        size = win32api.GetSystemMetrics(win32con.SM_CXSMICON) or 16

        screen_dc = win32gui.GetDC(0)
        try:
            mem_dc = win32gui.CreateCompatibleDC(screen_dc)
            color_bmp = win32gui.CreateCompatibleBitmap(screen_dc, size, size)
            old = win32gui.SelectObject(mem_dc, color_bmp)
            brush = win32gui.CreateSolidBrush(win32api.RGB(*color))
            win32gui.FillRect(mem_dc, (0, 0, size, size), brush)
            win32gui.DeleteObject(brush)
            win32gui.SelectObject(mem_dc, old)

            # AND mask: an all-zero monochrome bitmap means "fully opaque,
            # always show the colour bitmap" -- no transparency needed for a
            # solid square. CreateBitmap(..., None) leaves bits uninitialized
            # (verified: raising a real value there fails with "This param
            # must be None" on this pywin32 build), so the mask is zeroed via
            # PatBlt(BLACKNESS) on its own memory DC instead of passed bits.
            mask_dc = win32gui.CreateCompatibleDC(screen_dc)
            mask_bmp = win32gui.CreateBitmap(size, size, 1, 1, None)
            old_mask = win32gui.SelectObject(mask_dc, mask_bmp)
            win32gui.PatBlt(mask_dc, 0, 0, size, size, win32con.BLACKNESS)
            win32gui.SelectObject(mask_dc, old_mask)

            hicon = win32gui.CreateIconIndirect((True, 0, 0, mask_bmp, color_bmp))

            win32gui.DeleteDC(mem_dc)
            win32gui.DeleteDC(mask_dc)
            win32gui.DeleteObject(color_bmp)
            win32gui.DeleteObject(mask_bmp)
            return hicon
        finally:
            win32gui.ReleaseDC(0, screen_dc)

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32api
        import win32con
        import win32gui

        if msg == _WM_TRAYICON:
            if lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONUP):
                self._show_menu(hwnd)
            return 0
        if msg == win32con.WM_COMMAND:
            if win32api.LOWORD(wparam) == _ID_QUIT:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return 0
        if msg == win32con.WM_CLOSE:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, _NOTIFY_ID))
            except Exception as exc:
                _log.warning("Tray: NIM_DELETE during close failed (%s)", exc)
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == win32con.WM_DESTROY:
            # Clear _hwnd now, on the thread that owns the window, rather
            # than leaving it for _run_message_loop's finally block: a
            # menu-driven Quit destroys the window from inside this same
            # callback chain (WM_COMMAND -> WM_CLOSE -> DestroyWindow ->
            # WM_DESTROY), and stop() can be racing in from another thread
            # right now. Without this, stop() would PostMessage(WM_CLOSE)
            # to an already-dead handle and log a harmless but noisy
            # "Invalid window handle" warning every time Quit (rather than
            # stop()) is what actually ended the tray -- observed directly
            # via scripts/probe_tray.py, not theoretical.
            with self._lock:
                self._hwnd = None
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd: int) -> None:
        import win32con
        import win32gui

        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_GRAYED, 0, f"NOVA — {self._state.value}")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, _ID_QUIT, "Quit")
            pos = win32gui.GetCursorPos()
            # SetForegroundWindow before TrackPopupMenu, and a follow-up
            # WM_NULL after -- both are the documented workaround for a
            # notification-area popup menu that otherwise fails to close
            # when the user clicks away from it (MSDN's own NOTIFYICON
            # sample carries this same pair of calls).
            win32gui.SetForegroundWindow(hwnd)
            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN, pos[0], pos[1], 0, hwnd, None)
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)
