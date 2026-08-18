"""F16 diagnosis: where does the ~740-900 MB host RSS / ~300 MiB VRAM that
survives unload() actually live?

Not part of the test suite. This is investigation, not a fix -- per the
lead's explicit instruction, it produces evidence, not a patch.

Each "worker" mode runs in its OWN fresh subprocess (via `sys.executable -c
...`-style re-invocation of this same file) so every measurement starts from
a real, clean, single-purpose process -- no cross-contamination from
whichever component happened to import first.

Run: D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\diagnose_memory_retention.py
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time
import tracemalloc

import psutil

_PROC = psutil.Process(os.getpid())
_PYTHON = sys.executable
_THIS_FILE = os.path.abspath(__file__)


def rss_mb() -> float:
    return _PROC.memory_info().rss / 1e6


def vram_used_mib() -> int | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    try:
        return int(lines[0].strip())
    except (IndexError, ValueError):
        return None


def _run_worker(mode: str) -> dict:
    """Spawn a fresh interpreter running this file with `mode`, capture its
    single JSON-line stdout result."""
    result = subprocess.run(
        [_PYTHON, _THIS_FILE, mode],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        print(f"--- worker {mode!r} STDERR ---")
        print(result.stderr[-4000:])
        raise SystemExit(f"worker {mode!r} failed (exit {result.returncode})")
    # Last non-empty line is the JSON result; earlier lines are this
    # worker's own logging (nova.log writes to stderr AND file, so stdout
    # should be clean, but be defensive).
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Workers -- each runs standalone in its own process, prints one JSON line.
# ---------------------------------------------------------------------------


def worker_whisper_once() -> None:
    import numpy as np
    tracemalloc.start()
    baseline_rss = rss_mb()
    baseline_vram = vram_used_mib()

    from nova.stt.transcriber import Transcriber
    t = Transcriber()
    t.load()
    loaded_rss = rss_mb()
    loaded_vram = vram_used_mib()

    silence = np.zeros(16000, dtype=np.float32)
    t.transcribe(silence)
    used_rss = rss_mb()
    used_vram = vram_used_mib()
    _, tm_peak_used = tracemalloc.get_traced_memory()

    t.unload()
    gc.collect()
    time.sleep(0.3)  # let the driver settle its VRAM accounting
    unloaded_rss = rss_mb()
    unloaded_vram = vram_used_mib()
    tm_current_unloaded, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(json.dumps({
        "baseline_rss_mb": baseline_rss, "baseline_vram_mib": baseline_vram,
        "loaded_rss_mb": loaded_rss, "loaded_vram_mib": loaded_vram,
        "used_rss_mb": used_rss, "used_vram_mib": used_vram,
        "unloaded_rss_mb": unloaded_rss, "unloaded_vram_mib": unloaded_vram,
        "tracemalloc_peak_mb": tm_peak_used / 1e6,
        "tracemalloc_after_unload_mb": tm_current_unloaded / 1e6,
    }))


def worker_kokoro_once() -> None:
    tracemalloc.start()
    baseline_rss = rss_mb()

    from nova.tts.synthesizer import Synthesizer
    s = Synthesizer()
    s.load()
    loaded_rss = rss_mb()

    s.say("Testing memory retention.")
    used_rss = rss_mb()
    _, tm_peak_used = tracemalloc.get_traced_memory()

    s.unload()
    gc.collect()
    unloaded_rss = rss_mb()
    tm_current_unloaded, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(json.dumps({
        "baseline_rss_mb": baseline_rss,
        "loaded_rss_mb": loaded_rss,
        "used_rss_mb": used_rss,
        "unloaded_rss_mb": unloaded_rss,
        "tracemalloc_peak_mb": tm_peak_used / 1e6,
        "tracemalloc_after_unload_mb": tm_current_unloaded / 1e6,
    }))


def worker_whisper_cycles() -> None:
    """N load/transcribe/unload cycles in ONE process -- floor (flat after
    cycle 1) vs leak (keeps climbing) can only be told apart with several
    cycles, not the 3 the governor task used."""
    import numpy as np
    from nova.stt.transcriber import Transcriber

    silence = np.zeros(16000, dtype=np.float32)
    t = Transcriber()
    cycles = []
    for i in range(10):
        t.load()
        t.transcribe(silence)
        t.unload()
        gc.collect()
        time.sleep(0.2)
        cycles.append({"cycle": i + 1, "rss_mb": rss_mb(), "vram_mib": vram_used_mib()})
    print(json.dumps({"cycles": cycles}))


def worker_whisper_multi_use_before_unload() -> None:
    """Load ONCE, transcribe 3x (no unload between -- matching 3 real turns
    in one session), THEN unload once. Compares against worker_whisper_once's
    single-transcribe floor to see if *repeated use*, not reload, grows the
    retained floor."""
    import numpy as np
    from nova.stt.transcriber import Transcriber

    baseline_rss = rss_mb()
    t = Transcriber()
    t.load()
    silence = np.zeros(16000, dtype=np.float32)
    per_use = []
    for i in range(3):
        t.transcribe(silence)
        per_use.append({"use": i + 1, "rss_mb": rss_mb(), "vram_mib": vram_used_mib()})
    t.unload()
    gc.collect()
    time.sleep(0.3)
    print(json.dumps({
        "baseline_rss_mb": baseline_rss,
        "per_use": per_use,
        "unloaded_rss_mb": rss_mb(),
        "unloaded_vram_mib": vram_used_mib(),
    }))


def worker_combined_one_turn() -> None:
    """Whisper transcribe -> Kokoro say -> unload both, in ONE process,
    mimicking a real turn's actual usage order. Tests whether the two
    components' retained floors are simply additive when they coexist in
    the same process (my other workers only ever test one at a time)."""
    import numpy as np
    baseline_rss = rss_mb()
    baseline_vram = vram_used_mib()

    from nova.stt.transcriber import Transcriber
    from nova.tts.synthesizer import Synthesizer

    t = Transcriber()
    t.transcribe(np.zeros(16000, dtype=np.float32))  # lazy-loads internally
    after_stt_rss = rss_mb()
    after_stt_vram = vram_used_mib()

    s = Synthesizer()
    s.say("Testing combined memory retention.")  # lazy-loads internally
    after_tts_rss = rss_mb()
    after_tts_vram = vram_used_mib()

    t.unload()
    s.unload()
    gc.collect()
    time.sleep(0.3)

    print(json.dumps({
        "baseline_rss_mb": baseline_rss, "baseline_vram_mib": baseline_vram,
        "after_stt_rss_mb": after_stt_rss, "after_stt_vram_mib": after_stt_vram,
        "after_tts_rss_mb": after_tts_rss, "after_tts_vram_mib": after_tts_vram,
        "unloaded_rss_mb": rss_mb(), "unloaded_vram_mib": vram_used_mib(),
    }))


def worker_probe_gpu_cold() -> None:
    """Does probe_gpu()'s own tiny.en verification step (run when there is
    no cached profile) leave residue that compounds with the real model's
    load later? Forces a fresh probe rather than using the cached
    data/gpu_profile.json."""
    baseline_rss = rss_mb()
    baseline_vram = vram_used_mib()

    from nova.stt.gpu import probe_gpu
    profile = probe_gpu(force=True)
    after_probe_rss = rss_mb()
    after_probe_vram = vram_used_mib()

    from nova.stt.transcriber import Transcriber
    import numpy as np
    t = Transcriber(gpu_profile=profile)
    t.load()
    t.transcribe(np.zeros(16000, dtype=np.float32))
    used_rss = rss_mb()
    used_vram = vram_used_mib()
    t.unload()
    gc.collect()
    time.sleep(0.3)

    print(json.dumps({
        "baseline_rss_mb": baseline_rss, "baseline_vram_mib": baseline_vram,
        "after_probe_rss_mb": after_probe_rss, "after_probe_vram_mib": after_probe_vram,
        "used_rss_mb": used_rss, "used_vram_mib": used_vram,
        "unloaded_rss_mb": rss_mb(), "unloaded_vram_mib": vram_used_mib(),
        "probe_verified_model": profile.verified_model if hasattr(profile, "verified_model") else None,
    }))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"onnxruntime providers (does Kokoro ever touch the GPU?):")
    import onnxruntime as ort
    providers = ort.get_available_providers()
    print(f"  {providers}")
    assert "CUDAExecutionProvider" not in providers, "unexpected: CUDA provider present"
    print("  -> CPU-only build installed. Kokoro/onnxruntime cannot be the VRAM source.\n")

    print("=== worker: whisper, one load/transcribe/unload, tracemalloc'd ===")
    w1 = _run_worker("whisper_once")
    print(json.dumps(w1, indent=2))
    real_rss_delta = w1["used_rss_mb"] - w1["baseline_rss_mb"]
    retained_rss_delta = w1["unloaded_rss_mb"] - w1["baseline_rss_mb"]
    print(f"real RSS growth while used: {real_rss_delta:.1f} MB")
    print(f"RSS retained after unload:  {retained_rss_delta:.1f} MB")
    print(f"tracemalloc (Python-tracked) peak: {w1['tracemalloc_peak_mb']:.1f} MB")
    print(f"tracemalloc (Python-tracked) after unload: {w1['tracemalloc_after_unload_mb']:.1f} MB")
    if w1["tracemalloc_after_unload_mb"] < retained_rss_delta * 0.3:
        print(
            "-> Python-tracked memory accounts for only a small fraction of the "
            "retained RSS: the retention is native/library-level (CTranslate2's "
            "own C++ allocations), not a Python object reference leak."
        )

    print("\n=== worker: kokoro, one load/say/unload, tracemalloc'd ===")
    w2 = _run_worker("kokoro_once")
    print(json.dumps(w2, indent=2))
    kokoro_retained = w2["unloaded_rss_mb"] - w2["baseline_rss_mb"]
    print(f"Kokoro RSS retained after unload: {kokoro_retained:.1f} MB")

    print("\n=== worker: whisper, 10 load/transcribe/unload cycles (leak vs floor) ===")
    w3 = _run_worker("whisper_cycles")
    cycles = w3["cycles"]
    for c in cycles:
        print(f"  cycle {c['cycle']:2d}: rss={c['rss_mb']:8.1f} MB  vram={c['vram_mib']} MiB")
    first, last = cycles[0]["rss_mb"], cycles[-1]["rss_mb"]
    mid = cycles[len(cycles) // 2]["rss_mb"]
    print(f"cycle 1 -> mid -> last RSS: {first:.1f} -> {mid:.1f} -> {last:.1f} MB")
    print(f"drift cycle1->mid: {mid - first:+.1f} MB, mid->last: {last - mid:+.1f} MB")

    print("\n=== worker: whisper, load once, transcribe 3x, unload once (matches a 3-turn session) ===")
    w4 = _run_worker("whisper_multi_use_before_unload")
    print(json.dumps(w4, indent=2))
    for u in w4["per_use"]:
        print(f"  after use {u['use']}: rss={u['rss_mb']:.1f} MB vram={u['vram_mib']} MiB")
    print(f"single-transcribe floor (worker 1): {w1['unloaded_rss_mb']:.1f} MB")
    print(f"three-transcribes-before-unload floor (worker 4): {w4['unloaded_rss_mb']:.1f} MB")

    print("\n=== worker: cold probe_gpu() (tiny.en verification) then real load ===")
    w5 = _run_worker("probe_gpu_cold")
    print(json.dumps(w5, indent=2))

    print("\n=== worker: combined, one process, STT then TTS then unload both (real turn order) ===")
    w6 = _run_worker("combined_one_turn")
    print(json.dumps(w6, indent=2))
    additive_prediction = (w1["unloaded_rss_mb"] - w1["baseline_rss_mb"]) + kokoro_retained
    actual_combined = w6["unloaded_rss_mb"] - w6["baseline_rss_mb"]
    print(f"predicted combined retained RSS (Whisper floor + Kokoro floor): {additive_prediction:.1f} MB")
    print(f"actual combined retained RSS: {actual_combined:.1f} MB")
    print(f"combined unloaded VRAM: {w6['unloaded_vram_mib']} MiB (vs Whisper-alone: {w1['unloaded_vram_mib']} MiB)")

    print("\nDIAGNOSIS COMPLETE")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        {
            "whisper_once": worker_whisper_once,
            "kokoro_once": worker_kokoro_once,
            "whisper_cycles": worker_whisper_cycles,
            "whisper_multi_use_before_unload": worker_whisper_multi_use_before_unload,
            "combined_one_turn": worker_combined_one_turn,
            "probe_gpu_cold": worker_probe_gpu_cold,
        }[mode]()
    else:
        main()
