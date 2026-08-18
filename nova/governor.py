"""The lazy-model governor (docs/ROADMAP.md M1, docs/ARCHITECTURE.md's
"nova.core: bus + state + governor").

## What this does and does not do

Loading is already lazy and already someone else's job: `Transcriber.
transcribe()` and `Synthesizer.say()` each call their own `load()` on first
real use if not already loaded. This module never calls `.load()` on
anything — it only ever calls `.unload()`, and only under two conditions
both being true: the process's real RSS (via `psutil`, not an estimate) is
over `config.governor.rss_ceiling_mb`, and no turn is currently in flight.
"Don't reimplement lifecycle; drive it" — the governor drives existing
`load()`/`unload()` methods, it does not reimplement what they do.

## The turn-in-flight seam

The governor must never unload a component mid-turn — unloading Whisper
while it is mid-transcription is not just a latency spike, it is undefined
behavior. There is no existing signal for "a turn is in flight" anywhere in
the codebase (the turn loop currently only holds an internal `asyncio.Lock`),
so this module defines one: two bus topics, `TOPIC_TURN_START` /
`TOPIC_TURN_END`, mirroring the existing `nova.interfaces.hotkey` pattern
(`TOPIC_PTT_START`/`TOPIC_PTT_END`) rather than a bespoke shape. `Governor`
subscribes to both in its own constructor; whatever runs a turn (`nova.app.
NovaApp.run_turn()`) is responsible for publishing `TOPIC_TURN_START` before
the turn's real work begins and `TOPIC_TURN_END` in a `finally` once it ends
— coordinated directly with nova-dev-stt (app.py's owner) rather than
guessed at from this side. The guard is deliberately coarse: a turn in
flight pauses *all* unloading, not just the specific component currently
active in that turn (transcription then synthesis touch different
components sequentially within one turn) — simpler and safer for M1 than
per-component busy-tracking, at the cost of pausing unloads slightly more
often than strictly necessary. Revisit only if measurement shows it matters.

## Picking "the largest idle component"

There is no cheap, reliable way to attribute a live RSS delta to one
specific component after the fact (multiple components can load between two
polls, background allocator behavior is not per-component-attributable via
`psutil` alone). So `register()` takes a `typical_rss_mb` hint — a *measured*
number the caller supplies (see docs/GOVERNOR_MEASUREMENTS.md for how
Transcriber's and Synthesizer's were measured on this machine), used only to
choose unload *order*, never to decide *whether* to unload — that decision
is always the live `psutil` reading. Unloading the hinted-largest component
first, re-measuring, and continuing only if still over the ceiling means a
wrong hint costs an extra unload/reload cycle, not an incorrect decision.

## Loud and reversible

Every unload is logged at WARNING (docs/ROADMAP.md: "loud in the log") with
the measured before/after RSS, never silently. A component the governor
unloads reloads itself lazily on the caller's next real use, same as any
other lazy load — the governor does not need a "reload" path of its own.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

import psutil

from nova.bus import EventBus
from nova.config import GovernorConfig
from nova.log import get_logger

logger = get_logger(__name__)

TOPIC_TURN_START = "turn.start"
TOPIC_TURN_END = "turn.end"


class LoadableComponent(Protocol):
    """Structural protocol -- Transcriber and Synthesizer already satisfy
    this without inheriting from anything."""

    def load(self) -> None: ...
    def unload(self) -> None: ...


@dataclass(frozen=True)
class GovernorSnapshot:
    """Read-only introspection for tests/reporting -- never reach into
    Governor's private state directly."""

    rss_mb: float
    ceiling_mb: float
    turns_in_flight: int
    registered: tuple[str, ...]
    unload_count: int


@dataclass
class _Registration:
    name: str
    component: LoadableComponent
    typical_rss_mb: float


