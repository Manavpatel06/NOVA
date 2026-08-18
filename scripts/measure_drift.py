"""F16 follow-up: does the retained-memory floor grow over many cycles
(linear = blocker for always-on) or plateau (document and move on)? And is
growth, if any, tied to load/unload *cycles* or to *transcription count*?

Not part of the test suite. Three conditions, each in its own fresh
subprocess, same infra pattern as scripts/diagnose_memory_retention.py:

  A. 20 cycles of (load, transcribe once, unload) -- 20 reloads, 20 uses.
  B. 20 cycles of (load, transcribe 5x, unload) -- 20 reloads, 100 uses.
     Same cycle count as A, 5x the inference volume. If A and B end at
     similar floors, the reload act (not inference volume) is what matters.
  C. Load ONCE, transcribe 40x, never reload until the very end -- 1 reload,
     40 uses, zero cycling. If RSS grows here with no reloads at all,
     inference volume alone drives it, independent of load/unload.

Run: D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\measure_drift.py
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time

import psutil

_PROC = psutil.Process(os.getpid())
_PYTHON = sys.executable
_THIS_FILE = os.path.abspath(__file__)
_SILENCE_16K = None  # set lazily inside each worker (numpy import is worker-local)


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


def _run_worker(mode: str, timeout: float) -> dict:
    result = subprocess.run([_PYTHON, _THIS_FILE, mode], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"--- worker {mode!r} STDERR (last 4000 chars) ---")
        print(result.stderr[-4000:])
        raise SystemExit(f"worker {mode!r} failed (exit {result.returncode})")
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def worker_cycles_1x() -> None:
    import numpy as np
    from nova.stt.transcriber import Transcriber

    silence = np.zeros(16000, dtype=np.float32)
    t = Transcriber()
    points = []
    for i in range(20):
        t.load()
        t.transcribe(silence)
        t.unload()
        gc.collect()
        time.sleep(0.15)
        points.append({"cycle": i + 1, "rss_mb": rss_mb(), "vram_mib": vram_used_mib()})
    print(json.dumps({"condition": "A_20cycles_1transcribe", "points": points}))


def worker_cycles_5x() -> None:
    import numpy as np
    from nova.stt.transcriber import Transcriber

    silence = np.zeros(16000, dtype=np.float32)
    t = Transcriber()
    points = []
    for i in range(20):
        t.load()
        for _ in range(5):
            t.transcribe(silence)
        t.unload()
        gc.collect()
        time.sleep(0.15)
        points.append({"cycle": i + 1, "rss_mb": rss_mb(), "vram_mib": vram_used_mib()})
    print(json.dumps({"condition": "B_20cycles_5transcribes", "points": points}))


def worker_no_reload_40x() -> None:
    import numpy as np
    from nova.stt.transcriber import Transcriber

    silence = np.zeros(16000, dtype=np.float32)
    t = Transcriber()
    t.load()
    points = []
    for i in range(40):
        t.transcribe(silence)
        if (i + 1) % 2 == 0:  # every 2nd, to match ~20 data points
            points.append({"use": i + 1, "rss_mb": rss_mb(), "vram_mib": vram_used_mib()})
    t.unload()
    gc.collect()
    time.sleep(0.15)
    points.append({"use": "unloaded", "rss_mb": rss_mb(), "vram_mib": vram_used_mib()})
    print(json.dumps({"condition": "C_1load_40transcribes_noreload", "points": points}))


# ---------------------------------------------------------------------------
# Orchestrator / analysis
# ---------------------------------------------------------------------------


def _classify_shape(values: list[float]) -> str:
    """Crude, honest classification: compare the average slope of the first
    half of the run to the second half. Not a statistical model -- a
    reportable, inspectable heuristic, same spirit as the router's scoring."""
    n = len(values)
    if n < 4:
        return "insufficient data"
    half = n // 2
    first_half_delta = values[half - 1] - values[0]
    second_half_delta = values[-1] - values[half]
    total_delta = values[-1] - values[0]
    if abs(total_delta) < 15:  # MB -- within run-to-run noise band observed in F16
        return f"flat/plateau (total change {total_delta:+.1f} MB over the run -- within noise)"
    if second_half_delta > first_half_delta * 1.5 and second_half_delta > 15:
        return f"still climbing in the second half ({first_half_delta:+.1f} -> {second_half_delta:+.1f} MB) -- NOT yet plateaued, treat as a possible leak"
    if second_half_delta < first_half_delta * 0.5:
        return f"front-loaded then flattening (first half {first_half_delta:+.1f} MB, second half {second_half_delta:+.1f} MB) -- consistent with a floor, not a leak"
    return f"roughly linear across the whole run ({total_delta:+.1f} MB total, {first_half_delta:+.1f} then {second_half_delta:+.1f}) -- treat as a leak until proven otherwise"


