"""Contract: nova/audio/duplex.py (T1.7, finding F26) — DuplexGate.

    class DuplexGate:
        def __init__(self, bus: EventBus | None = None, *, hangover_ms: int | None = None)
        def mic_open(self) -> bool
        def on_speak_start(self) -> None
        def on_speak_end(self) -> None

Zero coverage before this file despite being the control that gates the
wake word reaching the turn loop -- a broken subscription here means either
a deaf assistant (silently wrong one direction) or NOVA re-triggering on
its own voice (silently wrong the other direction), and nothing
distinguished the two before this suite existed.

Promises pinned, in the priority order named when this file was assigned:
  - fail closed, precisely: no bus -> mic_open() False permanently, and a
    stray direct call (on_speak_start/on_speak_end) doesn't flip it open --
    _subscribed is the one fact mic_open() checks before anything else;
  - a freshly-subscribed gate at a quiet boot IS open -- that is the
    correct answer for "nothing to protect against yet", not the failure
    case, and conflating the two produces either a deaf assistant or false
    safety;
  - no timeout second-guesses a missing end event: once speaking starts,
    only a real on_speak_end() reopens the mic, however long the wait --
    pinned with a wait well past even a generous hangover-equivalent
    duration and no end event ever called;
  - on_speak_start() during an active hangover clears it: a new utterance
    beginning supersedes the previous one's cooldown rather than stacking
    with or being ignored by it.

Real, small, relative sleeps drive the hangover-expiry tests -- never
pinned to an absolute clock time, same discipline as
nova.interfaces.hotkey's tap-threshold tests.
"""
from __future__ import annotations

import time

import pytest

from nova.audio.duplex import DEFAULT_HANGOVER_MS, DuplexGate
from nova.bus import EventBus
from nova.interfaces.tray import TOPIC_SPEAK_END, TOPIC_SPEAK_START


# ---------------------------------------------------------------------------
# Fail closed: no bus at all
# ---------------------------------------------------------------------------


def test_gate_with_no_bus_is_closed_immediately():
    gate = DuplexGate(bus=None)
    assert gate.mic_open() is False


def test_gate_with_no_bus_stays_closed_after_a_stray_direct_speak_end_call():
    """Nothing should ever be calling on_speak_end() directly on an
    unsubscribed gate in practice, but the fail-closed guarantee must hold
    regardless of how it's driven: _subscribed is the one fact mic_open()
    checks first, before anything else."""
    gate = DuplexGate(bus=None)
    gate.on_speak_end()
    assert gate.mic_open() is False


def test_gate_with_no_bus_stays_closed_after_a_stray_direct_speak_start_call():
    gate = DuplexGate(bus=None)
    gate.on_speak_start()
    assert gate.mic_open() is False