class Governor:
    """Polls real RSS and unloads the largest-hinted idle component when
    over the configured ceiling. See module docstring for the turn-in-flight
    seam and how "largest" is chosen."""

    def __init__(self, bus: EventBus, config: GovernorConfig | None = None) -> None:
        self._bus = bus
        self._config = config or GovernorConfig()
        self._registrations: dict[str, _Registration] = {}
        self._turns_in_flight = 0
        self._task: asyncio.Task[None] | None = None
        self._process = psutil.Process()
        self._unload_count = 0
        self._last_unload_at: dict[str, float] = {}

        bus.subscribe(TOPIC_TURN_START, self._on_turn_start)
        bus.subscribe(TOPIC_TURN_END, self._on_turn_end)

    def register(self, name: str, component: LoadableComponent, *, typical_rss_mb: float) -> None:
        """Register a loadable component. `typical_rss_mb` is a measured
        hint (not a live reading) used only to pick unload order -- see
        docs/GOVERNOR_MEASUREMENTS.md for where the M0 callers' numbers came
        from. Required, not defaulted: a made-up number is worse than a
        real one, and a default invites skipping the measurement."""
        if name in self._registrations:
            raise ValueError(f"Governor: '{name}' is already registered")
        self._registrations[name] = _Registration(name, component, typical_rss_mb)
        logger.info("Governor: registered %r (typical footprint ~%.0f MB)", name, typical_rss_mb)

    # -- turn-in-flight guard (bus-driven; see module docstring) -----------

    async def _on_turn_start(self, _payload: Any) -> None:
        self._turns_in_flight += 1

    async def _on_turn_end(self, _payload: Any) -> None:
        self._turns_in_flight = max(0, self._turns_in_flight - 1)

    # -- measurement ---------------------------------------------------

    def current_rss_mb(self) -> float:
        """Real RSS of this process, right now, via psutil -- never a model
        or an estimate."""
        return self._process.memory_info().rss / 1e6

    def snapshot(self) -> GovernorSnapshot:
        return GovernorSnapshot(
            rss_mb=self.current_rss_mb(),
            ceiling_mb=self._config.rss_ceiling_mb,
            turns_in_flight=self._turns_in_flight,
            registered=tuple(self._registrations),
            unload_count=self._unload_count,
        )

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Governor started: poll every %.1fs, ceiling %.0f MB",
            self._config.poll_interval_s, self._config.rss_ceiling_mb,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Governor stopped")

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.poll_interval_s)
            self.check_once()

    # -- the actual decision ----------------------------------------------

    def check_once(self) -> None:
        """Run one check synchronously: if over the ceiling and no turn is
        in flight, unload registered components in descending
        `typical_rss_mb` order, re-measuring after each, until back under
        the ceiling or nothing left to unload. Public and synchronous (not
        just called from the poll loop) so tests and scripts can drive it
        deterministically without waiting on a timer.
        """
        if self._turns_in_flight > 0:
            return
        rss = self.current_rss_mb()
        if rss <= self._config.rss_ceiling_mb:
            return

        logger.info(
            "Governor: RSS %.0f MB over ceiling %.0f MB -- checking for something to unload",
            rss, self._config.rss_ceiling_mb,
        )
        ordered = sorted(self._registrations.values(), key=lambda r: r.typical_rss_mb, reverse=True)
        for reg in ordered:
            before = self.current_rss_mb()
            try:
                reg.component.unload()
            except Exception:
                logger.exception("Governor: %r.unload() raised; continuing", reg.name)
                continue
            after = self.current_rss_mb()

            if after >= before:
                # Nothing measurably freed -- most likely reg was already
                # unloaded (unload() is a documented no-op in that case).
                # Not logged as a real unload event; that would be noise
                # claiming an effect that didn't happen.
                logger.debug("Governor: %r.unload() freed nothing (already unloaded?)", reg.name)
                continue

            self._unload_count += 1
            self._last_unload_at[reg.name] = time.monotonic()
            logger.warning(
                "Governor: unloaded %r (RSS %.0f -> %.0f MB, ceiling %.0f MB)",
                reg.name, before, after, self._config.rss_ceiling_mb,
            )
            if after <= self._config.rss_ceiling_mb:
                return

        logger.warning(
            "Governor: still %.0f MB over the %.0f MB ceiling after unloading every "
            "registered component -- nothing left to free",
            self.current_rss_mb() - self._config.rss_ceiling_mb, self._config.rss_ceiling_mb,
        )
