"""The turn loop — six modules made into an assistant (docs/TASKS.md T0.6b).

The M0 thread, end to end:

    hotkey -> capture -> Whisper -> router -> ladder -> skill -> reply text
    -> Kokoro -> speakers

`NovaApp` wires to the `EventBus`, not to the other modules directly: it
subscribes to `ptt.start`/`ptt.end` (published by `nova.interfaces.hotkey`)
and, since M1's wake word (docs/THREAT_MODEL_ALWAYS_ON.md), `wake.start`/
`wake.end` (published by `nova.audio.wake.WakeWord`) rather than reaching
into either module directly — this file was written so that second
subscription would be a drop-in publisher swap, not a rewrite, and F28's
ceiling/`ArmedBy` seam is what actually made landing it safe (see below).

**Capture's lifecycle changed for the wake word, and it is worth being
explicit about the shape.** Before M1, `Capture` started on `ptt.start` and
stopped on `ptt.end` — the mic was only ever open inside an explicit,
human-armed window. `WakeWord.process()` needs to see every frame
continuously to hear the wake phrase at all (`nova/audio/wake.py`'s own
module docstring), so `Capture` now runs for the whole app lifetime,
started once in `start()`, read by exactly one continuous loop
(`_continuous_capture_loop`) that fans each frame out to at most two
destinations: the active PTT/wake session buffer (if either is capturing)
and `WakeWord.process()` — SUPPRESSED while a PTT session is capturing
(the lead's ruling: a PTT session already proves presence and deliberate
speech, so the wake word re-arming INSIDE that same utterance would only
produce a redundant, overlapping second turn, never a legitimate new one;
suppression can only ever reduce arming, never permit more, which is the
fail-safe direction). `Capture.read()` pulls from one shared queue with
one intended consumer — this is why there is exactly one reader loop, not
one per session, and why `_on_ptt_start`/`_on_ptt_end` no longer own
`Capture`'s start/stop at all, only a boolean flag and a frame buffer.

Landing this required two preconditions docs/THREAT_MODEL_ALWAYS_ON.md
named explicitly and called a precondition for wiring the wake word in at
all, not a follow-up: a mute-while-speaking control between `Player` and
`WakeWord`/`Capture` (`nova.audio.duplex.DuplexGate`, T1.7/F26, already
built) so NOVA's own playback can't re-trigger its own detector, and a
decision capping what an acoustic-only arm can reach (F28, T2.2's
`Ladder.authorize(ceiling=...)` / `SkillCtx.armed_by`) so an external
source (a TV, a person who isn't Manav) saying something wake-phrase-shaped
can reach READ at most, never ACT/CONFIRM. Both were checked as landed,
not assumed, before this wiring went in.

Every skill call goes through `SkillSpec.invoke()` — never a handler
directly — because that is the seam `nova-dev-sec` wired `Ladder.authorize()`
into (`SkillCtx.ladder`, required, not optional-with-a-None-default). This
module constructs the process's `AuditLog` and `Ladder` at boot (per their
own docstrings: "one instance, constructed at boot"), calls `bind_audit()`
so `Untrusted.reveal()` is never running audit-less, and threads
`self.ladder` into every `SkillCtx` it builds. It never calls
`ladder.authorize()` itself — that would double-authorize (and double-audit)
every invocation, since `invoke()` already does it.

Fails loudly, never silently: every stage that can fail is caught, logged
with a WARNING/ERROR naming the stage, and answered with *something* spoken
(via `Synthesizer`, which already falls back to Windows SAPI) rather than
leaving Manav staring at silence.

`run_turn()` publishes `nova.governor.TOPIC_TURN_START`/`TOPIC_TURN_END`
around its entire body (`try`/`finally`, so every early return still ends
the turn) — the signal T1.2's `Governor` needs to never unload a component
mid-turn. This module never calls `Governor.check_once()` itself; the
governor drives its own poll loop once started.

T2.2: every turn also publishes per-stage `LatencyMark`s (`nova.telemetry`)
from `ptt_end` through `first_audio_out` — the moment `nova.audio.player`
actually hands real PCM to the output device, not when synthesis returns —
so latency is decomposed rather than reported as one number. See
`docs/T2.2_LATENCY.md` for the measured breakdown and
`docs/decisions/0011-prewarm-vs-idle-ram.md` for the pre-warm trade this
instrumentation exists to price.
"""

