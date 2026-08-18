"""Push-to-talk global hotkey — M0's primary input path, not a fallback.

Backend is the `keyboard` package (MIT, installed into .venv): it installs a
Windows low-level keyboard hook (WH_KEYBOARD_LL) that sees every key event
system-wide, independent of which window currently has focus. That focus
independence is the entire point of a PTT hotkey and the thing the retired
build never verified — see the T0.6a report for how it was checked for
real (Notepad given genuine foreground focus, combo injected via
win32api.keybd_event, hook still fired).

Note `keyboard.send()` cannot be used to test this module: the library sets
an internal `is_replaying` flag around its own synthetic input and its
listener explicitly ignores events while that flag is set (see
keyboard/__init__.py's `direct_callback`), specifically to stop `send()`/
`write()` from re-triggering hotkeys recursively. Real hardware keys, and
synthetic input injected through any other path (win32api.keybd_event,
SendInput called directly, etc.), are unaffected and go through the normal
hook -> callback path this module relies on.

Two modes, disambiguated by how long the combo is held between the moment
it's fully pressed and the moment any part of it is released:
  - HOLD:  press, hold, release -> armed only while held (classic PTT).
  - TAP-LATCH: quick press+release -> armed until the *next* quick tap.
Both publish "ptt.start" the instant the full combo is down (latency
matters more than knowing the eventual mode), and "ptt.end" once the
session is classified as over.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from enum import Enum

from nova.bus import EventBus
from nova.log import get_logger

_log = get_logger(__name__)

TOPIC_PTT_START = "ptt.start"
TOPIC_PTT_END = "ptt.end"

DEFAULT_COMBO = "ctrl+alt+space"
# <= this held duration (ms) between full-press and first release = a tap
# (toggles tap-latch mode). > this = a hold (classic push-to-talk). See the
# T0.6a report for why 300ms: deliberate quick taps land well under
# 200-250ms; a real PTT press-then-speak sequence almost always exceeds
# 300-400ms just from motor + speech-onset latency. Not measured against a
# human (an agent can't gather that data) -- a documented, revisable
# default, not a claimed-precise number.
DEFAULT_TAP_THRESHOLD_MS = 300

# Windows reports left/right modifier keys as distinct event names
# ("left ctrl", "right alt", ...) even though a combo like "ctrl+alt+space"
# means either physical key. Confirmed empirically: injecting ctrl+alt+space
# via win32api.keybd_event produced event.name values "left ctrl",
# "left alt", "space" -- never the bare "ctrl"/"alt" the combo string uses.
_SIDED_TO_GENERIC = {
    "left ctrl": "ctrl", "right ctrl": "ctrl",
    "left alt": "alt", "right alt": "alt",
    "left shift": "shift", "right shift": "shift",
    "left windows": "windows", "right windows": "windows",
}


def _canonical(name: str | None) -> str | None:
    if name is None:
        return None
    return _SIDED_TO_GENERIC.get(name, name)


def _default_hotkey_config():
    """HotkeyConfig is requested from nova-dev-core (T0.6a) but may not have
    landed in nova.config yet. Degrade loudly, not silently, if it's
    missing -- same pattern used for AudioConfig before T0.1 landed."""
    try:
        from nova.config import load_config

        cfg = load_config()
        hotkey_cfg = getattr(cfg, "hotkey", None)
        if hotkey_cfg is not None:
            return hotkey_cfg
        _log.warning(
            "nova.config.Config has no 'hotkey' section yet; using built-in "
            "defaults (combo=%r, tap_threshold_ms=%d). Ask nova-dev-core "
            "about landing HotkeyConfig.",
            DEFAULT_COMBO, DEFAULT_TAP_THRESHOLD_MS,
        )
    except Exception as exc:
        _log.warning(
            "Could not load config/nova.yaml (%s); using built-in hotkey "
            "defaults (combo=%r, tap_threshold_ms=%d)",
            exc, DEFAULT_COMBO, DEFAULT_TAP_THRESHOLD_MS,
        )
    return None


class _State(Enum):
    IDLE = "idle"        # not listening
    ARMED = "armed"      # combo currently fully down, mode not yet decided
    LATCHED = "latched"  # tap-armed; listening until the next quick tap


@dataclass(frozen=True)
class PTTEvent:
    ts: float
    mode: str | None = None  # None for ptt.start; "hold" or "latch" for ptt.end


class Hotkey:
    """Global push-to-talk hotkey. Publishes PTTEvent payloads on the bus
    under TOPIC_PTT_START / TOPIC_PTT_END. Fires regardless of which window
    has focus (see module docstring)."""

    def __init__(
        self,
        bus: EventBus,
        combo: str | None = None,
        tap_threshold_ms: int | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        cfg = _default_hotkey_config()
        self._combo_str = combo or (cfg.combo if cfg is not None else DEFAULT_COMBO)
        threshold_ms = tap_threshold_ms if tap_threshold_ms is not None else (
            cfg.tap_threshold_ms if cfg is not None else DEFAULT_TAP_THRESHOLD_MS
        )
        self._tap_threshold_s = threshold_ms / 1000.0
        self._combo_keys = frozenset(
            k.strip().lower() for k in self._combo_str.split("+") if k.strip()
        )
        if not self._combo_keys:
            raise ValueError(f"hotkey combo {self._combo_str!r} parsed to no keys")

        self._bus = bus
        self._loop = loop
        self._state = _State.IDLE
        self._held: set[str] = set()
        self._press_time = 0.0
        self._start_stop_lock = threading.Lock()
        self._remove_hook = None  # callable returned by keyboard.hook(), or None
        self.backend_available = False

    @property
    def combo(self) -> str:
        return self._combo_str

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Install the global hook. Idempotent. Degrades loudly (logs a
        WARNING naming the backend) if the `keyboard` package is missing or
        the hook can't be installed -- NOVA having no hotkey input path must
        never happen silently."""
        with self._start_stop_lock:
            if self._remove_hook is not None:
                return

            if loop is not None:
                self._loop = loop
            elif self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    _log.warning(
                        "Hotkey.start() called with no running asyncio loop and "
                        "none provided; ptt events cannot be published. Pass "
                        "loop= explicitly or call start() from within the loop "
                        "that will run the EventBus."
                    )
                    return

            try:
                import keyboard  # noqa: F401  (import kept local: optional dependency)
            except ImportError as exc:
                _log.warning(
                    "Hotkey backend package 'keyboard' is not installed (%s). "
                    "Push-to-talk is disabled -- NOVA has no input path until "
                    "this is fixed.",
                    exc,
                )
                return

            self._keyboard = keyboard
            try:
                self._remove_hook = keyboard.hook(self._on_event)
            except Exception as exc:
                _log.warning(
                    "Hotkey backend 'keyboard' failed to install its global "
                    "hook (%s). Push-to-talk is disabled -- NOVA has no input "
                    "path until this is fixed. On Windows this can happen if "
                    "the hook is blocked by policy; running as the interactive "
                    "desktop user (not a service session) is required.",
                    exc,
                )
                return

            self.backend_available = True
            self._state = _State.IDLE
            self._held.clear()
            _log.info(
                "Hotkey armed: combo=%r tap_threshold_ms=%d",
                self._combo_str, int(self._tap_threshold_s * 1000),
            )

    def stop(self) -> None:
        with self._start_stop_lock:
            if self._remove_hook is not None:
                try:
                    self._remove_hook()
                except Exception as exc:
                    _log.warning("Error removing hotkey hook: %s", exc)
                self._remove_hook = None
            self.backend_available = False
            self._state = _State.IDLE
            self._held.clear()
            _log.info("Hotkey stopped")

    # -- hook callback: runs on the keyboard library's own hook thread ----

    def _on_event(self, event) -> bool:
        name = _canonical(getattr(event, "name", None))
        if name not in self._combo_keys:
            return True  # not part of our combo; don't suppress, don't act

        now = time.monotonic()
        event_type = getattr(event, "event_type", None)

        if event_type == "down":
            self._held.add(name)
            if self._state == _State.IDLE and self._combo_keys <= self._held:
                self._state = _State.ARMED
                self._press_time = now
                self._publish(TOPIC_PTT_START, PTTEvent(ts=time.time()))

        elif event_type == "up":
            self._held.discard(name)
            # Releasing a multi-key combo fires one 'up' event per key, a
            # few ms apart (measured: space/alt/ctrl each ~50ms apart when
            # releasing in physical order). Acting on the first one would
            # treat the second and third as spurious re-triggers. Only act
            # once every combo key is confirmed up.
            if self._held & self._combo_keys:
                return True

            if self._state == _State.ARMED:
                held_duration = now - self._press_time
                if held_duration < self._tap_threshold_s:
                    self._state = _State.LATCHED
                    _log.info("Hotkey: tap detected (%.0fms) -> latched (listening)", held_duration * 1000)
                else:
                    self._state = _State.IDLE
                    self._publish(TOPIC_PTT_END, PTTEvent(ts=time.time(), mode="hold"))
            elif self._state == _State.LATCHED:
                self._state = _State.IDLE
                self._publish(TOPIC_PTT_END, PTTEvent(ts=time.time(), mode="latch"))

        return True  # never suppress; NOVA observes, doesn't own this combo exclusively

    def _publish(self, topic: str, payload: PTTEvent) -> None:
        if self._loop is None:
            _log.warning("Hotkey has no event loop to publish %r on; dropping event", topic)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._bus.publish(topic, payload), self._loop)
        except Exception as exc:
            _log.warning("Failed to schedule bus publish for %r: %s", topic, exc)
