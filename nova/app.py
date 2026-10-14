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

**T2.4: when the router misses, Claude proposes and NOVA disposes**
(docs/M2_OPEN_BRAIN_PLAN.md, docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md).
Only the router's below-confidence-floor case falls through
(`_run_claude_fallthrough`'s own docstring states, and argues, why an
ambiguous match or a missing-param clarification stay local instead — both
are cases NOVA can resolve by asking, faster and for free, where Claude
would cost a multi-second round trip and a transcript egress to arrive at
a guess). `nova.brain.claude_arm.ask()` returns a structured
`ClaudeAnswer` (spoken, no action) or `ClaudeProposal` — bound and
authorized through the *identical* `SkillSpec.invoke()` / `Ladder.
authorize()` path a router match goes through, `SkillCtx.origin=
Origin.CLAUDE` applying T2.4's provenance floor (`floor=Tier.CONFIRM`,
READ exempt per F47) the same way `armed_by` applies F28's presence
ceiling — there is no second path to a tool call, and Claude cannot
invent a capability NOVA doesn't already have (an unrecognized
`skill_name` is refused and audited, never attempted). An immediate
spoken acknowledgment covers "say something immediately" (a multi-second
silence reads as a hang, not thinking) — see `_run_claude_fallthrough`'s
docstring for how that interacts honestly with T2.2's `first_audio_out`
metric rather than gaming it.

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
import inspect
import queue
import time
from typing import Any

import numpy as np

import nova.skills  # import triggers discover_skills() (nova/skills/__init__.py)
from nova.audio.capture import Capture
from nova.audio.duplex import DuplexGate
from nova.audio.player import Player
from nova.audio.utterance import UtteranceSegmenter
from nova.audio.vad import Vad
from nova.audio.wake import TOPIC_WAKE_END, TOPIC_WAKE_START, WakeEvent, WakeWord
from nova.brain.address import strip_address
from nova.brain import claude_arm
from nova.brain.claude_arm import ClaudeAnswer, ClaudeFailure, ClaudeProposal
from nova.brain.router import Router
from nova.config import Config, load_config
from nova.governor import TOPIC_TURN_END, TOPIC_TURN_START, Governor
from nova.interfaces.hotkey import TOPIC_PTT_END, TOPIC_PTT_START, Hotkey, PTTEvent
from nova.interfaces.tray import TOPIC_SPEAK_END, TOPIC_SPEAK_START, Tray
from nova.interfaces.window import TOPIC_UI_HEARD, TOPIC_UI_SAID, TOPIC_UI_STATE
from nova.log import get_logger
from nova.security.audit import AuditLog
from nova.security.confirm import DialogConfirmChannel
from nova.security.egress import Egress
from nova.security.ladder import Ladder
from nova.security.tiers import ArmedBy, Origin
from nova.security.untrusted import bind_audit
from nova.skills.registry import Reply, SkillCtx, get_skill
from nova.stt.transcriber import Transcriber
from nova.telemetry import TOPIC_LATENCY_MARK, LatencyLog, LatencyMark
from nova.tts.synthesizer import Synthesizer

_log = get_logger(__name__)


#: how often the capture reader thread re-checks the stop flag while
#: waiting for the next audio frame (Capture.read()'s timeout= parameter)
_CAPTURE_POLL_S = 0.2

#: T2.4: wall-clock budget for one Claude fallthrough round trip. Lower
#: than claude_arm.DEFAULT_TIMEOUT_S (30s) on purpose -- that default was
#: picked for a different verification (claude_arm's own kill-on-timeout
#: proof, docs/T2.4A_CLAUDE_ARM.md) and is explicitly labelled a
#: placeholder there too. 30s here specifically means run_turn()'s
#: _turn_lock -- held for this call's entire duration -- makes NOVA
#: unresponsive to a fresh PTT press for up to 30s on a hang; there is no
#: barge-in rescue for this window either, since barge-in cancels
#: *playback* and NOVA is silent while waiting on Claude. 15s, same
#: provisional-value-with-its-reasoning-attached style as DuplexGate's
#: hangover_ms: a starting number, not a measured one, to be replaced once
#: the claude_dispatched/claude_returned marks below produce a real
#: distribution to tune it against.
_CLAUDE_TIMEOUT_S = 15.0