from __future__ import annotations

import asyncio
import queue
import time
from typing import Any

import numpy as np

import nova.skills  # import triggers discover_skills() (nova/skills/__init__.py)
from nova.audio.capture import Capture
from nova.audio.duplex import DuplexGate
from nova.audio.player import Player
from nova.audio.wake import TOPIC_WAKE_END, TOPIC_WAKE_START, WakeEvent, WakeWord
from nova.brain.router import Router
from nova.config import Config, load_config
from nova.governor import TOPIC_TURN_END, TOPIC_TURN_START, Governor
from nova.interfaces.hotkey import TOPIC_PTT_END, TOPIC_PTT_START, Hotkey, PTTEvent
from nova.interfaces.tray import TOPIC_SPEAK_END, TOPIC_SPEAK_START, Tray
from nova.log import get_logger
from nova.security.audit import AuditLog
from nova.security.confirm import DialogConfirmChannel
from nova.security.ladder import Ladder
from nova.security.tiers import ArmedBy
from nova.security.untrusted import bind_audit
from nova.skills.registry import Reply, SkillCtx
from nova.stt.transcriber import Transcriber
from nova.telemetry import TOPIC_LATENCY_MARK, LatencyLog, LatencyMark
from nova.tts.synthesizer import Synthesizer

_log = get_logger(__name__)

#: how often the capture reader thread re-checks the stop flag while
#: waiting for the next audio frame (Capture.read()'s timeout= parameter)
_CAPTURE_POLL_S = 0.2


