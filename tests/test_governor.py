"""Contract: docs/CONTRACTS.md T1.2 — nova.governor.Governor.

    class Governor:
        def __init__(self, bus: EventBus, config: GovernorConfig | None = None)
        def register(self, name, component: LoadableComponent, *, typical_rss_mb: float) -> None
        def current_rss_mb(self) -> float
        def snapshot(self) -> GovernorSnapshot
        def check_once(self) -> None        # public + synchronous: drivable without a poll loop

Priority note: `Governor` is constructed and started for real in every
NovaApp session (nova/app.py:106-108,136) -- unlike wake.py, it's already
live, not a component waiting to be wired in. Its own module docstring is
explicit about the stakes: unloading Whisper mid-transcription is "not just
a latency spike, it is undefined behavior." So the promise under test here
-- never unload while a turn is in flight -- is the single most
consequential one in this module, and nothing checked it before this file.

Promises pinned below (docs/CONTRACTS.md T1.2):
  - never unloads mid-turn: turn.start withholds unloading no matter what
    RSS says, regardless of how many turns overlap or how turn.end/turn.start
    counts drift;
  - never calls load() -- the governor only ever drives unload();
  - typical_rss_mb affects unload ORDER only; the stop-unloading DECISION is
    always the live psutil reading, never the hint -- pinned with a scenario
    where trusting the hint's magnitude would predict "one unload is enough"
    and the live reading proves that wrong;
  - an unreachable ceiling logs a WARNING and returns -- it must not loop,
    thrash, or crash;
  - every real unload (RSS measurably decreased) is logged at WARNING; an
    unload() call that measurably freed nothing is not counted as one;
  - a raising unload() is caught, logged, and the loop continues to the next
    component rather than aborting the whole check.

check_once() is public and synchronous specifically so these tests can drive
it deterministically without a poll loop or a real timer (per the module's
own docstring). `current_rss_mb()` is monkeypatched per test to inject
controlled RSS sequences -- this file never depends on this process's actual
memory usage, which would make the suite flaky by construction.
"""
from __future__ import annotations

import asyncio

import pytest

from nova.bus import EventBus
from nova.config import GovernorConfig
from nova.governor import TOPIC_TURN_END, TOPIC_TURN_START, Governor


class _FakeComponent:
    """Stands in for Transcriber/Synthesizer -- LoadableComponent is a
    structural Protocol (load()/unload()), so this satisfies it without
    inheriting from anything, same as the real components do."""

    def __init__(self, name="comp", on_unload=None, unload_raises=None):
        self.name = name
        self.load_calls = 0
        self.unload_calls = 0
        self._on_unload = on_unload
        self.unload_raises = unload_raises

    def load(self):
        self.load_calls += 1

    def unload(self):
        self.unload_calls += 1
        if self._on_unload is not None:
            self._on_unload()
        if self.unload_raises is not None:
            raise self.unload_raises


def _rss_sequence(monkeypatch, gov: Governor, values):
    """Feeds current_rss_mb() a fixed sequence of readings, one per call, so
    a test can control exactly what the governor "measures" at each of its
    several call sites within check_once() (top-of-check, before-unload,
    after-unload, and the trailing "still over" log). Holds on the last
    value once exhausted (rather than raising StopIteration) so an
    incidental extra call -- e.g. a test's own snapshot() after check_once()
    -- doesn't fail for an unrelated reason."""
    values = list(values)

    def _next():
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    monkeypatch.setattr(gov, "current_rss_mb", _next)


async def _wait_until(predicate, timeout=2.0):
    import time

    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# Never unloads mid-turn -- the single most consequential promise here
# ---------------------------------------------------------------------------


async def test_check_once_never_unloads_while_a_turn_is_in_flight(monkeypatch):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    comp = _FakeComponent()
    gov.register("comp", comp, typical_rss_mb=500.0)
    monkeypatch.setattr(gov, "current_rss_mb", lambda: 999.0)  # always over the ceiling

    await bus.publish(TOPIC_TURN_START, None)
    gov.check_once()

    assert comp.unload_calls == 0, "unloaded a component while a turn was in flight"

    await bus.publish(TOPIC_TURN_END, None)
    gov.check_once()

    assert comp.unload_calls == 1, "did not unload once the turn ended and RSS was still over ceiling"


async def test_governor_never_calls_load_only_ever_unload(monkeypatch):
    """'It reduces; it does not summon.' -- register() must never trigger a
    load(), and neither must check_once(), regardless of turn state."""
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=0.0))  # always over
    comp = _FakeComponent()
    gov.register("comp", comp, typical_rss_mb=500.0)
    monkeypatch.setattr(gov, "current_rss_mb", lambda: 999.0)

    gov.check_once()

    assert comp.load_calls == 0