#: T2.4: spoken before awaiting claude_arm.ask() -- "say something
#: immediately" (docs/M2_OPEN_BRAIN_PLAN.md T2.4), because a multi-second
#: silence reads as a hang, not thinking. This IS the turn's real
#: first_audio_out (see run_turn()'s Claude-fallthrough branch for why
#: that's an honest, not a misleading, use of the mark).
_CLAUDE_ACK_TEXT = "Let me think about that."

#: T2.4: a plain spoken failure per reason (docs/THREAT_MODEL_CLAUDE_
#: FALLTHROUGH.md finding 6 -- "never fail silently, not hang", and never
#: partially/best-effort act on a failure either). `None` is the fallback
#: for a reason string this dict doesn't recognize (defensive: claude_arm's
#: own ClaudeFailure.reason is documented as one of a fixed set, but this
#: app must not crash speaking about a failure just because that set grew
#: without this dict being updated to match).
_CLAUDE_FAILURE_TEXT: dict[str | None, str] = {
    "cli_not_found": "I can't reach Claude right now.",
    "spawn_failed": "I couldn't start that.",
    "timeout": "That took too long, sorry.",
    "process_error": "Something went wrong on my end.",
    "model_error": "I got an error back, sorry.",
    "unparseable_output": "I got a garbled response, sorry.",
    None: "Sorry, I couldn't get an answer for that.",
}


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
        # T2.4: the one sanctioned egress boundary, for claude_arm.ask()'s
        # release_for_process() call. One instance, constructed here and
        # reused for the app's lifetime -- Egress.__init__ has a real
        # process-wide side effect (installs a urllib opener that refuses
        # every redirect), so constructing it per-call would mean
        # reinstalling that opener on every Claude fallthrough turn for no
        # reason. allowlist is the real configured one (egress_allowlist),
        # not an empty stand-in -- release_for_process() doesn't consult it
        # today, but Egress is meant to be the one instance this app ever
        # constructs, and a real caller should never hand it a fake
        # allowlist "because this particular call doesn't need it".
        self.egress = Egress(
            self.config.security.egress_allowlist,
            audit=self.audit,
            redact_terms=self.config.security.redact_terms,
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
        self.transcriber = Transcriber(language=self.config.stt.language)
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
        self.window = None
        self.tray = None
        if self.config.ui.window:
            from nova.interfaces.window import NovaWindow

            self.window = NovaWindow(
                self.bus,
                on_close=self._request_stop,
                on_text=self._on_typed_text,
                on_mic_toggle=self._on_mic_toggle,
            )
        else:
            self.tray = Tray(self.bus, on_quit=self._request_stop)

        # --- prefix wake (Manav, 2026-10-14): while the window is open,
        # every utterance is transcribed locally and acted on ONLY if it
        # starts with NOVA's name (nova/brain/address.py). The detector-
        # based wake word above stays available as wake.mode = "model".
        # An unrecognised mode fails CLOSED to "model" (wake-armed turns are
        # READ-only), never open to prefix's action-capable voice commands.
        if self.config.wake.mode not in ("prefix", "model"):
            _log.warning("wake.mode %r is not 'prefix' or 'model' -- using the restrictive 'model' mode", self.config.wake.mode)
        self._prefix_mode = self.config.wake.mode == "prefix"
        self.vad = Vad()
        self.segmenter = UtteranceSegmenter(
            self.vad,
            silence_ms=self.config.wake.vad_silence_ms,
            max_ms=self.config.wake.max_utterance_ms,
            min_speech_ms=self.config.wake.min_speech_ms,
        )
        self._listening = True          # mic toggle in the window
        self._stopping = False
        self._follow_up_until = 0.0     # monotonic deadline after a bare "Nova"
        self._utterance_tasks: set[asyncio.Task[None]] = set()

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

        if self._prefix_mode:
            self.vad.load()
            if self.vad.available:
                _log.info("Listening for sentences that start with %s", " / ".join(repr(n) for n in self.config.wake.names))
            else:
                _log.warning("VAD model unavailable -- 'Nova ...' voice commands are off; the hotkey and typing still work.")
        else:
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
        if self.window is not None:
            self.window.start(loop=asyncio.get_running_loop())
        if self.tray is not None:
            self.tray.start(loop=asyncio.get_running_loop())
        await self.governor.start()
        await self._ui_state("idle")

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
        self._stopping = True
        self._listening = False
        self.hotkey.stop()
        if not self._prefix_mode:
            self.wake_word.stop()
        if self._utterance_tasks:
            # let in-flight transcriptions finish (they now see _stopping and
            # do nothing) so Whisper is never unloaded mid-transcribe
            await asyncio.wait(list(self._utterance_tasks), timeout=5.0)
        if self._capture_reader_stop is not None:
            self._capture_reader_stop.set()
        if self._capture_reader_task is not None:
            await self._capture_reader_task
            self._capture_reader_task = None
        self._capture.stop()
        if self.tray is not None:
            self.tray.stop()
        if self.window is not None:
            self.window.stop()
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
        _log.info("Stop requested (%s)", "window closed" if self.window is not None else "tray Quit")
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
            elif self._prefix_mode:
                self._feed_prefix_listener(frame)
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
        self.segmenter.reset()
        _log.info("ptt.start: capture began")
        await self._ui_state("listening")

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

    # -- prefix wake + window -------------------------------------------

    def _feed_prefix_listener(self, frame: np.ndarray) -> None:
        """Called for every mic frame in prefix mode (from the capture loop).
        Frames are not even segmented while the mic is muted from the
        window, while NOVA is talking (DuplexGate -- it must not hear its
        own voice), or while a turn is running."""
        if not self._listening or not self.vad.available:
            return
        if not self.duplex_gate.mic_open() or self._turn_lock.locked():
            if self.segmenter.in_speech:
                self.segmenter.reset()
            return
        utterance = self.segmenter.process(frame)
        if utterance is not None:
            task = asyncio.create_task(self._on_utterance(utterance))
            self._utterance_tasks.add(task)
            task.add_done_callback(self._utterance_tasks.discard)

    async def _on_utterance(self, pcm: np.ndarray) -> None:
        """One utterance heard while NOVA's window is open. Transcribed
        locally; acted on only if it starts with NOVA's name (or arrives
        inside the follow-up window after a bare "Nova"). Everything else
        is dropped: its text is never logged, displayed or kept."""
        if self._turn_lock.locked():
            return
        async with self._turn_lock:
            # bracket STT like a turn so the governor never unloads Whisper
            # mid-transcription (its own docstring's undefined-behaviour case)
            await self.bus.publish(TOPIC_TURN_START, None)
            try:
                text = await asyncio.to_thread(self.transcriber.transcribe, pcm)
            except Exception as exc:
                _log.error("Prefix listener: transcription failed (%s)", exc)
                return
            finally:
                await self.bus.publish(TOPIC_TURN_END, None)

        # muted, or shutting down, while this utterance was being transcribed
        if not self._listening or self._stopping:
            return

        text = (text or "").strip()
        command = strip_address(text, self.config.wake.names)

        if time.monotonic() < self._follow_up_until:
            # Follow-up after a bare "Nova": the next real sentence is the
            # command, name optional. Noise (empty text) and another bare
            # "Nova" neither consume nor extend the window -- the deadline
            # set when "Yes?" finished is the hard limit (security review
            # 2026-10-14: re-arming on noise kept it open indefinitely).
            if command is None:
                command = text
            if not command:
                return
            self._follow_up_until = 0.0  # exactly one sentence
            await self.run_text(command, source="voice", armed_by=ArmedBy.VOICE)
            return

        if command is None:
            _log.debug("Prefix listener: speech not addressed to NOVA -- ignored")
            return
        if command == "":
            await self._acknowledge_bare_name()
            return
        await self.run_text(command, source="voice", armed_by=ArmedBy.VOICE)

    async def _acknowledge_bare_name(self) -> None:
        """Bare "Nova": say "Yes?" and open the follow-up window. Spoken
        under the turn lock (nothing else may talk over it), and the window
        starts only AFTER the reply finished playing, so NOVA's own "Yes?"
        cannot land inside it."""
        if self._turn_lock.locked():
            return
        async with self._turn_lock:
            await self.bus.publish(TOPIC_TURN_START, None)
            try:
                await self._ui_state("listening")
                await self._speak("Yes?")
            finally:
                await self.bus.publish(TOPIC_TURN_END, None)
        self.segmenter.reset()
        self._follow_up_until = time.monotonic() + self.config.wake.follow_up_ms / 1000.0

    async def _on_typed_text(self, text: str) -> None:
        """Window text box -> a turn. Typing is the same as asking."""
        text = (text or "").strip()
        if text:
            await self.run_text(text, source="typed", armed_by=ArmedBy.TYPED)

    async def _on_mic_toggle(self, enabled: bool) -> None:
        self._listening = bool(enabled)
        self.segmenter.reset()
        self._follow_up_until = 0.0
        _log.info("Voice listening %s from the window", "resumed" if enabled else "paused")
        await self._ui_state("idle" if enabled else "muted")

    async def _ui_state(self, state: str) -> None:
        if not self._listening and state == "idle":
            state = "muted"
        await self.bus.publish(TOPIC_UI_STATE, state)

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

                return await self._handle_text(text, source=source, armed_by=armed_by, turn_id=turn_id)
            finally:
                await self.bus.publish(TOPIC_TURN_END, None)
                await self._ui_state("idle")

    async def run_text(
        self,
        text: str,
        *,
        source: str = "typed",
        armed_by: ArmedBy = ArmedBy.TYPED,
        turn_id: str | None = None,
    ) -> Reply | None:
        """Run one turn from text that is already known -- typed into NOVA's
        window, or a prefix-wake command already transcribed and stripped of
        "Nova". Same router -> ladder -> skill/Claude -> speech path as
        run_turn(), minus STT."""
        if turn_id is None:
            turn_id = self._next_turn_id()
        async with self._turn_lock:
            await self.bus.publish(TOPIC_TURN_START, None)
            try:
                await self._mark(turn_id, "transcript_ready")
                return await self._handle_text(text, source=source, armed_by=armed_by, turn_id=turn_id)
            finally:
                await self.bus.publish(TOPIC_TURN_END, None)
                await self._ui_state("idle")

    async def _handle_text(
        self, text: str, *, source: str, armed_by: ArmedBy, turn_id: str,
    ) -> Reply | None:
        """Everything after speech has become text: route, act, reply.
        Caller holds `_turn_lock` and brackets TOPIC_TURN_START/END."""

        if text.strip():
            await self.bus.publish(TOPIC_UI_HEARD, text.strip())
            await self._ui_state("thinking")
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
            # T2.4: below the router's OWN confidence floor (the
            # exact value it was constructed with) and no local
            # skill matched at all -- "the router finds no skill"
            # in docs/M2_OPEN_BRAIN_PLAN.md's own words. This is
            # the only clarification sub-case that falls through to
            # Claude; see _run_claude_fallthrough's own docstring
            # for why ambiguous / missing-param stay local instead.
            if match.confidence < self.config.brain.confidence_floor:
                return await self._run_claude_fallthrough(
                    text, source=source, armed_by=armed_by, turn_id=turn_id,
                )
            # Constitutional requirement, not an error path: an
            # ambiguous match or a confidently-identified skill
            # missing a param -- NOVA asks rather than guesses (or
            # defers to a model that has no more insight into
            # Manav's own intent than NOVA already does).
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

    def _compose_claude_prompt(self, text: str) -> str:
        """T2.4's "minimal context" list (docs/M2_OPEN_BRAIN_PLAN.md),
        literally: the transcript, plus the registered skill names and
        descriptions -- nothing else. No file contents, no conversation
        history (this method is stateless -- called fresh every turn, no
        instance state carried across calls), no machine details.

        Skill descriptions come from `SkillSpec.description` (T2.4, a
        field written for exactly this -- see its own comment for why it's
        deliberately not `handler.__doc__`). Parameter names are derived
        live via `inspect.signature()`, excluding `ctx`, never hand-
        maintained here -- the identical technique `Router._param_names()`
        already uses privately; duplicated as three lines rather than
        importing a private method across files.

        The WHOLE returned string is what `claude_arm.ask()` passes to
        `Egress.release_for_process()` -- one call, one redaction pass,
        covering the transcript AND every skill description together
        (docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md finding 8: route the
        entire outbound payload through redact(), not just the
        transcript). This function does not call release_for_process()
        itself; composing the prompt and releasing it are different jobs,
        and claude_arm.ask() already owns the second one.
        """
        from nova.skills.registry import all_skills

        lines = [f'The user said: "{text}"', "", "Registered skills you may propose:"]
        for spec in sorted(all_skills().values(), key=lambda s: s.name):
            param_names = [p for p in inspect.signature(spec.handler).parameters if p != "ctx"]
            params_desc = f"Params: {', '.join(param_names)}." if param_names else "No parameters."
            lines.append(f"- {spec.name}: {spec.description} {params_desc}")
        return "\n".join(lines)

    async def _run_claude_fallthrough(
        self, text: str, *, source: str, armed_by: ArmedBy, turn_id: str,
    ) -> Reply | None:
        """T2.4: the router found no skill it was confident enough to act
        on. "Claude proposes, NOVA disposes" (docs/M2_OPEN_BRAIN_PLAN.md) --
        `claude_arm.ask()` returns a structured `ClaudeAnswer` (spoken, no
        action) or `ClaudeProposal` (bound and authorized through the
        SAME `SkillSpec.invoke()` / `Ladder.authorize()` path a router
        match goes through, `SkillCtx.origin=Origin.CLAUDE` applying the
        provenance floor -- never a second path to a tool call).

        Only called for the router's below-floor case (see run_turn()'s
        call site) -- deliberately NOT for an ambiguous match (the router
        already found two real local candidates with nothing about the
        ambiguity a model can resolve better than just asking) or a
        missing-param clarification (the router already identified the
        right skill confidently; asking "which app?" is faster, free, and
        certainly correct, where Claude would cost a multi-second round
        trip, an egress of the transcript, and a model GUESSING the same
        argument NOVA could ask for directly -- confidence-honesty in its
        purest form: NOVA already knows exactly what it doesn't know).

        Speaks `_CLAUDE_ACK_TEXT` immediately, via `_speak_and_mark` --
        this turn's real `first_audio_out`, honestly: it IS the first
        sound Manav hears (T2.2's whole point is time-to-first-sound, not
        turn-complete). The eventual real reply plays via a plain
        `_speak()` afterward with no second marks under this turn_id
        (`LatencyLog._flush()` pops and logs on the first `first_audio_out`
        -- a second one would misrepresent or double-log, neither right).
        To keep the round trip itself from hiding behind a fast-looking
        ack, its own duration gets its own PROMINENT, separately-labelled
        log line (not folded into the mark-sorted breakdown as just one
        more stage) -- same principle as F46's audit/display split: two
        readers, two needs, neither served by collapsing them.
        """
        prompt = self._compose_claude_prompt(text)
        await self._mark(turn_id, "claude_dispatched")
        await self._speak_and_mark(_CLAUDE_ACK_TEXT, turn_id)

        t0 = time.monotonic()
        try:
            result = await claude_arm.ask(
                prompt, egress=self.egress, audit=self.audit, timeout_s=_CLAUDE_TIMEOUT_S,
            )
        except Exception as exc:
            _log.error("Turn (%s): claude_arm.ask() raised (%s)", source, exc)
            await self._mark(turn_id, "abort:claude")
            await self._speak("Sorry, something went wrong reaching Claude.")
            return None
        elapsed = time.monotonic() - t0
        await self._mark(turn_id, "claude_returned")
        # The prominent line: always logged, whatever the outcome, so
        # nobody skimming this turn's entry concludes it was fast because
        # the ack's own first_audio_out was.
        _log.info("Turn (%s): Claude round trip %.2fs -> %s", source, elapsed, type(result).__name__)

        if isinstance(result, ClaudeFailure):
            # docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md finding 6: a plain
            # spoken failure, never a silent fallthrough to guessing
            # locally with the router's second-best score -- that would be
            # the same confidence-honesty violation with an unusual
            # trigger. No retry; Manav can simply ask again.
            _log.warning("Turn (%s): Claude fallthrough failed (%s: %s)", source, result.reason, result.detail)
            await self._speak(_CLAUDE_FAILURE_TEXT.get(result.reason, _CLAUDE_FAILURE_TEXT[None]))
            return None

        if isinstance(result, ClaudeAnswer):
            await self._speak(result.text)
            return Reply(text=result.text, success=True)

        assert isinstance(result, ClaudeProposal)
        spec = get_skill(result.skill_name)
        if spec is None:
            # docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md finding 8: an
            # unrecognized skill_name is a refusal, not an attempt --
            # Claude cannot invent a capability NOVA doesn't have. Audited
            # as its own distinct event kind (not an ordinary ladder
            # denial, which requires a real SkillSpec this by definition
            # isn't) so a pattern of repeated unknown-name proposals is
            # visible to whoever reviews the log later.
            _log.warning("Turn (%s): Claude proposed unknown skill_name=%r; refusing", source, result.skill_name)
            self.ladder.record_event(
                "claude.propose_unknown_skill", {"skill_name": result.skill_name, "params": result.params},
            )
            await self._speak("Sorry, I can't do that.")
            return None

        ctx = SkillCtx(config=self.config, ladder=self.ladder, armed_by=armed_by, origin=Origin.CLAUDE)
        try:
            reply = await spec.invoke(ctx, **result.params)
        except Exception as exc:
            _log.error("Turn (%s): Claude-proposed skill %r raised (%s)", source, spec.name, exc)
            await self._speak(f"Sorry, something went wrong with {spec.name}.")
            return None
        await self._speak(reply.text)
        return reply

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
        await self.bus.publish(TOPIC_UI_SAID, text)

        # Text mode (config `tts.speak: false`). Deliberate muting, which is a
        # different thing from the "NOVA must never go silently mute" promise
        # above -- that promise is about synthesis *failing*. Here nothing has
        # failed and nothing is wrong, so this logs at INFO and emits the reply
        # on stdout, and must never log a WARNING/ERROR or reach the SAPI
        # fallback: a user who asked for text should not be told something
        # broke. Speak/turn topics are still published so the tray keeps
        # showing state, and the latency marks simply have no first_audio_out
        # for this turn, which is honest -- there was no audio.
        if not self.config.tts.speak:
            _log.info("Turn reply (text mode, not spoken): %s", text)
            print(f"NOVA: {text}", flush=True)
            return

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
        _tell_user_already_running()
        return

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


def _tell_user_already_running() -> None:
    """A second launch used to vanish silently (no console in windowed
    mode), which looked exactly like NOVA being broken. Say so instead."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            0, "NOVA is already open -- look for its window on the taskbar.", "NOVA", 0x40 | 0x40000,
        )
    except Exception:  # noqa: BLE001 -- not Windows / no desktop: the log line above is enough
        pass
