"""Contract: docs/CONTRACTS.md T0.3 — nova.stt.gpu.probe_gpu.

    def probe_gpu(force: bool = False) -> GpuProfile

Two settled hazards under test (do not re-derive, per CONTRACTS.md):
  1. ctranslate2.get_supported_compute_types() advertises int8 support on
     this Blackwell card and then dies with CUBLAS_STATUS_NOT_SUPPORTED on
     first inference. int8 / int8_bfloat16 must be refused outright on
     compute capability >= 12, regardless of what that function claims.
  2. An unsupported-kernel error must advance to the next candidate; an OOM
     error must retry smaller. verified=True only after a real inference
     completed. Cache keyed by (gpu name, driver, ct2 version).

White-box against nova/stt/gpu.py as landed: it never even calls
ctranslate2.get_supported_compute_types() (candidates are a hardcoded,
reviewed-per-hardware list instead — arguably a *stronger* implementation
of "trust nothing" than dynamically trusting-then-filtering that function's
claims). So instead of mocking that function, the "never selects a refused
compute type on this card" test both (a) forces the refusal decision path
via a controlled fake GPU identity, and (b) independently verifies against
real hardware that this card really is compute capability >= 12 using
nvidia-smi as an out-of-band oracle — never trusting probe_gpu's own
detection to grade itself.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

import nova.stt.gpu as gpu_mod
from nova.stt.gpu import GpuProfile, probe_gpu


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    path = tmp_path / "gpu_profile.json"
    monkeypatch.setattr(gpu_mod, "GPU_PROFILE_PATH", path)
    monkeypatch.setattr(gpu_mod, "DATA_DIR", tmp_path)
    return path


@pytest.fixture
def fake_transcribe_calls(monkeypatch):
    """Replaces gpu_mod._try_transcribe with a fully controllable fake.
    Returns a list the test appends (compute_type -> outcome) rules to, and
    the fake records every (device, compute_type) call it received."""
    calls: list[tuple[str, str]] = []
    outcomes: dict[tuple[str, str], list] = {}

    def fake(device, compute_type):
        calls.append((device, compute_type))
        key = (device, compute_type)
        queue = outcomes.get(key)
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return None  # default: succeeds

    monkeypatch.setattr(gpu_mod, "_try_transcribe", fake)

    class Controller:
        def fail_once(self, device, compute_type, exc):
            outcomes.setdefault((device, compute_type), []).append(exc)

        def fail_always(self, device, compute_type, exc):
            # a big queue is enough for these tests' bounded retry counts
            outcomes.setdefault((device, compute_type), []).extend([exc] * 50)

        @property
        def calls(self):
            return calls

    return Controller()


def _fake_identity(monkeypatch, gpu_name, driver, compute_capability):
    monkeypatch.setattr(gpu_mod, "_gpu_identity", lambda: (gpu_name, driver, compute_capability))


def _skip_if_no_cuda_visible_to_ctranslate2():
    import ctranslate2

    if ctranslate2.get_cuda_device_count() == 0:
        pytest.skip("no CUDA device visible to ctranslate2 on this machine")


# ---------------------------------------------------------------------------
# The crux: never select int8 / int8_bfloat16 on compute capability >= 12
# ---------------------------------------------------------------------------


def test_refused_set_matches_the_contract():
    """Pins the exact contract values as a regression tripwire."""
    assert gpu_mod._REFUSED_ON_CC12 == {"int8", "int8_bfloat16"}
    assert gpu_mod._REFUSED_MIN_COMPUTE_CAPABILITY == 12.0


def test_never_selects_a_refused_compute_type_when_cc_is_12(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Fake Blackwell GPU", "999.99", 12.0)

    profile = probe_gpu(force=True)

    assert profile.device == "cuda"
    assert profile.verified is True
    assert profile.compute_type not in gpu_mod._REFUSED_ON_CC12
    assert all(ct not in gpu_mod._REFUSED_ON_CC12 for _dev, ct in fake_transcribe_calls.calls)


def test_never_selects_a_refused_compute_type_above_12(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    """The rule is '>= 12', not '== 12' — a hypothetical future card at 12.x
    or higher must still be refused."""
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Fake Future GPU", "1.0", 13.0)

    profile = probe_gpu(force=True)

    assert profile.compute_type not in gpu_mod._REFUSED_ON_CC12


def test_compute_capability_below_12_does_not_trigger_the_refusal(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    """Sanity check on the boundary from the other side: a pre-Blackwell
    card (cc < 12) must not have candidates spuriously filtered."""
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Fake Ampere GPU", "1.0", 8.6)

    profile = probe_gpu(force=True)

    # float16 is always first and always succeeds in this fixture's default.
    assert profile.compute_type == "float16"


# ---------------------------------------------------------------------------
# Candidate fallback behavior on failure
# ---------------------------------------------------------------------------


def test_unsupported_kernel_error_advances_to_the_next_candidate(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Fake GPU", "1.0", 8.0)  # cc<12: nothing pre-filtered
    fake_transcribe_calls.fail_once(
        "cuda", "float16", RuntimeError("CUBLAS_STATUS_NOT_SUPPORTED: no kernel image is available")
    )

    profile = probe_gpu(force=True)

    assert fake_transcribe_calls.calls == [("cuda", "float16"), ("cuda", "int8_float16")]
    assert profile.compute_type == "int8_float16"
    assert profile.verified is True


def test_oom_error_retries_the_same_candidate_before_advancing(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    """docs/CONTRACTS.md: 'an unsupported-kernel error advances to the next
    candidate while an OOM error retries smaller' — i.e. OOM must behave
    differently from unsupported-kernel, not fall through to the next dtype
    identically.

    EXPECTED RED against nova/stt/gpu.py as landed: reading the source,
    probe_gpu()'s candidate loop calls _classify_failure() purely to choose
    a log message ('trying next (lower-memory) candidate' for oom, 'trying
    next candidate' for unsupported_kernel) and then unconditionally
    `continue`s to the next dtype in both cases — there is no retry-same-
    candidate path for OOM at all. This is either a real gap (T0.3 should
    implement an OOM retry) or the contract's 'retries smaller' should be
    clarified to mean 'eventually falls through to the CPU path', in which
    case this specific test's expectation is wrong and should be relaxed —
    flagged to the lead either way; see report.
    """
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Fake GPU", "1.0", 8.0)
    fake_transcribe_calls.fail_once(
        "cuda", "float16", RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
    )

    profile = probe_gpu(force=True)

    assert fake_transcribe_calls.calls[0] == ("cuda", "float16")
    assert fake_transcribe_calls.calls[1] == ("cuda", "float16"), (
        "an OOM on the first candidate should be retried on the SAME "
        f"candidate, not advance immediately; actual call sequence was "
        f"{fake_transcribe_calls.calls}"
    )
    assert profile.compute_type == "float16"
    assert profile.verified is True


def test_all_candidates_failing_falls_back_to_cpu_without_lying_about_verified(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Fake GPU", "1.0", 8.0)
    fake_transcribe_calls.fail_always("cuda", "float16", RuntimeError("out of memory"))
    fake_transcribe_calls.fail_always("cuda", "int8_float16", RuntimeError("out of memory"))
    fake_transcribe_calls.fail_always("cpu", "int8", RuntimeError("boom"))
    fake_transcribe_calls.fail_always("cpu", "float32", RuntimeError("boom"))

    profile = probe_gpu(force=True)

    assert profile.device == "cpu"
    assert profile.verified is False  # must never lie


def test_verified_is_true_only_after_a_real_transcription_completed(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Fake GPU", "1.0", 8.0)

    profile = probe_gpu(force=True)

    assert len(fake_transcribe_calls.calls) >= 1
    assert profile.verified is True


def test_no_gpu_visible_falls_back_to_cpu(isolated_cache, fake_transcribe_calls, monkeypatch):
    _fake_identity(monkeypatch, None, None, None)

    profile = probe_gpu(force=True)

    assert profile.device == "cpu"
    assert profile.gpu_name == ""
    assert profile.driver == ""
    assert ("cuda", "float16") not in fake_transcribe_calls.calls


# ---------------------------------------------------------------------------
# Cache: keyed by (gpu name, driver, ct2 version)
# ---------------------------------------------------------------------------


def test_cache_hit_skips_reprobe_when_nothing_changed(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Stable GPU", "1.0", 8.0)

    probe_gpu(force=False)
    calls_after_first = len(fake_transcribe_calls.calls)
    assert calls_after_first >= 1

    probe_gpu(force=False)
    assert len(fake_transcribe_calls.calls) == calls_after_first


def test_cache_invalidates_when_driver_changes(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Stable GPU", "111.11", 8.0)
    first = probe_gpu(force=False)
    assert first.driver == "111.11"

    _fake_identity(monkeypatch, "Stable GPU", "222.22", 8.0)
    second = probe_gpu(force=False)

    assert second.driver == "222.22"  # fresh, not the stale cached "111.11"


def test_cache_invalidates_when_gpu_name_changes(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "GPU One", "1.0", 8.0)
    first = probe_gpu(force=False)
    assert first.gpu_name == "GPU One"

    _fake_identity(monkeypatch, "GPU Two (swapped card)", "1.0", 8.0)
    second = probe_gpu(force=False)

    assert second.gpu_name == "GPU Two (swapped card)"


def test_cache_invalidates_when_ct2_version_changes(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    import ctranslate2

    _fake_identity(monkeypatch, "Stable GPU", "1.0", 8.0)
    first = probe_gpu(force=False)
    assert first.ct2_version == ctranslate2.__version__

    monkeypatch.setattr(ctranslate2, "__version__", "999.999.999")
    second = probe_gpu(force=False)

    assert second.ct2_version == "999.999.999"


def test_cache_round_trips_through_disk(isolated_cache, fake_transcribe_calls, monkeypatch):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Stable GPU", "1.0", 8.0)

    original = probe_gpu(force=False)
    assert isolated_cache.exists()

    from_disk = GpuProfile(**json.loads(isolated_cache.read_text(encoding="utf-8")))
    assert from_disk == original


# ---------------------------------------------------------------------------
# DLL directories
# ---------------------------------------------------------------------------


def test_dll_directories_reported_are_real_existing_nvidia_bin_dirs(
    isolated_cache, fake_transcribe_calls, monkeypatch
):
    _skip_if_no_cuda_visible_to_ctranslate2()
    _fake_identity(monkeypatch, "Stable GPU", "1.0", 8.0)

    profile = probe_gpu(force=True)

    assert profile.dll_directories, "expected at least one nvidia DLL directory to be recorded"
    for d in profile.dll_directories:
        from pathlib import Path

        assert Path(d).is_dir()
    assert any("nvidia" in d.lower() and d.lower().endswith("bin") for d in profile.dll_directories)


# ---------------------------------------------------------------------------
# One source of truth: nova/stt/_paths.py duplicates DATA_DIR / HF cache dir
# rather than importing them from nova/__init__.py. CLAUDE.md: "a test and a
# config file describing the same promise differently is a promise nobody
# can act on" — pin that the two independently-computed values never drift,
# even though the constitution's preferred fix is a single import.
# ---------------------------------------------------------------------------


def test_stt_paths_agree_with_the_nova_package_path_registry():
    import nova
    import nova.stt._paths as stt_paths

    assert stt_paths.DATA_DIR == nova.DATA_DIR
    assert stt_paths.HF_HUB_CACHE_DIR == nova.HF_HUB_DIR


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_gpu_profile_is_a_frozen_dataclass():
    import dataclasses

    assert dataclasses.is_dataclass(GpuProfile)
    p = GpuProfile(device="cuda", compute_type="float16", verified=True, gpu_name="x",
                    driver="1.0", ct2_version="4.0", dll_directories=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.verified = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Real hardware: independently confirms (via nvidia-smi, not probe_gpu's own
# detection) that this machine's card is compute capability >= 12, then
# proves the real, unmocked probe_gpu() genuinely never selects int8 /
# int8_bfloat16 on it. This is the actual hazard from CONTRACTS.md
# reproduced for real, not simulated.
# ---------------------------------------------------------------------------


def test_real_hardware_never_selects_a_refused_compute_type(isolated_cache):
    _skip_if_no_cuda_visible_to_ctranslate2()

    oracle = subprocess.run(
        ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=10,
    )
    if oracle.returncode != 0 or not oracle.stdout.strip():
        pytest.skip("nvidia-smi unavailable to independently confirm compute capability")
    compute_capability = float(oracle.stdout.strip().splitlines()[0])
    if compute_capability < 12.0:
        pytest.skip(
            f"this GPU is compute capability {compute_capability} (<12.0); "
            "the refusal rule under test doesn't apply to it"
        )

    profile = probe_gpu(force=True)

    assert profile.verified is True
    assert profile.compute_type not in {"int8", "int8_bfloat16"}