class NovaApp:
    """Owns one instance of every M0 module and wires them through the bus.
    Construction is cheap and loads no models (Transcriber/Synthesizer are
    lazy: the underlying WhisperModel/Kokoro session loads on first real
    use, not at construction) -- see the T0.6b report for measured idle vs.
    mid-turn RSS/VRAM.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()

        from nova.bus import EventBus

        self.bus = EventBus()

        # --- security kernel, constructed at boot per Ladder's/AuditLog's
        # own docstrings; see module docstring for why this app does not
        # call ladder.authorize() itself.
        # redact_terms MUST be threaded from config here. Content-shape
        # redaction (paths, secrets, emails) is pattern-based and works without
        # it, which is exactly what makes omitting it dangerous: with the
        # default empty list, a wired-correctly boot and a not-wired-at-all boot
        # are indistinguishable, and the day Manav adds a private term to
        # config/nova.yaml it would silently keep appearing in every audit
        # record. Found by nova-security (F22).
        self.audit = AuditLog(redact_terms=self.config.security.redact_terms)
        bind_audit(self.audit)
        # T1.4: DialogConfirmChannel is what makes CONFIRM-tier skills
        # actually approvable -- before this, Ladder.authorize() failed
        # closed for every CONFIRM skill by design (no channel registered),
        # which was correct for M0 (zero CONFIRM skills) but would have
        # silently stayed that way forever if nothing ever attached a real
        # channel. M0 still ships zero CONFIRM skills, so this wiring is
        # exercised today only by the ladder's own dormant-but-ready branch,
        # not by any live skill yet.
        self.ladder = Ladder(
            self.audit,
            confirm_channel=DialogConfirmChannel(redact_terms=self.config.security.redact_terms),
        )

        # --- brain: router reads the live skill registry, populated by the
        # `import nova.skills` above.
        self.router = Router(
            confidence_floor=self.config.brain.confidence_floor,
            ambiguity_margin=self.config.brain.ambiguity_margin,
        )

        # --- speech I/O. Constructing these does not load a model; both
        # classes lazy-load on first real use (Transcriber.transcribe(),
        # Synthesizer.say()).
        self.transcriber = Transcriber()
        self.synth = Synthesizer()
        self.player = Player()
        self.hotkey = Hotkey(self.bus)

        # --- wake word (M1, docs/THREAT_MODEL_ALWAYS_ON.md). DuplexGate
        # subscribes itself to TOPIC_SPEAK_START/END at construction (its
        # own docstring: "one instance, constructed at boot") -- built
        # before WakeWord so it can be threaded in as WakeWord's gate=
        # from the start, never a gap where the detector could run ungated.
        # Neither loads a model here; WakeWord.start() (called from this
        # app's own start(), below) is what loads the ONNX sessions, and
        # degrades loudly on its own if the model files are missing (see
        # its own docstring) -- this app does not duplicate that check.
        self.duplex_gate = DuplexGate(self.bus)
        self.wake_word = WakeWord(self.bus, gate=self.duplex_gate)

        # --- tray icon (T1.4): subscribes itself to ptt/turn/speak topics
        # in its own __init__ (see nova.interfaces.tray's module docstring
        # for why TOPIC_SPEAK_* are owned there rather than here). Quit from
        # the tray menu requests a graceful shutdown via self._stop_event,
        # the same seam a future signal handler / CLI Ctrl+C could use.
        self._stop_event = asyncio.Event()
        self.tray = Tray(self.bus, on_quit=self._request_stop)

        # --- governor (T1.2): drives existing load()/unload() lifecycle,
        # never calls load() itself. typical_rss_mb hints are measured, not
        # guessed -- see docs/GOVERNOR_MEASUREMENTS.md.
        self.governor = Governor(self.bus, self.config.governor)
        self.governor.register("transcriber", self.transcriber, typical_rss_mb=780.0)
        self.governor.register("synth", self.synth, typical_rss_mb=1150.0)

        # --- capture: ONE instance, ONE continuous reader for the app's
        # whole lifetime (module docstring's "Capture's lifecycle changed"
        # section explains why). Constructed here so probing the input
        # device happens once, at boot, not on Manav's first PTT press;
        # started/stopped in start()/stop(), never inside a bus handler.
        self._capture = Capture()
        self._capture_reader_task: asyncio.Task[None] | None = None
        self._capture_reader_stop: asyncio.Event | None = None

        # --- PTT session state, populated per ptt.start/ptt.end pair. A
        # plain list the continuous reader appends to while `_capturing`,
        # not a dedicated per-session task any more -- there is only ever
        # one reader now.
        self._capturing = False
        self._ptt_frames: list[np.ndarray] = []

        # --- wake session state, populated per wake.start/wake.end pair,
        # same shape as the PTT state above and fed by the same continuous
        # reader. `preroll` (WakeEvent.wake.start's lead-in audio) seeds
        # the buffer in _on_wake_start so the phrase itself isn't lost --
        # WakeWord's own detection is inherently a little late relative to
        # when the phrase started (nova/audio/wake.py's own docstring).
        self._wake_capturing = False
        self._wake_frames: list[np.ndarray] = []

        # serializes turns -- a stray second ptt.start mid-turn must not
        # start a second, overlapping run_turn()
        self._turn_lock = asyncio.Lock()

        # --- latency instrumentation (T2.2): a turn_id counter (turns are
        # serialized by _turn_lock, so a plain incrementing int is enough --
        # no need for uuid4's collision-safety across concurrent turns that
        # can't happen), the one LatencyLog subscriber that turns marks into
        # a logged breakdown, and a scratch slot _speak() writes into from
        # Player's audio-thread callback (see _speak's docstring).
        self._turn_seq = 0
        self.latency_log = LatencyLog()
        self.bus.subscribe(TOPIC_LATENCY_MARK, self.latency_log.handle)
        self._last_synth_returned_t: float | None = None
        self._last_first_audio_t: float | None = None

        self.bus.subscribe(TOPIC_PTT_START, self._on_ptt_start)
        self.bus.subscribe(TOPIC_PTT_END, self._on_ptt_end)
        self.bus.subscribe(TOPIC_WAKE_START, self._on_wake_start)
        self.bus.subscribe(TOPIC_WAKE_END, self._on_wake_end)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Arm the hotkey and start listening. Loads no models except
        WakeWord's -- small (~1MB each, its own module docstring), always-
        resident, and not governor-managed (unlike Transcriber/Synth it is
        never unloaded while idle; there is no idle for an always-on
        detector to be idle *from*).

        Starts Capture once, for the app's whole lifetime, before either
        the wake word or the hotkey needs it -- see module docstring's
        "Capture's lifecycle changed" section. A missing/unusable input
        device degrades the same way the hotkey backend already does:
        loud WARNING, no exception, both PTT and the wake word are simply
        non-functional for this boot (there is no third path around "no
        microphone").
        """
        try:
            self._capture.start()
            self._capture_reader_stop = asyncio.Event()
            self._capture_reader_task = asyncio.create_task(self._continuous_capture_loop())
        except Exception as exc:
            _log.error(
                "Capture failed to start (%s) -- neither push-to-talk nor the wake word "
                "will produce any audio until this is fixed.",
                exc,
            )

        self.wake_word.start(loop=asyncio.get_running_loop())
        if not self.wake_word.backend_available:
            _log.warning(
                "Wake word unavailable (see the WARNING above naming why) -- "
                "NOVA will still respond to push-to-talk."
            )

        self.hotkey.start(loop=asyncio.get_running_loop())
        if not self.hotkey.backend_available:
            _log.warning(
                "NovaApp started but the hotkey backend is unavailable -- "
                "NOVA has no input path until this is fixed (see the WARNING "
                "above naming why)."
            )
        else:
            _log.info("NovaApp ready: hold %r to talk", self.hotkey.combo)
        # Degrades loudly on its own (see nova.interfaces.tray) -- a missing
        # tray icon must never mean a missing assistant, so its failure is
        # not checked here the way the hotkey's is; the hotkey is NOVA's
        # only input path, the tray is not.
        self.tray.start(loop=asyncio.get_running_loop())
        await self.governor.start()

    async def stop(self) -> None:
        """Tear down cleanly: stop the hotkey, the wake word, the
        continuous capture reader, the tray, the governor, release loaded
        models.

        Order matters: the reader task must actually finish (it is the
        only thing calling `self._capture.read()`) before `Capture.stop()`
        closes the stream underneath it, and the wake word stops before
        Capture so nothing calls `WakeWord.process()` on a frame read from
        a stream that's mid-teardown.
        """
        self.hotkey.stop()
        self.wake_word.stop()
        if self._capture_reader_stop is not None:
            self._capture_reader_stop.set()
        if self._capture_reader_task is not None:
            await self._capture_reader_task
            self._capture_reader_task = None
        self._capture.stop()
        self.tray.stop()
        await self.governor.stop()
        self.transcriber.unload()
        self.synth.unload()

    def _request_stop(self) -> None:
        """Wired as the tray's on_quit callback. Tray already marshals this
        onto the asyncio loop via nova.threadsafe.post_callback before
        calling it (see Tray._invoke_on_quit), so by the time this runs it
        is safe to touch asyncio state directly -- and if the loop had
        already closed by then, post_callback would have dropped the call
        before this method ever ran (F30)."""
        _log.info("Stop requested (tray Quit)")
        self._stop_event.set()

    async def wait_until_stopped(self) -> None:
        """Blocks until something (currently: the tray's Quit menu item)
        requests a graceful shutdown."""
        await self._stop_event.wait()

    # -- bus handlers ---------------------------------------------------

    async def _continuous_capture_loop(self) -> None:
        """Runs for the app's whole lifetime (launched once from start(),
        stopped from stop()) — the single consumer of `Capture.read()`,
        fanning each frame out to whichever of PTT/wake is actually
        capturing. See module docstring's "Capture's lifecycle changed"
        section for why there is exactly one of these rather than one per
        session.

        Suppression, not just routing: `WakeWord.process()` is skipped
        entirely while a PTT session is capturing (never fed the frame at
        all, not muted after the fact) -- the lead's ruling on why a wake
        re-arm inside a PTT utterance would only ever be redundant, never
        legitimate, and why suppressing is the fail-safe direction (it can
        only reduce arming, never permit more).
        """
        assert self._capture_reader_stop is not None
        stop_event = self._capture_reader_stop

        def _blocking_read() -> np.ndarray | None:
            try:
                return self._capture.read(timeout=_CAPTURE_POLL_S)
            except queue.Empty:
                return None
            except Exception as exc:
                _log.error("Capture.read() failed in the continuous loop (%s)", exc)
                return None

        while not stop_event.is_set():
            frame = await asyncio.to_thread(_blocking_read)
            if frame is None:
                continue
            if self._capturing:
                self._ptt_frames.append(frame)
            else:
                self.wake_word.process(frame)  # own broad try/except; safe to call from any thread/context
            if self._wake_capturing:
                self._wake_frames.append(frame)

    async def _on_ptt_start(self, event: PTTEvent) -> None:
        if self._capturing:
            _log.warning("ptt.start received while a capture is already in progress; ignoring")
            return
        if self._wake_capturing:
            # A human just grabbed the hotkey mid-wake-session (the wake
            # word fired, VAD is still watching for end-of-speech, and PTT
            # arrived before wake.end did). PTT is the stronger, more
            # deliberate signal -- abort the in-flight wake turn rather
            # than let both eventually fire; _turn_lock would only
            # serialize two overlapping replies to what was really one
            # utterance, not prevent the second one.
            _log.info("ptt.start: aborting an in-flight wake-armed session in favor of push-to-talk")
            self._wake_capturing = False
            self._wake_frames = []

        self._capturing = True
        self._ptt_frames = []
        _log.info("ptt.start: capture began")

    async def _on_ptt_end(self, event: PTTEvent) -> None:
        if not self._capturing:
            _log.warning("ptt.end received with no active capture; ignoring")
            return

        # T2.2: "speech ended" is measured here, at the instant this handler
        # starts running the PTT-release event it was dispatched -- the
        # closest this process gets to the real hardware moment (`event.ts`
        # is wall-clock `time.time()`, not comparable to the monotonic
        # marks the rest of the turn is measured in). turn_id is minted
        # here, before either capture-drain outcome, so both the "no
        # frames" apology and a real turn report under the same id.
        turn_id = self._next_turn_id()
        await self._mark(turn_id, "ptt_end")

        self._capturing = False
        frames = self._ptt_frames
        self._ptt_frames = []
        await self._mark(turn_id, "capture_drained")

        if not frames:
            _log.warning("ptt session (mode=%s) produced no audio frames", event.mode)
            await self._speak_and_mark("I didn't catch that.", turn_id)
            return

        pcm = np.concatenate(frames)
        duration_s = len(pcm) / 16000
        _log.info("ptt.end (mode=%s): captured %.2fs of audio", event.mode, duration_s)
        await self.run_turn(pcm, source=f"ptt:{event.mode}", armed_by=ArmedBy.PTT, turn_id=turn_id)

    async def _on_wake_start(self, event: WakeEvent) -> None:
        if self._wake_capturing:
            _log.warning("wake.start received while a wake capture is already in progress; ignoring")
            return
        if self._capturing:
            # Structurally shouldn't happen -- _continuous_capture_loop
            # never feeds WakeWord.process() while a PTT session is
            # capturing, so it cannot have detected anything to arm on.
            # Logged rather than silently accepted in case that invariant
            # is ever broken by a future change to the loop above.
            _log.warning("wake.start received during an active PTT session; ignoring (should be unreachable)")
            return
        self._wake_capturing = True
        # preroll is the audio immediately before detection -- the
        # classifier's own receptive field makes detection inherently a
        # little late relative to when the phrase actually started
        # (nova/audio/wake.py's own docstring); seeding the buffer with it
        # is how the wake phrase itself isn't lost from the transcript.
        self._wake_frames = [event.preroll] if event.preroll is not None else []
        _log.info("wake.start: wake word detected, listening for the command")

    async def _on_wake_end(self, event: WakeEvent) -> None:
        if not self._wake_capturing:
            _log.warning("wake.end received with no active wake capture; ignoring")
            return

        turn_id = self._next_turn_id()
        await self._mark(turn_id, "wake_end")

        self._wake_capturing = False
        frames = self._wake_frames
        self._wake_frames = []

        if not frames:
            _log.warning("wake session (mode=%s) produced no audio frames", event.mode)
            await self._speak_and_mark("I didn't catch that.", turn_id)
            return

        pcm = np.concatenate(frames)
        duration_s = len(pcm) / 16000
        _log.info("wake.end (mode=%s): captured %.2fs of audio", event.mode, duration_s)
        await self.run_turn(pcm, source="wake", armed_by=ArmedBy.WAKE, turn_id=turn_id)

    def _next_turn_id(self) -> str:
        """Mint the next turn_id -- see the T2.2 comment in __init__ for why
        a plain counter is safe here."""
        self._turn_seq += 1
        return f"t{self._turn_seq}"

    async def _mark(self, turn_id: str, stage: str, *, t: float | None = None, **meta: Any) -> None:
        """Publish one T2.2 latency mark on the bus. `t` defaults to now
        (`time.monotonic()`); callers that already captured the real
        crossing instant (e.g. `_speak_and_mark`, reading a timestamp
        `_speak()` recorded on another thread) pass it explicitly so the
        mark reflects when the event actually happened, not when this
        coroutine got around to publishing it."""
        await self.bus.publish(
            TOPIC_LATENCY_MARK,
            LatencyMark(turn_id=turn_id, stage=stage, t=t if t is not None else time.monotonic(), meta=meta),
        )

    async def _speak_and_mark(self, text: str, turn_id: str) -> None:
        """`await self._speak(text)`, then turn the instance-level
        timestamps it leaves behind into T2.2 bus marks under `turn_id`.

        Kept separate from `_speak()` itself so `_speak()`'s signature
        never needs a `turn_id` parameter: `test_app_turn_loop.py`
        (`test_turns_are_serialized_not_overlapping`) replaces the whole
        bound method with a text-only stand-in
        (`app._speak = slow_speak`), and a required extra argument here
        would break that substitution for a test this module doesn't own.
        """
        await self._speak(text)
        if self._last_synth_returned_t is not None:
            await self._mark(turn_id, "synth_returned", t=self._last_synth_returned_t)
        if self._last_first_audio_t is not None:
            await self._mark(turn_id, "first_audio_out", t=self._last_first_audio_t)
        else:
            # Audio never reached the device this utterance -- both TTS
            # engines failed, playback raised before a real frame, or a
            # monkeypatched _speak() never set it. Still publish the
            # terminal mark (flagged, not a real timestamp) so LatencyLog
            # flushes this turn_id instead of holding it forever -- see
            # LatencyLog's own docstring in nova/telemetry.py.
            await self._mark(turn_id, "first_audio_out", aborted=True)

    # -- the turn ---------------------------------------------------------

    async def run_turn(
        self,
        pcm: np.ndarray,
        *,
        source: str = "capture",
        armed_by: ArmedBy = ArmedBy.PTT,
        turn_id: str | None = None,
    ) -> Reply | None:
        """Run one full turn: STT -> router -> ladder-gated skill -> TTS.
        Returns the skill's Reply, or None if the turn ended in a spoken
        clarification or a caught failure (both cases still *speak*
        something -- never a silent no-op).

        `turn_id` correlates this turn's T2.2 latency marks with the
        `ptt_end`/`capture_drained` marks `_on_ptt_end` already published
        before calling this. Optional because `run_turn()` is also called
        directly -- `scripts/probe_turn.py`, the test suite -- without going
        through `_on_ptt_end` at all; a fresh id is minted rather than
        leaving marks unpublishable.

        `armed_by` (F28) is threaded into `SkillCtx` below, honestly --
        `source` stays a free-text log label (`"ptt:tap"`, `"wake"`, ...)
        and is deliberately NOT parsed to derive this; a security-relevant
        signal must come from a real typed value the caller passes on
        purpose, not from string-matching a label meant for humans reading
        a log line. `_on_ptt_end` passes `ArmedBy.PTT` explicitly (the
        default here matches it, since every non-turn-loop caller --
        scripts, tests -- is implicitly "as if a human pressed the
        hotkey", the unrestricted case); the wake word's handler passes
        `ArmedBy.WAKE`.
        """
        if turn_id is None:
            turn_id = self._next_turn_id()

        async with self._turn_lock:
            await self.bus.publish(TOPIC_TURN_START, None)
            try:
                # marks the instant STT actually starts -- distinct from
                # capture_drained/ptt_end so lock-acquisition/dispatch
                # overhead is visible on its own, and so direct run_turn()
                # callers (scripts, tests, no ptt_end mark at all) still get
                # an STT-duration delta instead of transcript_ready being
                # the first mark in the breakdown.
                await self._mark(turn_id, "stt_start")

                # --- STT -----------------------------------------------
                try:
                    text = await asyncio.to_thread(self.transcriber.transcribe, pcm)
                except Exception as exc:
                    _log.error("Turn (%s): Transcriber.transcribe() failed (%s)", source, exc)
                    await self._mark(turn_id, "abort:stt")
                    await self._speak_and_mark("Sorry, I had trouble hearing that.", turn_id)
                    return None
                await self._mark(turn_id, "transcript_ready")

                text = text.strip()
                if not text:
                    _log.info("Turn (%s): STT returned empty text; nothing to route", source)
                    await self._speak_and_mark("I didn't catch that.", turn_id)
                    return None
                # docs/THREAT_MODEL_ALWAYS_ON.md §1.4: a PTT-armed turn only
                # ever logs what Manav deliberately pressed the hotkey and
                # said -- full verbatim logging there is fine, and stays.
                # A wake-armed turn has no such bound: every detector fire,
                # including a false accept from a TV or ambient noise, would
                # otherwise write whatever the room said, verbatim, to
                # nova.log.LOG_PATH -- a local plaintext file the retired
                # build's own hotkey module already named as "a file that
                # later gets attached to a bug report." Gating this behind
                # a debug LOG LEVEL would not actually help: nova/log.py's
                # file handler is set to DEBUG (only the console handler
                # caps at INFO), so a debug-level call still lands in the
                # persistent file just as verbatim as INFO would. Length
                # only, never content, for a wake-armed turn -- the honest
                # fix, not a truncation cap that a short ambient snippet
                # would sail under anyway.
                if armed_by is ArmedBy.WAKE:
                    _log.info("Turn (%s): heard %d chars (wake-armed; content withheld from the log)", source, len(text))
                else:
                    _log.info("Turn (%s): heard %r", source, text)

                # --- router ------------------------------------------------
                try:
                    match = self.router.route(text)
                except Exception as exc:
                    # Same §1.4 reasoning as the "heard" log above -- an
                    # internal Router crash is rare, but a wake-armed turn
                    # hitting it must not be the one path that still leaks
                    # room audio verbatim into the log file.
                    if armed_by is ArmedBy.WAKE:
                        _log.error("Turn (%s): Router.route() failed on %d chars (wake-armed; content withheld) (%s)", source, len(text), exc)
                    else:
                        _log.error("Turn (%s): Router.route(%r) failed (%s)", source, text, exc)
                    await self._mark(turn_id, "abort:route")
                    await self._speak_and_mark("Sorry, something went wrong understanding that.", turn_id)
                    return None
                await self._mark(turn_id, "route_decided")

                if match.clarification is not None:
                    # Constitutional requirement, not an error path: below
                    # the confidence floor (or ambiguous), NOVA asks rather
                    # than guesses -- it never acts on a low-confidence
                    # match as if certain.
                    _log.info(
                        "Turn (%s): confidence %.3f -- asking for clarification instead of acting",
                        source, match.confidence,
                    )
                    await self._speak_and_mark(match.clarification, turn_id)
                    return None

                # --- skill, through the ladder's one entry point ----------
                assert match.spec is not None
                ctx = SkillCtx(config=self.config, ladder=self.ladder, armed_by=armed_by)
                try:
                    reply = await match.spec.invoke(ctx, **match.params)
                except Exception as exc:
                    _log.error("Turn (%s): skill %r raised (%s)", source, match.spec.name, exc)
                    await self._mark(turn_id, "abort:skill")
                    await self._speak_and_mark(f"Sorry, something went wrong with {match.spec.name}.", turn_id)
                    return None
                await self._mark(turn_id, "skill_returned")

                # --- TTS + playback ----------------------------------------
                await self._speak_and_mark(reply.text, turn_id)
                return reply
            finally:
                await self.bus.publish(TOPIC_TURN_END, None)

    async def _speak(self, text: str) -> None:
        """Synthesize and play `text`. NOVA must never go silently mute:
        Synthesizer already falls back to Windows SAPI if Kokoro fails, so
        the only way this logs an error is if *both* engines failed.

        Brackets its whole body with TOPIC_SPEAK_START/TOPIC_SPEAK_END
        (try/finally, so a synth or playback failure still ends the
        "speaking" state) -- the one place NOVA's tray icon (T1.4) can
        observe "speaking" distinctly from "thinking", mirroring how
        run_turn() already brackets itself with TOPIC_TURN_START/END for the
        governor.

        T2.2: records `self._last_synth_returned_t`/`self._last_first_audio_t`
        (reset to None at the top of every call) for `_speak_and_mark()` to
        turn into bus marks -- not parameters on this method itself, so its
        signature stays exactly `_speak(text)`. `test_app_turn_loop.py`
        replaces this whole bound method with a text-only stand-in
        (`test_turns_are_serialized_not_overlapping`), and this module does
        not own that test."""
        self._last_synth_returned_t = None
        self._last_first_audio_t = None
        await self.bus.publish(TOPIC_SPEAK_START, None)
        try:
            try:
                pcm, samplerate = await asyncio.to_thread(self.synth.say, text)
            except Exception as exc:
                _log.error(
                    "Synthesizer.say(%r) failed even with its SAPI fallback (%s); "
                    "NOVA cannot speak this turn", text, exc,
                )
                return
            self._last_synth_returned_t = time.monotonic()

            def _on_first_frame() -> None:
                # Runs on Player's realtime audio callback thread, not this
                # coroutine's. A plain attribute write is safe unguarded:
                # single writer, and the only reader (_speak_and_mark) only
                # looks at it after `await self.player.play(...)` below has
                # returned, which cannot happen until this callback either
                # already ran or never will (empty/cancelled playback).
                if self._last_first_audio_t is None:
                    self._last_first_audio_t = time.monotonic()

            try:
                await self.player.play(pcm, samplerate, on_first_frame=_on_first_frame)
            except Exception as exc:
                _log.error("Player.play() failed (%s); the reply was %r", exc, text)
        finally:
            await self.bus.publish(TOPIC_SPEAK_END, None)


async def _run() -> None:
    app = NovaApp()
    await app.start()
    try:
        await app.wait_until_stopped()  # runs until interrupted or tray Quit
    finally:
        await app.stop()


def run() -> None:
    """Entry point for `python -m nova`.

    Claims the single-instance lock (T1.4) before anything else -- in
    particular, before NovaApp() is ever constructed, since construction is
    what wires the hotkey/capture path that would contend for the
    microphone. Refuses to start a second instance rather than logging a
    warning and continuing: two NOVAs is exactly the bug
    nova.interfaces.tray.acquire_single_instance_lock exists to make
    impossible, not just unlikely.
    """
    from nova.interfaces.tray import acquire_single_instance_lock

    if not acquire_single_instance_lock():
        _log.error("NOVA is already running (or the single-instance check failed) -- exiting.")
        return

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
