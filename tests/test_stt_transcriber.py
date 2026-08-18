"""Contract: docs/CONTRACTS.md T0.3 — nova.stt.Transcriber.

    class Transcriber:
        def load(self) -> None; def unload(self) -> None
        def transcribe(self, pcm: np.ndarray) -> str      # 16 kHz mono float32 in

Uses large-v3-turbo on whichever device/compute type probe_gpu() actually
verified — never a merely-advertised compute type (see test_stt_gpu.py for
that hazard directly). Transcriber's own constructor accepts an optional
gpu_profile injection (not part of the formal T0.3 signature, but the seam
that makes it testable without a real GPU) — tests use that seam to avoid
running a real probe_gpu() in the fast/mocked tests.

faster_whisper.WhisperModel is imported lazily inside Transcriber.load()
(`from faster_whisper import WhisperModel`), so it's mocked by patching the
attribute on the real `faster_whisper` module itself — that's the only
patch target a deferred `from X import Y` will actually observe.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from nova.stt.gpu import GpuProfile
from nova.stt.transcriber import Transcriber


class _FakeSegment:
    def __init__(self, text):
        self.text = text


class _FakeWhisperModel:
    instances: list["_FakeWhisperModel"] = []

    def __init__(self, model_name, device, compute_type, download_root, local_files_only):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.local_files_only = local_files_only
        self.transcribe_calls: list = []
        _FakeWhisperModel.instances.append(self)

    def transcribe(self, pcm):
        self.transcribe_calls.append(pcm)
        segments = iter([_FakeSegment(" hello "), _FakeSegment("world ")])
        return segments, object()


@pytest.fixture
def fake_whisper_model(monkeypatch):
    import faster_whisper

    _FakeWhisperModel.instances.clear()
    monkeypatch.setattr(faster_whisper, "WhisperModel", _FakeWhisperModel)
    return _FakeWhisperModel


@pytest.fixture
def cuda_profile() -> GpuProfile:
    return GpuProfile(
        device="cuda", compute_type="float16", verified=True,
        gpu_name="Fake GPU", driver="1.0", ct2_version="4.0", dll_directories=[],
    )


@pytest.fixture
def no_probe(monkeypatch):
    """A probe_gpu that fails the test if called — every test here injects
    a gpu_profile explicitly and must never trigger a real probe."""
    import nova.stt.transcriber as transcriber_mod

    def boom(*a, **kw):
        raise AssertionError("Transcriber must not call probe_gpu() when a gpu_profile was given")

    monkeypatch.setattr(transcriber_mod, "probe_gpu", boom)


def test_injected_gpu_profile_is_used_and_probe_gpu_is_never_called(cuda_profile, no_probe):
    t = Transcriber(gpu_profile=cuda_profile)
    assert t.profile == cuda_profile


def test_no_gpu_profile_given_falls_back_to_probe_gpu(monkeypatch, cuda_profile):
    import nova.stt.transcriber as transcriber_mod

    calls = []

    def fake_probe(*a, **kw):
        calls.append((a, kw))
        return cuda_profile

    monkeypatch.setattr(transcriber_mod, "probe_gpu", fake_probe)
    t = Transcriber()

    assert t.profile == cuda_profile
    assert len(calls) == 1


def test_load_constructs_the_model_with_the_profiles_device_and_compute_type(
    cuda_profile, no_probe, fake_whisper_model
):
    t = Transcriber(gpu_profile=cuda_profile)
    t.load()

    assert len(fake_whisper_model.instances) == 1
    m = fake_whisper_model.instances[0]
    assert m.device == "cuda"
    assert m.compute_type == "float16"


def test_load_never_fetches_over_the_network(cuda_profile, no_probe, fake_whisper_model):
    """Privacy constraint: STT must resolve strictly from the local HF
    cache, never hit the network at inference time."""
    t = Transcriber(gpu_profile=cuda_profile)
    t.load()

    m = fake_whisper_model.instances[0]
    assert m.local_files_only is True


def test_load_uses_the_local_hf_cache_download_root(cuda_profile, no_probe, fake_whisper_model):
    from nova.stt._paths import HF_HUB_CACHE_DIR

    t = Transcriber(gpu_profile=cuda_profile)
    t.load()

    m = fake_whisper_model.instances[0]
    assert m.download_root == str(HF_HUB_CACHE_DIR)


def test_load_is_idempotent(cuda_profile, no_probe, fake_whisper_model):
    t = Transcriber(gpu_profile=cuda_profile)
    t.load()
    t.load()
    assert len(fake_whisper_model.instances) == 1


def test_unload_then_load_creates_a_fresh_model(cuda_profile, no_probe, fake_whisper_model):
    t = Transcriber(gpu_profile=cuda_profile)
    t.load()
    t.unload()
    t.load()
    assert len(fake_whisper_model.instances) == 2


def test_unload_without_load_does_not_raise(cuda_profile, no_probe):
    t = Transcriber(gpu_profile=cuda_profile)
    t.unload()  # must not raise


def test_transcribe_auto_loads_when_not_yet_loaded(cuda_profile, no_probe, fake_whisper_model):
    t = Transcriber(gpu_profile=cuda_profile)
    text = t.transcribe(np.zeros(16000, dtype=np.float32))

    assert len(fake_whisper_model.instances) == 1
    assert isinstance(text, str)


def test_transcribe_joins_and_strips_segment_text(cuda_profile, no_probe, fake_whisper_model):
    t = Transcriber(gpu_profile=cuda_profile)
    text = t.transcribe(np.zeros(16000, dtype=np.float32))

    assert text == "hello world"  # joined verbatim (" hello " + "world "), then outer .strip()


def test_transcribe_passes_pcm_through_unchanged(cuda_profile, no_probe, fake_whisper_model):
    t = Transcriber(gpu_profile=cuda_profile)
    pcm = np.linspace(-1, 1, 16000, dtype=np.float32)

    t.transcribe(pcm)

    m = fake_whisper_model.instances[0]
    assert m.transcribe_calls[-1] is pcm


def test_transcribe_after_unload_reloads_automatically(cuda_profile, no_probe, fake_whisper_model):
    t = Transcriber(gpu_profile=cuda_profile)
    t.load()
    t.unload()
    t.transcribe(np.zeros(16000, dtype=np.float32))

    assert len(fake_whisper_model.instances) == 2


# ---------------------------------------------------------------------------
# Real hardware (opt-in): set NOVA_TEST_REAL_HARDWARE=1 to load the actual
# large-v3-turbo weights (already on disk) and run one real transcription.
# Off by default since it's a multi-second GPU model load, not a unit test.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_TEST_REAL_HARDWARE") != "1",
    reason="set NOVA_TEST_REAL_HARDWARE=1 to load the real large-v3-turbo model",
)
def test_real_transcriber_loads_and_transcribes_real_audio():
    t = Transcriber()  # real probe_gpu() + real model
    try:
        t.load()
        silence = np.zeros(16000, dtype=np.float32)
        text = t.transcribe(silence)
        assert isinstance(text, str)
    finally:
        t.unload()
