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
# process(): returns the model's score, with the correct shapes/dtypes fed in
# ---------------------------------------------------------------------------


def test_process_returns_the_models_score(model_file, monkeypatch):
    fake_session = _FakeVadSession(score_fn=lambda feeds: 0.73)
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()
    assert vad.process(_frame(480)) == pytest.approx(0.73)


def test_process_reshapes_the_frame_to_batch_one(model_file, monkeypatch):
    fake_session = _FakeVadSession()
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()
    vad.process(_frame(480))
    fed = fake_session.run_calls[0]["input"]
    assert fed.shape == (1, 480)
    assert fed.dtype == np.float32


def test_process_passes_the_configured_samplerate(model_file, monkeypatch):
    fake_session = _FakeVadSession()
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file, samplerate=8000)
    vad.load()
    vad.process(_frame(480))
    fed = fake_session.run_calls[0]["sr"]
    assert int(fed) == 8000


# ---------------------------------------------------------------------------
# process()/reset(): recurrent state is threaded, never silently reset
# ---------------------------------------------------------------------------


def test_process_threads_recurrent_state_from_one_call_into_the_next(model_file, monkeypatch):
    fake_session = _FakeVadSession()
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()

    vad.process(_frame(480))
    fed_state_call1 = fake_session.run_calls[0]["state"]
    assert np.array_equal(fed_state_call1, np.zeros((2, 1, 128), dtype=np.float32))

    vad.process(_frame(480))
    fed_state_call2 = fake_session.run_calls[1]["state"]
    # call 2 must receive exactly what call 1's session returned as the new
    # state (the fake's marker: input state + 1) -- not a fresh zero state.
    assert np.array_equal(fed_state_call2, np.ones((2, 1, 128), dtype=np.float32))


def test_reset_clears_recurrent_state_back_to_zero(model_file, monkeypatch):
    fake_session = _FakeVadSession()
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()

    vad.process(_frame(480))  # state becomes non-zero (ones, per the fake's marker)
    vad.reset()
    vad.process(_frame(480))

    fed_after_reset = fake_session.run_calls[-1]["state"]
    assert np.array_equal(fed_after_reset, np.zeros((2, 1, 128), dtype=np.float32))


def test_state_shape_and_dtype_match_the_silero_recurrent_state_contract(model_file, monkeypatch):
    fake_session = _FakeVadSession()
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()
    vad.process(_frame(480))
    fed = fake_session.run_calls[0]["state"]
    assert fed.shape == (2, 1, 128)
    assert fed.dtype == np.float32


# ---------------------------------------------------------------------------
# The chunk-size claim: 480/512 accepted; anything else fails inside the
# model, and process() must swallow that -- never raise, treat as silence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [480, 512])
def test_process_accepts_the_documented_chunk_sizes(model_file, monkeypatch, n):
    fake_session = _FakeVadSession(accepted_sizes=(480, 512), score_fn=lambda feeds: 0.42)
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()
    assert vad.process(_frame(n)) == pytest.approx(0.42)


@pytest.mark.parametrize("n", [1024, 1536])
def test_process_swallows_the_internal_shape_error_for_rejected_chunk_sizes(
    model_file, monkeypatch, caplog, n
):
    fake_session = _FakeVadSession(accepted_sizes=(480, 512))
    _install_fake_ort(monkeypatch, session=fake_session)
    vad = Vad(model_path=model_file)
    vad.load()
    with caplog.at_level("WARNING"):
        result = vad.process(_frame(n))  # must not raise
    assert result == 0.0
    assert any("inference failed" in r.message.lower() for r in caplog.records)


def test_process_never_raises_even_on_a_completely_broken_session(model_file, monkeypatch):
    class _BrokenSession:
        def run(self, output_names, feeds):
            raise ValueError("simulated catastrophic ONNX failure")

    _install_fake_ort(monkeypatch, session=_BrokenSession())
    vad = Vad(model_path=model_file)
    vad.load()
    assert vad.process(_frame(480)) == 0.0  # never raises out of process()


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
        vad.process(_frame(480))
    joined = " ".join(r.message for r in caplog.records)
    assert "SUPER_SECRET_DETAIL" not in joined
    assert "ValueError" in joined


# ---------------------------------------------------------------------------
# Real hardware (opt-in): the accepted-chunk-size claim against the actual
# on-disk silero_vad.onnx, not a stand-in. Off by default -- a real ONNX
# session load and several real inferences, not a unit test.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_TEST_REAL_HARDWARE") != "1",
    reason="set NOVA_TEST_REAL_HARDWARE=1 to load the real silero_vad.onnx model",
)
class TestRealSileroVadModel:
    def test_480_and_512_sample_chunks_are_accepted(self):
        vad = Vad()
        vad.load()
        assert vad.available is True
        for n in (480, 512):
            prob = vad.process(_frame(n))
            assert 0.0 <= prob <= 1.0

    def test_1024_and_1536_sample_chunks_are_rejected_but_do_not_crash(self, caplog):
        vad = Vad()
        vad.load()
        with caplog.at_level("WARNING"):
            for n in (1024, 1536):
                assert vad.process(_frame(n)) == 0.0
        assert any("inference failed" in r.message.lower() for r in caplog.records)