async def test_overlapping_turn_starts_require_a_matching_number_of_ends(monkeypatch):
    """Defensive: app.py's own asyncio.Lock is what normally prevents two
    overlapping run_turn() calls, but the governor's counting must not
    silently unblock after only a partial set of turn.end events."""
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    comp = _FakeComponent()
    gov.register("comp", comp, typical_rss_mb=500.0)
    monkeypatch.setattr(gov, "current_rss_mb", lambda: 999.0)

    await bus.publish(TOPIC_TURN_START, None)
    await bus.publish(TOPIC_TURN_START, None)
    await bus.publish(TOPIC_TURN_END, None)  # only one of the two ends

    gov.check_once()

    assert comp.unload_calls == 0, "still one turn in flight -- must still be blocked"


async def test_turn_end_without_a_matching_start_clamps_turns_in_flight_at_zero():
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))

    await bus.publish(TOPIC_TURN_END, None)  # stray end, no prior start

    assert gov.snapshot().turns_in_flight == 0  # clamped, not negative


# ---------------------------------------------------------------------------
# Hints pick unload ORDER only; the live psutil reading picks the DECISION
# ---------------------------------------------------------------------------


def test_unload_order_follows_typical_rss_hints_descending(monkeypatch):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    order: list[str] = []
    big = _FakeComponent("big", on_unload=lambda: order.append("big"))
    small = _FakeComponent("small", on_unload=lambda: order.append("small"))
    # Registered small-hint-first, on purpose: proves order comes from the
    # hint, not registration order.
    gov.register("small", small, typical_rss_mb=200.0)
    gov.register("big", big, typical_rss_mb=1000.0)

    _rss_sequence(monkeypatch, gov, [200.0, 200.0, 90.0])  # top-check, before(big), after(big) -- freed enough

    gov.check_once()

    assert order == ["big"], "must try the larger-hinted component first"
    assert small.unload_calls == 0, "stopped once under ceiling -- must not touch the next component"


def test_stop_condition_follows_the_live_reading_not_the_hints_magnitude(monkeypatch):
    """The scenario that would fool a hint-trusting implementation: 'big'
    carries a hint five times larger than 'small', which would predict one
    unload is surely enough -- but the LIVE reading after unloading 'big'
    shows only a small amount was actually freed and RSS is still over
    ceiling, so the governor must continue on to 'small' regardless of what
    the hint implied."""
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    order: list[str] = []
    big = _FakeComponent("big", on_unload=lambda: order.append("big"))
    small = _FakeComponent("small", on_unload=lambda: order.append("small"))
    gov.register("big", big, typical_rss_mb=1000.0)
    gov.register("small", small, typical_rss_mb=200.0)

    _rss_sequence(monkeypatch, gov, [
        200.0,  # top-of-check: over ceiling
        200.0,  # before unloading big
        180.0,  # after unloading big: freed only 20MB, still over ceiling -- hint said 1000MB
        180.0,  # before unloading small
        90.0,   # after unloading small: now under ceiling -- stop
    ])

    gov.check_once()

    assert order == ["big", "small"], "must continue past the largest-hinted component when live RSS says so"
    assert big.unload_calls == 1
    assert small.unload_calls == 1


# ---------------------------------------------------------------------------
# Logging discipline: real unloads at WARNING; a no-op unload is not counted
# ---------------------------------------------------------------------------


def test_a_real_unload_is_logged_at_warning_with_before_after_rss(monkeypatch, caplog):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    gov.register("comp", _FakeComponent("comp"), typical_rss_mb=500.0)
    _rss_sequence(monkeypatch, gov, [200.0, 200.0, 50.0])

    with caplog.at_level("WARNING"):
        gov.check_once()

    assert gov.snapshot().unload_count == 1
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("comp" in m and "->" in m for m in warnings), (
        f"expected a WARNING naming the component and the before->after RSS, got: {warnings}"
    )


def test_an_unload_that_frees_nothing_is_not_counted_as_a_real_unload(monkeypatch, caplog):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    comp = _FakeComponent("comp")
    gov.register("comp", comp, typical_rss_mb=500.0)
    # before == after at every step -- unload() runs but frees nothing
    # measurable (e.g. it was already unloaded).
    _rss_sequence(monkeypatch, gov, [200.0, 200.0, 200.0, 200.0])

    with caplog.at_level("WARNING"):
        gov.check_once()

    assert comp.unload_calls == 1, "unload() must still have been attempted"
    assert gov.snapshot().unload_count == 0, "an unload that measurably freed nothing must not be counted"
    assert not any("->" in r.message for r in caplog.records), (
        "must not log a before->after RSS line for an unload that freed nothing"
    )


