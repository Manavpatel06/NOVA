"""Wake word: 'hey jarvis' (Manav's stand-in until 'hey nova' exists --
training one is a free ~30-minute Colab job, done later; a wake-word
model is a small network trained on one specific phrase, so there is no
generic "hey nova" available today).

Backend is a from-scratch onnxruntime pipeline, NOT the `openwakeword`
PyPI package. That package's utils.py does `import requests` unconditionally
at module load -- disqualifying under this codebase's egress model ("ONE
sanctioned module opens sockets, a test proves nothing else does"). Instead
this module re-implements the inference pipeline directly against
onnxruntime, an existing dependency, based on reading openwakeword's source
(Apache 2.0) for the algorithm and then verifying every shape against the
actual on-disk models rather than trusting either the source or memory of
it (see the T1.1 report for the real numbers):

  melspectrogram.onnx : input float32 holding INT16-MAGNITUDE values (not
                         normalized -1..1) -- confirmed by reading
                         openwakeword's input validation, then verified by
                         running it. 1.5s of audio (24000 samples) -> 147
                         mel-frames (~163 samples/frame, ~10ms hop).
  embedding_model.onnx : wants exactly 76 mel-frames x 32 bins -> outputs a
                         96-dim embedding. Confirmed via get_inputs().
  <wake>.onnx (classifier) : wants exactly N embeddings x 96-dim -> one
                         score in [0,1]. N is READ from the model's own
                         input shape at load time (16 for hey_jarvis_v0.1),
                         not hardcoded, so a differently-shaped hey_nova.onnx
                         still works with zero code changes here.

Rather than replicate openwakeword's incremental buffer-indexing algorithm
(fragile -- their own code comment admits streaming vs. full-recompute
differ slightly and it's "unclear if [the difference is] significant"),
this module recomputes the melspectrogram over a small bounded ring of
recent raw audio on each detection step and takes the last 76 frames.
Costs more CPU than the incremental version; see the T1.1 report for
measured latency/RAM, which is what determines whether that tradeoff was
acceptable, not assumption.

PRIVACY: this is an always-on detector processing every second of audio in
Manav's home. Unlike nova.interfaces.hotkey (which discards non-combo keys
entirely, zero buffering), wake detection genuinely needs a short window of
recent audio -- that's a sanctioned, bounded exception, not a loophole:
  - the ring buffer is FIXED SIZE (computed once at init, never grows).
  - it is never written to disk.
  - its contents never appear in a log line -- logs carry only counts,
    scores, states, and durations, never raw samples.
  - the only place raw audio leaves this module is the `preroll` field on
    a wake.start WakeEvent, published in-process on the bus to whatever
    needs it downstream (STT) -- not logged, not persisted here.

Mirrors nova.interfaces.hotkey's bus contract (ptt.start/ptt.end) as
closely as makes sense, so the turn loop can treat push-to-talk and the
wake word as interchangeable arming sources: wake.start / wake.end topics,
WakeEvent(ts, mode) with mode None on start and a reason string on end.

Does NOT open its own audio device. Fed 16kHz mono float32 frames by
whatever owns the single shared nova.audio.Capture instance -- opening a
second InputStream on the same device here would conflict with Capture's
own device selection (T0.2).
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np
import onnxruntime as ort

from nova import WAKE_MODEL_DIR
from nova.audio.vad import Vad
from nova.bus import EventBus
from nova.log import get_logger

_log = get_logger(__name__)

TOPIC_WAKE_START = "wake.start"
TOPIC_WAKE_END = "wake.end"

SAMPLERATE = 16000

DEFAULT_MODEL_FILENAME = "hey_jarvis_v0.1.onnx"
# The one knob Manav will actually tune. 0.55 is openWakeWord's own
# documented ballpark for a usable false-accept/false-reject tradeoff on
# their pretrained models; not independently measured against real usage
# here yet (that needs a human saying the phrase repeatedly over time).
DEFAULT_THRESHOLD = 0.55
# Audio kept before the detection instant. The classifier's own receptive
# field is ~760ms (76 mel-frames @ ~10ms hop), so detection is inherently a
# little late relative to when the phrase started -- 1000ms covers that
# plus real processing latency, without dragging in much irrelevant lead-in
# silence. Documented choice, not a measured constant (same honesty as the
# hotkey's tap-threshold default).
DEFAULT_PREROLL_MS = 1000
# Hard ceiling if VAD never detects a gap -- same failsafe reasoning as
# nova.interfaces.hotkey's latch_timeout_ms. Ordinary commands should end
# via VAD well before this fires.
DEFAULT_ARM_TIMEOUT_MS = 15_000
# Consecutive trailing silence (VAD) that ends an armed turn.
DEFAULT_VAD_SILENCE_MS = 800

# -- pipeline constants, verified empirically against the real on-disk
# models (see module docstring) --
MEL_STEP_SAMPLES = 1280  # one detection step per 80ms of new audio (matches openWakeWord's native step rate)
EMBED_MEL_FRAMES = 76  # mel-frames per embedding window
DSP_CONTEXT_MS = 1000  # audio needed to reliably yield >= EMBED_MEL_FRAMES mel-frames (empirically ~760ms; rounded up with margin)

# Silero VAD's own documented default speech/silence cutoff.
VAD_SPEECH_THRESHOLD = 0.5
# Time after arming before silence starts counting toward ending the turn,
# so a natural pre-speech pause (the beat between the wake word and the
# command) doesn't end things instantly. Internal constant, not exposed as
# config -- keeps the config surface to the 5 fields actually requested.
SPEECH_GRACE_S = 0.5


def _default_wake_config():
    """WakeConfig is requested from nova-dev-core (T1.1) but may not have
    landed in nova.config yet. Degrade loudly, not silently -- same
    pattern used for HotkeyConfig before it landed."""
    try:
        from nova.config import load_config

        cfg = load_config()
        wake_cfg = getattr(cfg, "wake", None)
        if wake_cfg is not None:
            return wake_cfg
        _log.warning(
            "nova.config.Config has no 'wake' section yet; using built-in "
            "defaults (model=%r, threshold=%.2f). Ask nova-dev-core about "
            "landing WakeConfig.",
            DEFAULT_MODEL_FILENAME, DEFAULT_THRESHOLD,
        )
    except Exception as exc:
        _log.warning(
            "Could not load config/nova.yaml (%s); using built-in wake defaults",
            exc,
        )
    return None


class _State(Enum):
    IDLE = "idle"    # scanning for the wake word
    ARMED = "armed"  # wake word fired; VAD watching for end-of-speech


@dataclass(frozen=True)
class WakeEvent:
    ts: float
    # None for wake.start; "vad_end" / "arm_timeout" for wake.end.
    mode: str | None = None
    # Raw PCM immediately preceding detection. Only set on wake.start;
    # never logged, never persisted -- see the module PRIVACY note.
    preroll: np.ndarray | None = None


class WakeWord:
    """Always-on wake-word detector. See module docstring for the full
    design (backend choice, privacy discipline, device-ownership rule)."""

    def __init__(
        self,
        bus: EventBus,
        model_path: str | Path | None = None,
        threshold: float | None = None,
        preroll_ms: int | None = None,
        arm_timeout_ms: int | None = None,
        vad_silence_ms: int | None = None,
        vad: Vad | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        cfg = _default_wake_config()
        resolved_model = model_path or (cfg.model_path if cfg is not None else DEFAULT_MODEL_FILENAME)
        self._model_path = self._resolve_model_path(resolved_model)
        self._melspec_path = WAKE_MODEL_DIR / "melspectrogram.onnx"
        self._embedding_path = WAKE_MODEL_DIR / "embedding_model.onnx"

        self._threshold = threshold if threshold is not None else (
            cfg.threshold if cfg is not None else DEFAULT_THRESHOLD
        )
        self._preroll_ms = preroll_ms if preroll_ms is not None else (
            getattr(cfg, "preroll_ms", DEFAULT_PREROLL_MS) if cfg is not None else DEFAULT_PREROLL_MS
        )
        arm_timeout_ms_v = arm_timeout_ms if arm_timeout_ms is not None else (
            getattr(cfg, "arm_timeout_ms", DEFAULT_ARM_TIMEOUT_MS) if cfg is not None else DEFAULT_ARM_TIMEOUT_MS
        )
        vad_silence_ms_v = vad_silence_ms if vad_silence_ms is not None else (
            getattr(cfg, "vad_silence_ms", DEFAULT_VAD_SILENCE_MS) if cfg is not None else DEFAULT_VAD_SILENCE_MS
        )
        self._arm_timeout_s = arm_timeout_ms_v / 1000.0
        self._vad_silence_s = vad_silence_ms_v / 1000.0

        self._bus = bus
        self._loop = loop
        self._vad = vad or Vad()

        ring_ms = max(DSP_CONTEXT_MS, self._preroll_ms) + 200  # margin
        self._ring_max_samples = int(SAMPLERATE * ring_ms / 1000)
        self._dsp_context_samples = int(SAMPLERATE * DSP_CONTEXT_MS / 1000)
        self._preroll_samples = int(SAMPLERATE * self._preroll_ms / 1000)
        self._ring = np.zeros(0, dtype=np.float32)

        self._samples_since_infer = 0
        self._embeddings: deque = deque(maxlen=1)  # resized to the real classifier window in start()

        self._state = _State.IDLE
        self._time_armed_s = 0.0
        self._silence_run_s = 0.0
        self._arm_timer: threading.Timer | None = None
        self._state_lock = threading.Lock()
        self._start_stop_lock = threading.Lock()

        self.backend_available = False
        self._melspec_session: ort.InferenceSession | None = None
        self._embedding_session: ort.InferenceSession | None = None
        self._classifier_session: ort.InferenceSession | None = None
        self._melspec_input_name = "input"
        self._embedding_input_name = "input_1"
        self._classifier_input_name = "x.1"

    @staticmethod
    def _resolve_model_path(model_path: str | Path) -> Path:
        p = Path(model_path)
        return p if p.is_absolute() else WAKE_MODEL_DIR / p

    # -- lifecycle -----------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Load the ONNX sessions and VAD. Idempotent. Degrades loudly:
        any missing model file logs a WARNING naming it and leaves
        backend_available=False -- never boots healthy while listening for
        a wake word that doesn't exist."""
        with self._start_stop_lock:
            if self.backend_available:
                return

            if loop is not None:
                self._loop = loop
            elif self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    _log.warning(
                        "WakeWord.start() called with no running asyncio loop "
                        "and none provided; wake events cannot be published."
                    )
                    return

            missing = [
                p for p in (self._model_path, self._melspec_path, self._embedding_path)
                if not p.exists()
            ]
            if missing:
                _log.warning(
                    "Wake word model file(s) missing: %s. Wake detection "
                    "disabled -- NOVA will not respond to the wake word "
                    "until this is fixed.",
                    ", ".join(str(p) for p in missing),
                )
                return

            try:
                # intra/inter_op_num_threads=1: measured, not assumed --
                # onnxruntime's default multi-threaded CPU provider adds
                # thread-pool wake-up jitter on models this small (~1MB
                # each). Isolated single-inference cost was already fast
                # (~1-2ms) but with occasional 40-140ms spikes under the
                # default threading; pinning to 1 thread made those spikes
                # disappear entirely across 60 consecutive calls (see the
                # T1.1 report). These models are tiny enough that a single
                # thread is already faster than the synchronization cost of
                # spreading the work across a pool.
                sess_opts = ort.SessionOptions()
                sess_opts.intra_op_num_threads = 1
                sess_opts.inter_op_num_threads = 1
                self._melspec_session = ort.InferenceSession(
                    str(self._melspec_path), sess_options=sess_opts, providers=["CPUExecutionProvider"]
                )
                self._embedding_session = ort.InferenceSession(
                    str(self._embedding_path), sess_options=sess_opts, providers=["CPUExecutionProvider"]
                )
                self._classifier_session = ort.InferenceSession(
                    str(self._model_path), sess_options=sess_opts, providers=["CPUExecutionProvider"]
                )
            except Exception as exc:
                _log.warning(
                    "Wake word ONNX session(s) failed to load (%s). Wake "
                    "detection disabled.",
                    type(exc).__name__,
                )
                return

            self._melspec_input_name = self._melspec_session.get_inputs()[0].name
            self._embedding_input_name = self._embedding_session.get_inputs()[0].name
            clf_input = self._classifier_session.get_inputs()[0]
            self._classifier_input_name = clf_input.name
            classifier_window = clf_input.shape[1]
            if not isinstance(classifier_window, int) or classifier_window <= 0:
                _log.warning(
                    "Classifier model %s reports a non-fixed embedding "
                    "window (%r); wake detection disabled.",
                    self._model_path.name, classifier_window,
                )
                return
            self._embeddings = deque(maxlen=classifier_window)
            with self._state_lock:
                self._warmup_locked()

            self._vad.load()
            if not self._vad.available:
                _log.warning(
                    "VAD unavailable; wake-word turns will only end via "
                    "arm_timeout_ms (%.0fs), not real silence detection.",
                    self._arm_timeout_s,
                )

            self.backend_available = True
            with self._state_lock:
                self._state = _State.IDLE
            _log.info(
                "Wake word armed: model=%s threshold=%.2f preroll_ms=%d "
                "arm_timeout_ms=%d vad_silence_ms=%d",
                self._model_path.name, self._threshold, self._preroll_ms,
                int(self._arm_timeout_s * 1000), int(self._vad_silence_s * 1000),
            )

    def stop(self) -> None:
        with self._start_stop_lock:
            with self._state_lock:
                self._cancel_arm_timer_locked()
                self._state = _State.IDLE
            self.backend_available = False
            self._ring = np.zeros(0, dtype=np.float32)
            _log.info("Wake word stopped")

    # -- streaming processing -------------------------------------------

    def process(self, frame: np.ndarray) -> None:
        """Feed one 16kHz mono float32 frame (as produced by
        Capture.read()). Safe to call from any thread; a safe no-op if the
        backend never loaded (see start()). Wrapped in a broad except so an
        internal DSP/ONNX hiccup can never take down whatever loop is
        driving Capture -- logs only the exception type name, never audio
        data (see module PRIVACY note)."""
        if not self.backend_available:
            return
        try:
            frame = np.asarray(frame, dtype=np.float32).reshape(-1)
            if frame.size == 0:
                return
            frame_duration_s = frame.size / SAMPLERATE

            with self._state_lock:
                self._push_to_ring(frame)

                if self._state == _State.IDLE:
                    self._samples_since_infer += frame.size
                    if self._samples_since_infer >= MEL_STEP_SAMPLES:
                        self._samples_since_infer = 0
                        score = self._run_detection_step()
                        if score >= self._threshold:
                            self._arm_locked(score)

                elif self._state == _State.ARMED:
                    prob = self._vad.process(frame)
                    self._time_armed_s += frame_duration_s
                    if prob >= VAD_SPEECH_THRESHOLD:
                        self._silence_run_s = 0.0
                    elif self._time_armed_s >= SPEECH_GRACE_S:
                        self._silence_run_s += frame_duration_s

                    if self._silence_run_s >= self._vad_silence_s:
                        self._end_turn_locked(mode="vad_end")
        except Exception as exc:
            _log.warning(
                "WakeWord.process() raised %s; continuing (state preserved "
                "where possible, no audio in this log line).",
                type(exc).__name__,
            )

    def _warmup_locked(self) -> None:
        """Pre-fill the ring and the embeddings window with silence.

        Bug found and fixed during T1.1 verification: without this, the
        classifier window starts empty and _run_detection_step() refuses to
        score anything until it has accumulated a full classifier_window
        (e.g. 16) of SEQUENTIALLY, REALLY computed embeddings on top of the
        ~1s of ring context each embedding itself needs -- roughly 1s +
        16*80ms = ~2.3s of continuous audio before the classifier ever
        produces a single non-placeholder score. A real wake-word
        utterance (~1-1.5s) plus reasonable lead-in scrolls out of the ring
        before that cold start ever finishes, so nothing is ever detected
        within a realistic utterance length -- confirmed by a real
        acoustic test that produced a 0.0 peak score end to end on a clip
        that a reference implementation scored 0.998 on the identical
        audio; the reference avoids this by pre-filling its own window
        with (randomly generated) embeddings at construction time. Silence
        is the more honest choice here since NOVA genuinely boots into
        silence, not noise, and it lets the classifier window start
        "full" and progressively absorb real content from the first real
        frame onward instead of gating on a cold start that a normal
        utterance can never outlast."""
        self._ring = np.zeros(self._dsp_context_samples, dtype=np.float32)
        while len(self._embeddings) < self._embeddings.maxlen:
            self._run_detection_step()

    def _push_to_ring(self, frame: np.ndarray) -> None:
        self._ring = np.concatenate([self._ring, frame])
        if len(self._ring) > self._ring_max_samples:
            self._ring = self._ring[-self._ring_max_samples:]

    def _run_detection_step(self) -> float:
        """One melspec -> embedding -> classifier pass over the current
        ring tail. Returns a score in [0, 1]; 0.0 if there isn't enough
        buffered audio yet (startup transient) or a model returned an
        unexpected shape."""
        if len(self._ring) < self._dsp_context_samples:
            return 0.0
        audio = self._ring[-self._dsp_context_samples:]
        # melspectrogram.onnx wants float32 holding int16-magnitude values,
        # not normalized -1..1 (verified against the real model; see module
        # docstring).
        x = (audio * 32767.0).astype(np.float32)[None, :]
        mel_out = self._melspec_session.run(None, {self._melspec_input_name: x})[0]
        mel = np.squeeze(mel_out)
        if mel.ndim != 2:
            return 0.0
        mel = mel / 10.0 + 2.0  # openWakeWord's documented melspec transform
        if mel.shape[0] < EMBED_MEL_FRAMES:
            return 0.0
        window = mel[-EMBED_MEL_FRAMES:].astype(np.float32)[None, :, :, None]
        emb_out = self._embedding_session.run(None, {self._embedding_input_name: window})[0]
        embedding = np.squeeze(emb_out).reshape(-1)
        self._embeddings.append(embedding)
        if len(self._embeddings) < self._embeddings.maxlen:
            return 0.0
        clf_input = np.stack(list(self._embeddings))[None, :, :].astype(np.float32)
        score_out = self._classifier_session.run(None, {self._classifier_input_name: clf_input})[0]
        return float(np.squeeze(score_out))

    # -- state transitions (caller must hold self._state_lock) ----------

    def _arm_locked(self, score: float) -> None:
        self._state = _State.ARMED
        self._time_armed_s = 0.0
        self._silence_run_s = 0.0
        self._vad.reset()
        preroll = self._ring[-self._preroll_samples:].copy()
        self._arm_timer_start_locked()
        _log.info(
            "Wake word detected (score=%.3f >= threshold=%.2f); armed, preroll=%dms",
            score, self._threshold, self._preroll_ms,
        )
        self._publish(TOPIC_WAKE_START, WakeEvent(ts=time.time(), preroll=preroll))

    def _end_turn_locked(self, mode: str) -> None:
        self._state = _State.IDLE
        self._cancel_arm_timer_locked()
        self._embeddings.clear()
        self._samples_since_infer = 0
        _log.info("Wake turn ended (mode=%s)", mode)
        self._publish(TOPIC_WAKE_END, WakeEvent(ts=time.time(), mode=mode))

    # -- arm-timeout failsafe: ARMED must never be able to last forever --

    def _cancel_arm_timer_locked(self) -> None:
        if self._arm_timer is not None:
            self._arm_timer.cancel()
            self._arm_timer = None

    def _arm_timer_start_locked(self) -> None:
        self._cancel_arm_timer_locked()
        timer = threading.Timer(self._arm_timeout_s, self._on_arm_timeout)
        timer.daemon = True
        self._arm_timer = timer
        timer.start()

    def _on_arm_timeout(self) -> None:
        """Runs on the Timer's own thread -- guard against racing a real
        VAD-detected end or a stop() call landing at the same moment
        (same pattern as nova.interfaces.hotkey's latch timeout)."""
        with self._state_lock:
            self._arm_timer = None
            if self._state != _State.ARMED or not self.backend_available:
                return
            self._end_turn_locked(mode="arm_timeout")

    def _publish(self, topic: str, payload: WakeEvent) -> None:
        if self._loop is None:
            _log.warning("WakeWord has no event loop to publish %r on; dropping event", topic)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._bus.publish(topic, payload), self._loop)
        except Exception as exc:
            _log.warning("Failed to schedule bus publish for %r: %s", topic, exc)
