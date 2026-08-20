"""Per-stage turn latency, published on the bus rather than computed and
logged in one place (T2.2, `docs/M2_OPEN_BRAIN_PLAN.md`).

The turn loop crosses named stage boundaries -- speech captured, transcript
ready, route decided, skill returned, synthesis returned, first sound
actually reaching the speaker -- and each crossing publishes a
`LatencyMark` on `TOPIC_LATENCY_MARK`. Nothing computes a single "turn took
Ns" number at the source; `LatencyLog` (the one subscriber `NovaApp` wires
by default) is what turns a stream of marks into a decomposed breakdown, so
a future consumer (a dashboard, `scripts/measure_latency.py`,
nova-accuracy's re-run) reads the same marks NOVA itself logs from rather
than a second, differently-computed definition of "latency" -- the
constitution's "one source of truth per invariant" rule applied to a
number, not just a config value. This module replaces the ad hoc
per-turn `timings` dict `run_turn()` used to compute and log inline before
T2.2 -- that dict and this module were measuring the same thing two ways,
which is exactly the drift risk the constitution's rule warns about.

**`first_audio_out` is the metric this exists to produce, not
`skill_returned` or `synth_returned`.** Manav's "felt slow" was about
time-to-first-sound, not turn-complete -- a turn that finishes in 5s but
starts speaking at 1.2s is a different experience from one silent for 4s.
`nova.audio.player.Player.play()`'s `on_first_frame` callback is what makes
this mark honest: it fires from the audio callback the first time real PCM
data is handed to the device, not when `Synthesizer.say()` returns (that is
`synth_returned` -- synthesis compute, kept as its own mark specifically so
a gap between it and `first_audio_out` would itself be a finding, not
hidden inside one number).

Turns that abort early (STT/router/skill exception, or a below-floor
clarification) still call `_speak()` for the apology/clarification text, so
they still produce `synth_returned`/`first_audio_out` marks -- just with an
`abort:<stage>` mark in place of whichever stage(s) never happened. That is
itself the answer to "how long before Manav hears *anything*" even on a
turn that never produces a real skill reply, which is the case this module
exists to keep visible rather than average away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nova.log import get_logger

_log = get_logger(__name__)

TOPIC_LATENCY_MARK = "latency.mark"

#: Canonical stage names, in pipeline order -- used only to label the
#: deltas in the breakdown `LatencyLog` logs. A mark with a stage not in
#: this tuple (e.g. "abort:stt") is still recorded and still shown; it is
#: just sorted in by timestamp rather than by a known position.
STAGE_ORDER: tuple[str, ...] = (
    "ptt_end",
    "capture_drained",
    "stt_start",
    "transcript_ready",
    "route_decided",
    "skill_returned",
    "synth_returned",
    "first_audio_out",
)

#: The stage that ends a turn's breakdown and triggers LatencyLog to log
#: and flush it. Every `_speak()` call reaches this (or leaves it absent,
#: which `_flush` treats as "nothing to report" rather than logging a
#: silent partial), so at most one turn's marks are ever pending at once.
TERMINAL_STAGE = "first_audio_out"


@dataclass(frozen=True)
class LatencyMark:
    """One stage-boundary crossing. `t` is `time.monotonic()` -- never
    wall-clock, so deltas stay correct across a system-clock adjustment and
    are comparable to the durations `run_turn()` already reasoned in before
    T2.2."""

    turn_id: str
    stage: str
    t: float
    meta: dict[str, Any] = field(default_factory=dict)


class LatencyLog:
    """Accumulates `LatencyMark`s per `turn_id` and logs a decomposed
    breakdown -- consecutive-stage deltas plus the ptt_end-to-first-audio-out
    total -- the moment a turn's terminal stage arrives.

    Single-slot per `turn_id`, not a bounded cache: a turn_id that is
    started but never reaches `TERMINAL_STAGE` would sit in `_pending`
    forever. Accepted for M2 -- NOVA runs one turn at a time
    (`NovaApp._turn_lock`), and every `_speak()` call (including
    apology/clarification replies) reaches `first_audio_out`, so in
    practice at most one turn_id is ever pending.
    """

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, LatencyMark]] = {}

    async def handle(self, mark: LatencyMark) -> None:
        """Bus handler -- subscribe this to `TOPIC_LATENCY_MARK`."""
        turn = self._pending.setdefault(mark.turn_id, {})
        turn[mark.stage] = mark
        if mark.stage == TERMINAL_STAGE:
            self._flush(mark.turn_id)

    def _flush(self, turn_id: str) -> None:
        marks = self._pending.pop(turn_id, {})
        if not marks:
            return
        ordered = [marks[s] for s in STAGE_ORDER if s in marks]
        extra = [m for s, m in marks.items() if s not in STAGE_ORDER]
        all_marks = sorted(ordered + extra, key=lambda m: m.t)
        if len(all_marks) < 2:
            # Only one mark ever arrived (e.g. a bare first_audio_out with
            # no ptt_end) -- nothing to compute a delta from.
            return

        deltas: dict[str, float] = {}
        prev = all_marks[0]
        for m in all_marks[1:]:
            deltas[f"{prev.stage}->{m.stage}"] = round(m.t - prev.t, 3)
            prev = m
        total = round(all_marks[-1].t - all_marks[0].t, 3)

        _log.info(
            "Turn %s latency (s): %s -> first_audio_out total=%.3f; %s",
            turn_id, all_marks[0].stage, total, deltas,
        )