def test_gate_with_no_bus_logs_a_warning_naming_the_reason(caplog):
    with caplog.at_level("WARNING"):
        DuplexGate(bus=None)
    assert any("no bus" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# A freshly-subscribed gate at a quiet boot is legitimately open -- NOT the
# failure case this module exists to guard against.
# ---------------------------------------------------------------------------


def test_freshly_subscribed_gate_is_open_at_a_quiet_boot():
    bus = EventBus()
    gate = DuplexGate(bus)
    assert gate.mic_open() is True


def test_subscribed_vs_unsubscribed_is_the_only_difference_that_matters_at_construction():
    """Same construction shape, the only difference being a real bus vs.
    none -- isolates _subscribed as the one thing mic_open() cares about at
    boot, distinct from anything about speaking state (neither gate has
    ever been told about a speak event yet)."""
    unsubscribed = DuplexGate(bus=None)
    subscribed = DuplexGate(EventBus())
    assert unsubscribed.mic_open() is False
    assert subscribed.mic_open() is True


# ---------------------------------------------------------------------------
# Once speaking starts, only a real end event reopens the mic -- no timeout
# second-guesses a missing one.
# ---------------------------------------------------------------------------


def test_mic_closes_the_instant_speaking_starts():
    gate = DuplexGate(EventBus(), hangover_ms=50)
    assert gate.mic_open() is True
    gate.on_speak_start()
    assert gate.mic_open() is False


def test_no_timeout_reopens_the_mic_without_a_real_end_event():
    """The property named as mattering most: stays shut well past even a
    generous hangover-equivalent wait, with no on_speak_end() ever called.
    A well-meaning 'safety' timeout added later would silently reopen the
    mic mid-utterance -- the exact self-echo loop this module exists to
    prevent."""
    gate = DuplexGate(EventBus(), hangover_ms=100)
    gate.on_speak_start()

    time.sleep(0.3)  # well past the 100ms hangover -- but no end event fired

    assert gate.mic_open() is False


# ---------------------------------------------------------------------------
# on_speak_end(): starts a real, expiring hangover window
# ---------------------------------------------------------------------------


def test_on_speak_end_keeps_the_mic_closed_immediately_after():
    gate = DuplexGate(EventBus(), hangover_ms=100)
    gate.on_speak_start()
    gate.on_speak_end()
    assert gate.mic_open() is False


def test_hangover_window_expires_and_reopens_the_mic():
    gate = DuplexGate(EventBus(), hangover_ms=50)
    gate.on_speak_start()
    gate.on_speak_end()
    assert gate.mic_open() is False

    time.sleep(0.15)  # comfortably past the 50ms hangover

    assert gate.mic_open() is True


def test_default_hangover_is_used_when_not_explicitly_configured():
    assert DEFAULT_HANGOVER_MS > 0
    gate = DuplexGate(EventBus())
    assert gate._hangover_s == pytest.approx(DEFAULT_HANGOVER_MS / 1000.0)


def test_hangover_ms_is_configurable_and_actually_used():
    """Proves the parameter is load-bearing, not accepted and ignored -- a
    near-zero hangover reopens almost immediately, a long one doesn't, for
    the same elapsed real wait."""
    fast_gate = DuplexGate(EventBus(), hangover_ms=1)
    slow_gate = DuplexGate(EventBus(), hangover_ms=5000)

    for gate in (fast_gate, slow_gate):
        gate.on_speak_start()
        gate.on_speak_end()

    time.sleep(0.05)

    assert fast_gate.mic_open() is True
    assert slow_gate.mic_open() is False


# ---------------------------------------------------------------------------
# A new utterance starting supersedes any pending hangover from the last one
# ---------------------------------------------------------------------------


def test_speaking_again_during_the_hangover_window_clears_it():
    gate = DuplexGate(EventBus(), hangover_ms=200)
    gate.on_speak_start()
    gate.on_speak_end()  # hangover now pending
    assert gate._hangover_until is not None

    gate.on_speak_start()  # a new utterance begins before the hangover elapsed

    assert gate._hangover_until is None
    assert gate.mic_open() is False  # still closed -- now speaking again, not "cooling down"


def test_a_second_utterances_own_hangover_is_what_eventually_reopens_the_mic():
    """Not just 'cleared' in isolation -- prove the SECOND on_speak_end()'s
    fresh hangover is what actually governs reopening, by outliving what
    would have been the first (cleared) hangover's own expiry."""
    gate = DuplexGate(EventBus(), hangover_ms=100)

    gate.on_speak_start()
    gate.on_speak_end()  # first hangover would expire ~100ms from here
    gate.on_speak_start()  # superseded before it could fire
    time.sleep(0.12)  # past when the FIRST hangover would have expired
    assert gate.mic_open() is False, "still speaking -- the cleared first hangover must not reopen the mic"

    gate.on_speak_end()  # the second, real hangover starts now
    assert gate.mic_open() is False
    time.sleep(0.15)  # past the SECOND hangover
    assert gate.mic_open() is True


# ---------------------------------------------------------------------------
# Bus wiring: the real subscribed handlers, not just the direct methods
# ---------------------------------------------------------------------------


async def test_bus_speak_start_and_speak_end_topics_drive_the_gate_through_the_real_bus():
    bus = EventBus()
    gate = DuplexGate(bus, hangover_ms=50)
    assert gate.mic_open() is True

    await bus.publish(TOPIC_SPEAK_START, None)
    assert gate.mic_open() is False

    await bus.publish(TOPIC_SPEAK_END, None)
    assert gate.mic_open() is False  # hangover still pending
    time.sleep(0.1)
    assert gate.mic_open() is True


# ---------------------------------------------------------------------------
# Config precedence: explicit constructor arg wins, missing config degrades
# ---------------------------------------------------------------------------


def test_explicit_hangover_ms_overrides_config(monkeypatch):
    import nova.config as config_mod

    def fake_load_config():
        raise config_mod.ConfigError("no config in this test")

    monkeypatch.setattr(config_mod, "load_config", fake_load_config)
    gate = DuplexGate(EventBus(), hangover_ms=12345)
    assert gate._hangover_s == pytest.approx(12.345)


def test_missing_config_degrades_to_the_default_hangover_not_a_crash(monkeypatch, caplog):
    import nova.config as config_mod

    def fake_load_config():
        raise config_mod.ConfigError("no config in this test")

    monkeypatch.setattr(config_mod, "load_config", fake_load_config)
    with caplog.at_level("WARNING"):
        gate = DuplexGate(EventBus())
    assert gate._hangover_s == pytest.approx(DEFAULT_HANGOVER_MS / 1000.0)