def test_unreachable_ceiling_logs_a_warning_and_returns_without_thrashing(monkeypatch, caplog):
    """CONTRACTS.md: 'An unreachable ceiling logs a WARNING and returns. It
    must not loop, thrash, or crash.'"""
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    comp = _FakeComponent("comp")
    gov.register("comp", comp, typical_rss_mb=500.0)
    monkeypatch.setattr(gov, "current_rss_mb", lambda: 500.0)  # always over, never moves

    with caplog.at_level("WARNING"):
        gov.check_once()  # must return promptly, not hang

    assert comp.unload_calls == 1, "must try each registered component exactly once, not thrash"
    assert any("nothing left to free" in r.message.lower() for r in caplog.records)


def test_a_raising_unload_is_caught_logged_and_the_loop_continues(monkeypatch, caplog):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=100.0))
    order: list[str] = []
    broken = _FakeComponent("broken", on_unload=lambda: order.append("broken"), unload_raises=RuntimeError("boom"))
    healthy = _FakeComponent("healthy", on_unload=lambda: order.append("healthy"))
    gov.register("broken", broken, typical_rss_mb=1000.0)  # tried first (larger hint)
    gov.register("healthy", healthy, typical_rss_mb=200.0)

    _rss_sequence(monkeypatch, gov, [
        200.0,  # top-of-check
        200.0,  # before unloading broken
        # unload() raises here -- no "after" measurement for broken
        200.0,  # before unloading healthy
        90.0,   # after unloading healthy -- under ceiling, stop
    ])

    with caplog.at_level("ERROR"):
        gov.check_once()  # must not raise

    assert order == ["broken", "healthy"]
    assert healthy.unload_calls == 1
    assert gov.snapshot().unload_count == 1  # only the healthy one counted
    assert any(
        "broken" in r.message and r.levelname == "ERROR" for r in caplog.records
    ), "a raising unload() must be logged, naming the component"


def test_below_ceiling_does_nothing(monkeypatch):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=900.0))
    comp = _FakeComponent("comp")
    gov.register("comp", comp, typical_rss_mb=500.0)
    monkeypatch.setattr(gov, "current_rss_mb", lambda: 100.0)  # comfortably under

    gov.check_once()

    assert comp.unload_calls == 0


# ---------------------------------------------------------------------------
# register(): required hint, duplicate names
# ---------------------------------------------------------------------------


def test_register_duplicate_name_raises():
    bus = EventBus()
    gov = Governor(bus)
    gov.register("comp", _FakeComponent(), typical_rss_mb=100.0)
    with pytest.raises(ValueError):
        gov.register("comp", _FakeComponent(), typical_rss_mb=200.0)


def test_register_requires_typical_rss_mb_as_a_keyword_with_no_default():
    """CONTRACTS.md: 'typical_rss_mb is required, no default... A default
    would be an invented number, and invented numbers are what this project
    keeps deleting.'"""
    bus = EventBus()
    gov = Governor(bus)
    with pytest.raises(TypeError):
        gov.register("comp", _FakeComponent())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# snapshot(): read-only introspection
# ---------------------------------------------------------------------------


def test_snapshot_reports_live_state_not_the_hints(monkeypatch):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(rss_ceiling_mb=321.0))
    gov.register("a", _FakeComponent(), typical_rss_mb=100.0)
    monkeypatch.setattr(gov, "current_rss_mb", lambda: 42.0)

    snap = gov.snapshot()

    assert snap.rss_mb == 42.0
    assert snap.ceiling_mb == 321.0
    assert snap.turns_in_flight == 0
    assert snap.registered == ("a",)
    assert snap.unload_count == 0


# ---------------------------------------------------------------------------
# start()/stop(): the poll loop, idempotency, clean cancellation
# ---------------------------------------------------------------------------


async def test_stop_before_start_does_not_raise():
    bus = EventBus()
    gov = Governor(bus)
    await gov.stop()  # must not raise


async def test_start_is_idempotent_and_does_not_schedule_a_second_task():
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(poll_interval_s=10.0))
    await gov.start()
    task_after_first = gov._task
    await gov.start()  # second call must not replace/duplicate the task

    assert gov._task is task_after_first
    await gov.stop()
    assert gov._task is None


async def test_poll_loop_actually_drives_check_once_periodically(monkeypatch):
    bus = EventBus()
    gov = Governor(bus, GovernorConfig(poll_interval_s=0.005, rss_ceiling_mb=100.0))
    calls = []
    monkeypatch.setattr(gov, "check_once", lambda: calls.append(1))

    await gov.start()
    try:
        await _wait_until(lambda: len(calls) >= 2, timeout=2.0)
    finally:
        await gov.stop()

    assert len(calls) >= 2
