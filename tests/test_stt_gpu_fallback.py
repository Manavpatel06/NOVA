"""2026-10-14 regression: one start where nvidia-smi gave no answer demoted
speech-to-text to the CPU and overwrote the verified GPU profile."""
from __future__ import annotations

import json

import pytest

import nova.stt.gpu as gpu


@pytest.fixture
def cuda_cache(tmp_path, monkeypatch):
    path = tmp_path / "gpu_profile.json"
    path.write_text(json.dumps({
        "device": "cuda", "compute_type": "float16", "verified": True,
        "gpu_name": "NVIDIA GeForce RTX 5060 Laptop GPU", "driver": "610.62",
        "ct2_version": __import__("ctranslate2").__version__,
        "dll_directories": [], "verified_model": "tiny.en",
    }), encoding="utf-8")
    monkeypatch.setattr(gpu, "GPU_PROFILE_PATH", path)
    monkeypatch.setattr(gpu, "ensure_dll_directories", lambda: [])
    monkeypatch.setattr(gpu, "_gpu_identity", lambda: (None, None, None))  # nvidia-smi failed
    return path


def test_silent_nvidia_smi_keeps_the_verified_gpu_profile(cuda_cache, monkeypatch):
    import ctranslate2

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(gpu, "_probe_cpu", lambda *a: pytest.fail("must not fall back to CPU"))
    profile = gpu.probe_gpu()
    assert profile.device == "cuda" and profile.compute_type == "float16"


def test_gpu_switched_off_uses_cpu_this_run_without_forgetting_the_gpu(cuda_cache, monkeypatch):
    import ctranslate2

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    cpu = gpu.GpuProfile(device="cpu", compute_type="int8", verified=True, gpu_name="", driver="",
                         ct2_version="x", dll_directories=[], verified_model="tiny.en")
    monkeypatch.setattr(gpu, "_probe_cpu", lambda *a: cpu)
    assert gpu.probe_gpu().device == "cpu"
    assert json.loads(cuda_cache.read_text(encoding="utf-8"))["device"] == "cuda"  # cache untouched
