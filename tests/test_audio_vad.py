"""Contract: docs/CONTRACTS.md T1.1 — nova.audio.vad.Vad.

    class Vad:
        def load(self); def reset(self); def process(self, frame) -> float   # 480-sample frames

Model I/O and the accepted-chunk-size claim are pinned against a fake
onnxruntime.InferenceSession — no real model file is ever loaded by the
mocked tests below, so nothing here depends on models/vad/silero_vad.onnx
being on disk or its bytes being valid. `nova.audio.vad.ort` is replaced
wholesale with a small fake so the real `onnxruntime` package is never
touched by these tests.

Confirmed empirically (module docstring, and independently by the lead):
this specific ONNX export accepts only 480- or 512-sample chunks; 1024 and
1536 both throw an internal LSTM shape error ("Input X must have 3
dimensions only") on this exact file. The mocked tests below pin
Vad.process()'s *handling* of that failure (caught, logged, treated as
silence — never raises); the real-hardware-gated class at the bottom
re-confirms the same claim against the actual on-disk model rather than
trusting the docstring.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

import nova.audio.vad as vad_mod
from nova.audio.vad import Vad


class _FakeSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = None
        self.inter_op_num_threads = None


class _FakeVadSession:
    """Stands in for onnxruntime.InferenceSession for silero_vad.onnx.

    accepted_sizes: chunk sample counts that succeed; any other size raises,
    mirroring the real ONNX export's internal LSTM shape error on this file.
    The returned "new state" is deterministically `input_state + 1`, purely
    as a marker so tests can prove state is threaded from one call's output
    into the next call's input rather than recomputed or reset.
    """

    def __init__(self, accepted_sizes=(480, 512), score_fn=None):
        self.accepted_sizes = set(accepted_sizes)
        self.score_fn = score_fn or (lambda feeds: 0.5)
        self.run_calls: list[dict] = []

    def run(self, output_names, feeds):
        n = feeds["input"].shape[1]
        if n not in self.accepted_sizes:
            raise RuntimeError(f"Input X must have 3 dimensions only (chunk={n})")
        self.run_calls.append(feeds)
        score = float(self.score_fn(feeds))
        out = np.array([[score]], dtype=np.float32)
        new_state = feeds["state"] + 1.0
        return [out, new_state]


class _FakeORT:
    def __init__(self, session=None, create_raises=None):
        self.SessionOptions = _FakeSessionOptions
        self._session = session
        self._create_raises = create_raises
        self.created_paths: list[str] = []

    def InferenceSession(self, path, sess_options=None, providers=None):
        self.created_paths.append(str(path))
        if self._create_raises is not None:
            raise self._create_raises
        return self._session


@pytest.fixture
def model_file(tmp_path) -> Path:
    """A real file on disk (parent dir name has a space — Windows path
    stressor per CLAUDE.md) so Vad.load()'s existence check passes without
    ever touching the real silero_vad.onnx. Its bytes are never read: `ort`
    is faked in every test in this module."""
    d = tmp_path / "vad models (fake)"
    d.mkdir()
    p = d / "silero_vad.onnx"
    p.write_bytes(b"not a real onnx file -- ort is faked in this module")
    return p


def _install_fake_ort(monkeypatch, **kw):
    fake = _FakeORT(**kw)
    monkeypatch.setattr(vad_mod, "ort", fake)
    return fake


def _frame(n: int, value: float = 0.0) -> np.ndarray:
    return np.full(n, value, dtype=np.float32)


# ---------------------------------------------------------------------------
# load(): never boots deaf-and-silent
# ---------------------------------------------------------------------------


def test_load_missing_model_file_disables_vad_with_a_named_warning(tmp_path, caplog):
    missing = tmp_path / "does-not-exist" / "silero_vad.onnx"
    vad = Vad(model_path=missing)
    with caplog.at_level("WARNING"):
        vad.load()
    assert vad.available is False
    assert any(str(missing) in r.message for r in caplog.records)


def test_load_session_creation_failure_disables_vad_loudly(model_file, monkeypatch, caplog):
    _install_fake_ort(monkeypatch, create_raises=RuntimeError("bad onnx bytes"))
    vad = Vad(model_path=model_file)
    with caplog.at_level("WARNING"):
        vad.load()
    assert vad.available is False
    assert any("failed to load" in r.message.lower() for r in caplog.records)


def test_load_success_sets_available_true(model_file, monkeypatch):
    fake_session = _FakeVadSession()
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()
    assert vad.available is True


# ---------------------------------------------------------------------------
# process(): safe no-op when unavailable, never raises
# ---------------------------------------------------------------------------


def test_process_before_load_returns_zero_and_does_not_raise():
    vad = Vad()
    assert vad.process(_frame(480)) == 0.0


def test_process_after_a_failed_load_returns_zero(tmp_path):
    vad = Vad(model_path=tmp_path / "missing.onnx")
    vad.load()
    assert vad.process(_frame(480)) == 0.0


# ---------------------------------------------------------------------------
# process(): the model must be fed exactly as Silero's own wrapper feeds it --
# 512-sample chunks, each prefixed with the previous chunk's last 64 samples.
# Before 2026-10-14 NOVA fed bare 480-sample frames; the real model then
# scored clear speech ~0.002 (0 of 205 frames over 0.5) and NOVA never
# detected speech at all. These tests pin the corrected contract.
# ---------------------------------------------------------------------------


def _loaded(model_file, monkeypatch, **session_kw):
    fake_session = _FakeVadSession(accepted_sizes=(576,), **session_kw)
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()
    return vad, fake_session


def test_model_input_is_512_samples_plus_64_of_context(model_file, monkeypatch):
    vad, sess = _loaded(model_file, monkeypatch)
    vad.process(_frame(480))
    assert sess.run_calls == []  # 480 < 512: buffered, not yet run
    vad.process(_frame(480))
    fed = sess.run_calls[0]["input"]
    assert fed.shape == (1, 576)
    assert fed.dtype == np.float32


def test_context_is_the_tail_of_the_previous_chunk(model_file, monkeypatch):
    vad, sess = _loaded(model_file, monkeypatch)
    audio = np.arange(512 * 3, dtype=np.float32)
    for i in range(0, len(audio), 480):
        vad.process(audio[i:i + 480])
    first, second = sess.run_calls[0]["input"][0], sess.run_calls[1]["input"][0]
    assert np.array_equal(first[:64], np.zeros(64, dtype=np.float32))  # nothing before the first chunk
    assert np.array_equal(first[64:], audio[:512])
    assert np.array_equal(second[:64], audio[448:512])  # last 64 of chunk 1
    assert np.array_equal(second[64:], audio[512:1024])


def test_no_audio_is_dropped_or_duplicated_across_frames(model_file, monkeypatch):
    vad, sess = _loaded(model_file, monkeypatch)
    audio = np.arange(480 * 32, dtype=np.float32)  # 30 model chunks exactly
    for i in range(0, len(audio), 480):
        vad.process(audio[i:i + 480])
    fed = np.concatenate([c["input"][0][64:] for c in sess.run_calls])
    assert np.array_equal(fed, audio[: len(fed)])
    assert len(fed) == 512 * (len(audio) // 512)


def test_process_returns_the_latest_models_score(model_file, monkeypatch):
    vad, _ = _loaded(model_file, monkeypatch, score_fn=lambda feeds: 0.73)
    assert vad.process(_frame(480)) == 0.0  # no chunk complete yet
    assert vad.process(_frame(480)) == pytest.approx(0.73)


def test_process_passes_the_configured_samplerate(model_file, monkeypatch):
    fake_session = _FakeVadSession(accepted_sizes=(576,))
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file, samplerate=8000)
    vad.load()
    vad.process(_frame(512))
    assert int(fake_session.run_calls[0]["sr"]) == 8000


def test_process_threads_recurrent_state_from_one_call_into_the_next(model_file, monkeypatch):
    vad, sess = _loaded(model_file, monkeypatch)
    vad.process(_frame(1024))  # two chunks
    assert np.array_equal(sess.run_calls[0]["state"], np.zeros((2, 1, 128), dtype=np.float32))
    assert np.array_equal(sess.run_calls[1]["state"], np.ones((2, 1, 128), dtype=np.float32))


def test_reset_clears_state_context_and_buffered_audio(model_file, monkeypatch):
    vad, sess = _loaded(model_file, monkeypatch)
    vad.process(np.full(512 + 300, 5.0, dtype=np.float32))  # one chunk run, 300 samples pending
    vad.reset()
    vad.process(_frame(512))
    last = sess.run_calls[-1]
    assert np.array_equal(last["state"], np.zeros((2, 1, 128), dtype=np.float32))
    assert np.array_equal(last["input"][0], np.zeros(576, dtype=np.float32))  # no stale context/pending audio


def test_state_shape_and_dtype_match_the_silero_recurrent_state_contract(model_file, monkeypatch):
    vad, sess = _loaded(model_file, monkeypatch)
    vad.process(_frame(512))
    fed = sess.run_calls[0]["state"]
    assert fed.shape == (2, 1, 128)
    assert fed.dtype == np.float32


def test_process_never_raises_even_on_a_completely_broken_session(model_file, monkeypatch):
    class _BrokenSession:
        def run(self, output_names, feeds):
            raise ValueError("simulated catastrophic ONNX failure")

    _install_fake_ort(monkeypatch, session=_BrokenSession())
    vad = Vad(model_path=model_file)
    vad.load()
    assert vad.process(_frame(512)) == 0.0  # never raises out of process()


def test_process_inference_failure_log_names_only_the_exception_type(model_file, monkeypatch, caplog):
    """Same discipline as nova.interfaces.hotkey's F10 fix: the log line may
    name the exception type, never echo its message (which could, in a
    differently-shaped failure, carry something derived from the audio)."""

    class _BrokenSession:
        def run(self, output_names, feeds):
            raise ValueError("SUPER_SECRET_DETAIL")

    _install_fake_ort(monkeypatch, session=_BrokenSession())
    vad = Vad(model_path=model_file)
    vad.load()
    with caplog.at_level("WARNING"):
        vad.process(_frame(512))
    joined = " ".join(r.message for r in caplog.records)
    assert "SUPER_SECRET_DETAIL" not in joined
    assert "ValueError" in joined


# ---------------------------------------------------------------------------
# Real hardware (opt-in): the accepted-chunk-size claim against the actual
# on-disk silero_vad.onnx, not a stand-in. Off by default -- a real ONNX
# session load and several real inferences, not a unit test.
# ---------------------------------------------------------------------------


_REAL_MODEL = vad_mod.VAD_MODEL_PATH
_SPEECH = Path(__file__).parent / "fixtures" / "speech_espeak_16k.wav"


@pytest.mark.skipif(not _REAL_MODEL.exists(), reason="real silero_vad.onnx not on this machine")
class TestRealSileroVadModel:
    """Against the real model file. This is the test that would have caught
    the 2026-10-14 bug: fed incorrectly, the real model scored clear speech
    at ~0.002, so NOVA never heard anyone."""

    def _frames(self, audio):
        return [audio[i:i + 480] for i in range(0, len(audio) - 480, 480)]

    def test_detects_real_speech_in_30ms_capture_frames(self):
        import soundfile as sf

        speech, sr = sf.read(str(_SPEECH), dtype="float32")
        assert sr == 16000
        rng = np.random.default_rng(0)
        audio = np.concatenate([np.zeros(16000, np.float32), speech, np.zeros(16000, np.float32)])
        audio = audio + (rng.standard_normal(len(audio)) * 0.003).astype(np.float32)
        vad = Vad()
        vad.load()
        probs = [vad.process(f) for f in self._frames(audio)]
        assert sum(p > 0.5 for p in probs) > 40  # ~4 s of speech
        assert max(probs[:20]) < 0.5            # the leading quiet isn't speech

    def test_silence_is_not_speech(self):
        vad = Vad()
        vad.load()
        rng = np.random.default_rng(1)
        audio = (rng.standard_normal(16000 * 3) * 0.003).astype(np.float32)
        assert max(vad.process(f) for f in self._frames(audio)) < 0.5

    def test_any_frame_size_works(self):
        vad = Vad()
        vad.load()
        for n in (160, 480, 512, 1024, 1536):
            assert 0.0 <= vad.process(_frame(n)) <= 1.0
