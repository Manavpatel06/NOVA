"""Contract: nova/interfaces/hotkey.py — the T0.6a push-to-talk hotkey.

Highest-priority module in the current thinness report: zero coverage
before this file, and it carries three open HIGH-severity findings
(docs/TASKS.md F4/F5/F9/F10):
  F4/F9  the keystroke-privacy guarantee on _extract_combo_event — the ONLY
         place in the module allowed to touch a raw key event. Must never
         log, buffer, or leak it.
  F5     LATCHED must never be able to hold the mic open indefinitely —
         the latch-timeout failsafe must actually fire.
  F10    _on_event must never let an exception escape (Windows silently
         removes a hook that throws, killing PTT with no signal).

No real keyboard hook is ever installed in this file: `_on_event` /
`_extract_combo_event` are called directly with fake event objects, and
`start()`'s tests replace `sys.modules["keyboard"]` with a fake rather than
touching the real global hook (which would affect the actual machine).

Durations are all small and relative (tap_threshold_ms/latch_timeout_ms
configured to tens of milliseconds for the test), never pinned to the wall
clock — CLAUDE.md's time-independence rule.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import time
from pathlib import Path

import pytest

from nova.bus import EventBus
from nova.interfaces.hotkey import (
    DEFAULT_COMBO,
    TOPIC_PTT_END,
    TOPIC_PTT_START,
    Hotkey,
    PTTEvent,
    _canonical,
)


class _FakeEvent:
    def __init__(self, name, event_type):
        self.name = name
        self.event_type = event_type


class _FakeBus:
    def __init__(self):
        self.published: list[tuple[str, PTTEvent]] = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


async def _wait_until(predicate, timeout=2.0):
    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


def _press(hotkey: Hotkey, name: str) -> None:
    hotkey._on_event(_FakeEvent(name, "down"))


def _release(hotkey: Hotkey, name: str) -> None:
    hotkey._on_event(_FakeEvent(name, "up"))


# ---------------------------------------------------------------------------
# _extract_combo_event: the privacy-critical function (F4/F9)
# ---------------------------------------------------------------------------


def _make_hotkey(bus=None, loop=None, **kw) -> Hotkey:
    return Hotkey(bus=bus or _FakeBus(), combo=kw.pop("combo", "ctrl+alt+space"), loop=loop, **kw)


def test_extract_combo_event_discards_non_combo_keys():
    hk = _make_hotkey()
    assert hk._extract_combo_event(_FakeEvent("p", "down")) is None
    assert hk._extract_combo_event(_FakeEvent("a", "down")) is None
    assert hk._extract_combo_event(_FakeEvent("1", "down")) is None


def test_extract_combo_event_returns_a_minimal_tuple_for_a_combo_key():
    hk = _make_hotkey()
    result = hk._extract_combo_event(_FakeEvent("ctrl", "down"))
    assert result is not None
    key, event_type, ts = result
    assert key == "ctrl"
    assert event_type == "down"
    assert isinstance(ts, float)


def test_extract_combo_event_canonicalizes_sided_modifier_keys():
    hk = _make_hotkey()
    left = hk._extract_combo_event(_FakeEvent("left ctrl", "down"))
    right = hk._extract_combo_event(_FakeEvent("right ctrl", "down"))
    assert left[0] == "ctrl"
    assert right[0] == "ctrl"


def test_extract_combo_event_never_logs_anything_not_even_for_a_discarded_key(caplog):
    """A password keystroke ('p', 'a', etc.) must produce zero log records
    at ANY level — not just avoid ERROR/WARNING, avoid all of them."""
    hk = _make_hotkey()
    with caplog.at_level("DEBUG"):
        hk._extract_combo_event(_FakeEvent("p", "down"))
        hk._extract_combo_event(_FakeEvent("a", "down"))
        hk._extract_combo_event(_FakeEvent("ctrl", "down"))  # combo key too — still no logging
    assert caplog.records == []


def test_extract_combo_event_source_contains_no_logging_call():
    """Structural backstop matching the module's own stated philosophy
    ('stay small and reviewable enough that never is easy to verify by
    reading it') — an AST guard so a future edit that adds a stray
    _log.debug(event) call for 'temporary debugging' fails the build, not
    just a code review."""
    import nova.interfaces.hotkey as hotkey_mod

    source = Path(hotkey_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_extract_combo_event"
    )
    for node in ast.walk(target):
        if isinstance(node, ast.Attribute) and node.attr in (
            "debug", "info", "warning", "error", "exception", "critical", "log",
        ):
            pytest.fail(f"_extract_combo_event contains a logging call at line {node.lineno}")


def test_extract_combo_event_return_value_never_holds_the_raw_event_object():
    hk = _make_hotkey()
    ev = _FakeEvent("ctrl", "down")
    result = hk._extract_combo_event(ev)
    assert result is not None
    assert ev not in result
    assert len(result) == 3  # (key, event_type, ts) only


def test_canonical_handles_all_sided_modifiers_and_passthrough():
    assert _canonical("left ctrl") == "ctrl"
    assert _canonical("right ctrl") == "ctrl"
    assert _canonical("left alt") == "alt"
    assert _canonical("right alt") == "alt"
    assert _canonical("left shift") == "shift"
    assert _canonical("right shift") == "shift"
    assert _canonical("left windows") == "windows"
    assert _canonical("right windows") == "windows"
    assert _canonical("space") == "space"
    assert _canonical(None) is None


# ---------------------------------------------------------------------------
# PTTEvent never carries key identities
# ---------------------------------------------------------------------------


def test_ptt_event_has_no_key_identity_fields():
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(PTTEvent)}
    assert field_names == {"ts", "mode"}


# ---------------------------------------------------------------------------
# State machine: HOLD mode
# ---------------------------------------------------------------------------


async def test_hold_mode_publishes_start_then_end_with_hold_mode():
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop(), tap_threshold_ms=50)

    _press(hk, "ctrl")
    _press(hk, "alt")
    _press(hk, "space")
    await _wait_until(lambda: len(bus.published) == 1)
    assert bus.published[0][0] == TOPIC_PTT_START

    time.sleep(0.1)  # exceed the 50ms tap threshold -> classified as a hold
    _release(hk, "space")
    _release(hk, "alt")
    _release(hk, "ctrl")
    await _wait_until(lambda: len(bus.published) == 2)

    assert bus.published[1][0] == TOPIC_PTT_END
    assert bus.published[1][1].mode == "hold"


async def test_partial_combo_press_does_not_arm():
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop())

    _press(hk, "ctrl")
    _press(hk, "alt")  # space never pressed — combo incomplete
    await asyncio.sleep(0.05)

    assert bus.published == []


async def test_releasing_combo_keys_out_of_order_still_ends_correctly():
    """Real releases arrive a few ms apart, in whatever physical order the
    user lifts their fingers — must not react until ALL combo keys are up."""
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop(), tap_threshold_ms=50)

    _press(hk, "space")
    _press(hk, "ctrl")
    _press(hk, "alt")
    await _wait_until(lambda: len(bus.published) == 1)

    time.sleep(0.1)
    _release(hk, "alt")   # released first, out of press order
    assert bus.published == [(TOPIC_PTT_START, bus.published[0][1])]  # still just the start
    _release(hk, "ctrl")
    assert len(bus.published) == 1  # still not all up
    _release(hk, "space")  # last one — now it should end
    await _wait_until(lambda: len(bus.published) == 2)
    assert bus.published[1][0] == TOPIC_PTT_END


async def test_repeated_down_events_for_an_already_held_key_do_not_break_state():
    """Key-repeat (holding a key sends repeated 'down' events) must be
    idempotent, not cause a double-arm or a spurious second start."""
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop())

    _press(hk, "ctrl")
    _press(hk, "ctrl")  # OS key-repeat
    _press(hk, "alt")
    _press(hk, "space")
    _press(hk, "space")  # key-repeat on the last key too
    await _wait_until(lambda: len(bus.published) == 1)

    assert len(bus.published) == 1  # exactly one ptt.start, not more


# ---------------------------------------------------------------------------
# State machine: TAP-LATCH mode + the failsafe timeout (F5)
# ---------------------------------------------------------------------------


async def test_quick_tap_enters_latched_and_a_second_tap_ends_it():
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop(), tap_threshold_ms=200, latch_timeout_ms=5000)

    _press(hk, "ctrl"); _press(hk, "alt"); _press(hk, "space")
    _release(hk, "space"); _release(hk, "alt"); _release(hk, "ctrl")  # fast — well under 200ms
    await _wait_until(lambda: len(bus.published) == 1)
    assert hk._state.value == "latched"

    _press(hk, "ctrl"); _press(hk, "alt"); _press(hk, "space")
    _release(hk, "space"); _release(hk, "alt"); _release(hk, "ctrl")
    await _wait_until(lambda: len(bus.published) == 2)

    assert bus.published[1][0] == TOPIC_PTT_END
    assert bus.published[1][1].mode == "latch"
    assert hk._state.value == "idle"


class _FakeTimer:
    """Stands in for threading.Timer so the latch failsafe is testable
    without ever waiting on a real wall-clock interval. Per the lead's
    ruling: a test that depends on a real timer firing within a real
    interval fails under load, on a busy machine, or with six agents
    hammering the CPU (exactly this machine, exactly what happened —
    test_latch_auto_expires_after_the_timeout_with_no_closing_tap was
    observed flaky both isolated and in the full suite). This tests the
    promise (a timer is scheduled with the configured duration; firing it
    publishes ptt.end(mode="latch_timeout")) rather than the scheduler."""

    instances: list["_FakeTimer"] = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False
        _FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def fake_timer(monkeypatch):
    import threading as _threading

    _FakeTimer.instances.clear()
    monkeypatch.setattr(_threading, "Timer", _FakeTimer)
    return _FakeTimer


async def test_latch_timer_is_armed_with_the_configured_duration(fake_timer):
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop(), tap_threshold_ms=200, latch_timeout_ms=12345)

    _press(hk, "ctrl"); _press(hk, "alt"); _press(hk, "space")
    _release(hk, "space"); _release(hk, "alt"); _release(hk, "ctrl")
    await _wait_until(lambda: len(bus.published) == 1)
    assert hk._state.value == "latched"

    assert len(fake_timer.instances) == 1
    timer = fake_timer.instances[-1]
    assert timer.interval == pytest.approx(12.345)  # seconds, from latch_timeout_ms=12345
    assert timer.started is True


async def test_latch_timer_firing_ends_the_latch_with_timeout_mode(fake_timer):
    """The failsafe itself (F5): firing the scheduled timer -- not waiting
    for it -- must end LATCHED and publish ptt.end(mode='latch_timeout')."""
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop(), tap_threshold_ms=200, latch_timeout_ms=30_000)
    hk.backend_available = True  # _on_latch_timeout no-ops unless "running"

    _press(hk, "ctrl"); _press(hk, "alt"); _press(hk, "space")
    _release(hk, "space"); _release(hk, "alt"); _release(hk, "ctrl")
    await _wait_until(lambda: len(bus.published) == 1)
    assert hk._state.value == "latched"

    timer = fake_timer.instances[-1]
    timer.function()  # fire the timeout callback directly, no real waiting

    await _wait_until(lambda: len(bus.published) == 2)
    assert bus.published[1][0] == TOPIC_PTT_END
    assert bus.published[1][1].mode == "latch_timeout"
    assert hk._state.value == "idle"


async def test_latch_timer_does_not_fire_if_backend_became_unavailable_meanwhile(fake_timer):
    """_on_latch_timeout's own guard: a timer that outlives stop() must not
    publish a stray ptt.end for a session that already ended for real."""
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop(), tap_threshold_ms=200, latch_timeout_ms=30_000)
    hk.backend_available = True

    _press(hk, "ctrl"); _press(hk, "alt"); _press(hk, "space")
    _release(hk, "space"); _release(hk, "alt"); _release(hk, "ctrl")
    await _wait_until(lambda: len(bus.published) == 1)

    hk.backend_available = False  # e.g. stop() ran before the timer fired
    timer = fake_timer.instances[-1]
    timer.function()

    await asyncio.sleep(0.05)
    assert len(bus.published) == 1  # no spurious ptt.end


async def test_a_second_tap_before_timeout_cancels_the_failsafe_timer(fake_timer):
    """If the closing tap beats the timeout, the scheduled timer must
    actually be cancelled, not merely superseded/ignored."""
    bus = _FakeBus()
    hk = _make_hotkey(bus=bus, loop=asyncio.get_running_loop(), tap_threshold_ms=200, latch_timeout_ms=300)

    _press(hk, "ctrl"); _press(hk, "alt"); _press(hk, "space")
    _release(hk, "space"); _release(hk, "alt"); _release(hk, "ctrl")
    await _wait_until(lambda: len(bus.published) == 1)
    armed_timer = fake_timer.instances[-1]
    assert armed_timer.cancelled is False

    _press(hk, "ctrl"); _press(hk, "alt"); _press(hk, "space")
    _release(hk, "space"); _release(hk, "alt"); _release(hk, "ctrl")
    await _wait_until(lambda: len(bus.published) == 2)

    assert armed_timer.cancelled is True
    assert bus.published[1][1].mode == "latch"  # ended by the tap, not the timeout
    # And even if it somehow still fired late, it must be a no-op now:
    armed_timer.function()
    await asyncio.sleep(0.05)
    assert len(bus.published) == 2


# ---------------------------------------------------------------------------
# _on_event: must never let an exception escape (F10)
# ---------------------------------------------------------------------------


def test_on_event_never_propagates_an_exception(monkeypatch):
    hk = _make_hotkey()

    def boom(event):
        raise RuntimeError("simulated failure deep in the state machine")

    monkeypatch.setattr(hk, "_extract_combo_event", boom)
    result = hk._on_event(_FakeEvent("ctrl", "down"))  # must not raise
    assert result is True  # never suppress the keystroke


def test_on_event_exception_log_never_contains_the_exception_message(monkeypatch, caplog):
    """Only the exception TYPE name may be logged — the message itself
    could echo something keystroke-derived."""
    hk = _make_hotkey()
    secret_message = "SUPER_SECRET_KEYSTROKE_DERIVED_TEXT"

    def boom(event):
        raise RuntimeError(secret_message)

    monkeypatch.setattr(hk, "_extract_combo_event", boom)
    with caplog.at_level("WARNING"):
        hk._on_event(_FakeEvent("ctrl", "down"))

    joined = " ".join(r.message for r in caplog.records)
    assert secret_message not in joined
    assert "RuntimeError" in joined


# ---------------------------------------------------------------------------
# start()/stop(): no real global hook is ever installed by this file
# ---------------------------------------------------------------------------


class _FakeKeyboardModule:
    def __init__(self, hook_raises: Exception | None = None):
        self.hook_raises = hook_raises
        self.hooked_callback = None
        self.removed = False

    def hook(self, callback):
        if self.hook_raises is not None:
            raise self.hook_raises
        self.hooked_callback = callback

        def _remove():
            self.removed = True

        return _remove


@pytest.fixture
def fake_keyboard_module(monkeypatch):
    fake = _FakeKeyboardModule()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    return fake


async def test_start_installs_the_hook_and_is_idempotent(fake_keyboard_module):
    hk = _make_hotkey(loop=asyncio.get_running_loop())
    hk.start()
    assert hk.backend_available is True
    # Bound-method access creates a fresh wrapper object each time (`is`
    # would always be False even when correctly wired) — compare by
    # equality and by underlying function identity instead.
    assert fake_keyboard_module.hooked_callback == hk._on_event
    assert fake_keyboard_module.hooked_callback.__func__ is Hotkey._on_event

    hk.start()  # second call must not re-install
    assert fake_keyboard_module.removed is False


async def test_start_degrades_loudly_when_the_hook_install_fails(caplog):
    fake = _FakeKeyboardModule(hook_raises=RuntimeError("policy blocked the hook"))
    import sys as _sys

    _sys.modules["keyboard"] = fake
    try:
        hk = _make_hotkey(loop=asyncio.get_running_loop())
        with caplog.at_level("WARNING"):
            hk.start()
        assert hk.backend_available is False
        assert any("hook" in r.message.lower() for r in caplog.records)
    finally:
        del _sys.modules["keyboard"]


def test_start_with_no_running_loop_and_none_provided_degrades_without_crashing(caplog):
    """Plain (non-async) test — deliberately no running asyncio loop."""
    hk = Hotkey(bus=_FakeBus(), combo="ctrl+alt+space")  # no loop passed
    with caplog.at_level("WARNING"):
        hk.start()
    assert hk.backend_available is False


async def test_stop_before_start_does_not_raise():
    hk = _make_hotkey(loop=asyncio.get_running_loop())
    hk.stop()  # must not raise


async def test_stop_removes_the_hook_and_resets_state(fake_keyboard_module):
    hk = _make_hotkey(loop=asyncio.get_running_loop())
    hk.start()
    _press(hk, "ctrl")  # partial state, mid-combo

    hk.stop()

    assert fake_keyboard_module.removed is True
    assert hk.backend_available is False
    assert hk._state.value == "idle"
    assert hk._held == set()


# ---------------------------------------------------------------------------
# Config precedence: explicit constructor args win, missing config degrades
# ---------------------------------------------------------------------------


def test_explicit_combo_overrides_config(monkeypatch):
    import nova.config as config_mod

    def fake_load_config():
        raise config_mod.ConfigError("no config in this test")

    monkeypatch.setattr(config_mod, "load_config", fake_load_config)
    hk = Hotkey(bus=_FakeBus(), combo="ctrl+shift+f9")
    assert hk.combo == "ctrl+shift+f9"


def test_missing_config_degrades_to_defaults_not_a_crash(monkeypatch, caplog):
    import nova.config as config_mod

    def fake_load_config():
        raise config_mod.ConfigError("no config in this test")

    monkeypatch.setattr(config_mod, "load_config", fake_load_config)
    with caplog.at_level("WARNING"):
        hk = Hotkey(bus=_FakeBus())
    assert hk.combo == DEFAULT_COMBO


def test_empty_combo_string_is_rejected():
    with pytest.raises(ValueError):
        Hotkey(bus=_FakeBus(), combo="   ")