def main() -> None:
    print("=== Condition A: 20 cycles, 1 transcribe/cycle (20 reloads, 20 uses) ===")
    a = _run_worker("cycles_1x", timeout=180)
    a_rss = [p["rss_mb"] for p in a["points"]]
    a_vram = [p["vram_mib"] for p in a["points"]]
    for p in a["points"]:
        print(f"  cycle {p['cycle']:2d}: rss={p['rss_mb']:8.1f} MB  vram={p['vram_mib']} MiB")
    print(f"A RSS shape:  {_classify_shape(a_rss)}")
    print(f"A VRAM shape: {_classify_shape([float(v) for v in a_vram])}")

    print("\n=== Condition B: 20 cycles, 5 transcribes/cycle (20 reloads, 100 uses) ===")
    b = _run_worker("cycles_5x", timeout=300)
    b_rss = [p["rss_mb"] for p in b["points"]]
    b_vram = [p["vram_mib"] for p in b["points"]]
    for p in b["points"]:
        print(f"  cycle {p['cycle']:2d}: rss={p['rss_mb']:8.1f} MB  vram={p['vram_mib']} MiB")
    print(f"B RSS shape:  {_classify_shape(b_rss)}")
    print(f"B VRAM shape: {_classify_shape([float(v) for v in b_vram])}")

    print("\n=== Condition C: 1 load, 40 transcribes, 0 reloads until the final unload ===")
    c = _run_worker("no_reload_40x", timeout=180)
    for p in c["points"]:
        label = p["use"]
        print(f"  use {label!s:>10}: rss={p['rss_mb']:8.1f} MB  vram={p['vram_mib']} MiB")
    c_numeric = [p for p in c["points"] if isinstance(p["use"], int)]
    c_rss = [p["rss_mb"] for p in c_numeric]
    print(f"C RSS shape (across 40 transcriptions, never reloaded): {_classify_shape(c_rss)}")

    print("\n=== Comparison: does more inference per cycle raise the final floor? ===")
    a_final, b_final = a_rss[-1], b_rss[-1]
    print(f"A final (20 reloads x 1 use = 20 total uses):  {a_final:.1f} MB")
    print(f"B final (20 reloads x 5 uses = 100 total uses): {b_final:.1f} MB")
    print(f"difference: {b_final - a_final:+.1f} MB despite B doing 5x the inference volume")
    if abs(b_final - a_final) < 30:
        print(
            "-> A and B end at essentially the same floor despite a 5x difference in "
            "inference volume. This points at the RELOAD act itself (constructing a "
            "fresh WhisperModel / CUDA context touch) as what the allocator's "
            "high-water mark tracks -- not cumulative transcription count."
        )
    else:
        print(
            "-> B's floor is meaningfully higher than A's despite the same cycle count. "
            "This points at per-transcription accumulation, not just the reload act."
        )

    c_grew = c_rss[-1] - c_rss[0]
    print(f"\nCondition C (zero reloads, 40 transcriptions): RSS moved {c_grew:+.1f} MB "
          f"from first to last measurement with NO load/unload cycling at all.")
    if abs(c_grew) < 15:
        print("-> Confirms: repeated inference alone, without reloading, does not grow RSS.")
    else:
        print("-> Repeated inference alone measurably moves RSS even without reloading -- worth more cycles.")

    print("\nDIAGNOSIS COMPLETE")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        {
            "cycles_1x": worker_cycles_1x,
            "cycles_5x": worker_cycles_5x,
            "no_reload_40x": worker_no_reload_40x,
        }[mode]()
    else:
        main()
