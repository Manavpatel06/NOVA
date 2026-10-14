"""Voice activity detection: per-frame speech probability via Silero VAD.

Split out from nova.audio.wake (T1.1) rather than folded into it: this is
a genuinely reusable primitive (wake-word endpointing today, likely
barge-in detection during playback later), and keeping it a single-model,
single-responsibility wrapper is easier to review and test than a bigger
module that also happens to do VAD internally.

Model I/O verified directly against models/vad/silero_vad.onnx rather than
assumed from Silero's docs (versions differ):
  input:  [batch, samples]      float32, raw audio in [-1, 1]
  state:  [2, batch, 128]       float32, recurrent state threaded across calls
  sr:     scalar                int64, sample rate (16000 here)
  output: [batch, 1]            float32, speech probability
  stateN: [2, batch, 128]       float32, updated state for the next call

Chunk size matters and this export is picky about it: 480 and 512 samples
both work; 1024 and 1536 both threw an internal LSTM shape error on this
exact ONNX file ("Input X must have 3 dimensions only"), so this isn't a
documented Silero constraint being second-guessed, it's what this file
actually does. 480 samples = 30ms @ 16kHz, which is nova.audio.Capture's
default frame size (config/nova.yaml audio.frame_ms) -- process() takes a
Capture frame directly, no rebuffering needed.
"""
from __future__ import annotations

import numpy as np
import onnxruntime as ort

from nova import VAD_MODEL_PATH
from nova.log import get_logger

_log = get_logger(__name__)

DEFAULT_SAMPLERATE = 16000
_CHUNK = 512    # samples per model call at 16 kHz (Silero v5)
_CONTEXT = 64   # trailing samples of the previous chunk prepended to each call


class Vad:
    """Streaming, stateful speech/non-speech classifier.

    Usage:
        vad = Vad()
        vad.load()
        prob = vad.process(frame)   # frame: 16kHz mono float32, e.g. from Capture.read()
        vad.reset()                 # start a fresh utterance (clears recurrent state)
    """

    def __init__(self, model_path=None, samplerate: int = DEFAULT_SAMPLERATE):
        self._model_path = model_path or VAD_MODEL_PATH
        self._samplerate = samplerate
        self._session: ort.InferenceSession | None = None
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._context = np.zeros(_CONTEXT, dtype=np.float32)
        self._last_prob = 0.0
        self.available = False

    def load(self) -> None:
        """Load the ONNX session. Degrades loudly: a missing model file
        disables VAD (available=False) rather than crashing the caller --
        process() then returns 0.0 (treat as silence) so endpointing falls
        back to the arm-timeout ceiling instead of hanging forever."""
        if not self._model_path.exists():
            _log.warning(
                "VAD model missing at %s; VAD disabled. Endpointing will "
                "fall back to the arm-timeout ceiling only, not real "
                "silence detection.",
                self._model_path,
            )
            return
        try:
            # Same single-threaded fix as nova.audio.wake, and for the same
            # measured reason: onnxruntime's default thread pool adds
            # jitter (tens of ms of occasional spikes) on a model this
            # small; pinning to 1 thread removed it entirely in testing.
            sess_opts = ort.SessionOptions()
            sess_opts.intra_op_num_threads = 1
            sess_opts.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(self._model_path), sess_options=sess_opts, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:
            _log.warning("VAD model failed to load (%s); VAD disabled.", type(exc).__name__)
            return
        self.available = True

    def reset(self) -> None:
        """Clear recurrent state. Call at the start of each new utterance/
        turn so a previous turn's context doesn't bleed into the next."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._context = np.zeros(_CONTEXT, dtype=np.float32)
        self._last_prob = 0.0

    def process(self, frame: np.ndarray) -> float:
        """Return the latest speech probability in [0, 1]. Never raises and
        never logs the frame itself.

        FIXED 2026-10-14: frames used to go to the model as bare 480-sample
        chunks. The model only works when fed exactly the way Silero's own
        wrapper feeds it -- 512-sample chunks, each prefixed with the last
        64 samples of the previous one. Fed bare chunks it returned ~0.002
        for clear speech (measured on a TTS sample: 0 of 205 frames over
        0.5, versus 96 of 193 fed correctly), so NOVA never detected speech
        at all. Callers still pass 30 ms Capture frames; this re-buffers
        internally and returns the most recent chunk's probability."""
        if not self.available or self._session is None:
            return 0.0
        self._pending = np.concatenate([self._pending, np.asarray(frame, dtype=np.float32).reshape(-1)])
        sr = np.array(self._samplerate, dtype=np.int64)
        while len(self._pending) >= _CHUNK:
            chunk, self._pending = self._pending[:_CHUNK], self._pending[_CHUNK:]
            x = np.concatenate([self._context, chunk]).reshape(1, -1)
            try:
                out, new_state = self._session.run(None, {"input": x, "state": self._state, "sr": sr})
            except Exception as exc:
                _log.warning("VAD inference failed (%s); treating frame as silence", type(exc).__name__)
                self._last_prob = 0.0
                continue
            self._state = new_state
            self._context = chunk[-_CONTEXT:]
            self._last_prob = float(out.reshape(-1)[0])
        return self._last_prob
