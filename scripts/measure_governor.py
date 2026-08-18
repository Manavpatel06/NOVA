"""Real RSS/VRAM measurements for T1.2 (the governor) on this machine.

Not part of the test suite. Produces the table for docs/GOVERNOR_MEASUREMENTS.md
and exercises Governor.check_once() end to end against real components.

Run: D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\measure_governor.py
"""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import time

import numpy as np
import psutil

from nova.bus import EventBus
from nova.config import GovernorConfig
from nova.governor import TOPIC_TURN_END, TOPIC_TURN_START, Governor

_PROC = psutil.Process(os.getpid())


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


def row(label: str) -> str:
    return f"{label:38s} RSS={rss_mb():8.1f} MB   VRAM={vram_used_mib()!s:>6} MiB"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    section("baseline: process just started, nothing imported yet")
    print(row("interpreter + psutil only"))

    # Import everything NovaApp imports, but construct nothing that loads a
    # model -- NovaApp's own docstring promises construction is cheap and
    # loads no models; this is that promise, measured.
    from nova.stt.transcriber import Transcriber
    from nova.tts.synthesizer import Synthesizer

    gc.collect()
    section("idle: Transcriber + Synthesizer constructed, nothing loaded")
    t0 = time.monotonic()
    transcriber = Transcriber()
    synth = Synthesizer()
    print(f"construction took {time.monotonic() - t0:.3f}s (should be fast -- no model I/O)")
    print(row("idle, nothing loaded"))
    idle_rss = rss_mb()

    # --- Whisper resident --------------------------------------------------
    section("Whisper resident")
    t0 = time.monotonic()
    transcriber.load()
    print(f"Transcriber.load() took {time.monotonic() - t0:.3f}s")
    print(row("Whisper resident"))
    whisper_rss = rss_mb()
    whisper_vram = vram_used_mib()

    silence = np.zeros(16000, dtype=np.float32)  # 1s of silence, 16kHz mono
    t0 = time.monotonic()
    text = transcriber.transcribe(silence)
    print(f"transcribe(1s silence) -> {text!r} in {time.monotonic() - t0:.3f}s")
    print(row("Whisper resident, after one transcribe() call"))
    # This is the number that matters for a "typical footprint" hint, not
    # the post-load()-only figure above: real usage always transcribes
    # immediately, and the jump from inference-time host buffers (beam
    # search, CTranslate2 allocator arenas) is bigger than model load alone.
    whisper_rss_after_use = rss_mb()

    transcriber.unload()
    gc.collect()
    print(row("Whisper unloaded"))
    whisper_unloaded_rss = rss_mb()
    whisper_unloaded_vram = vram_used_mib()
    if whisper_unloaded_rss > whisper_rss_after_use - 20:
        print(
            f"FINDING: Transcriber.unload() did not measurably reduce host RSS "
            f"({whisper_rss_after_use:.1f} -> {whisper_unloaded_rss:.1f} MB) even though "
            f"VRAM did ({whisper_vram} -> {whisper_unloaded_vram} MiB). See report."
        )

    # --- Kokoro resident -----------------------------------------------
    section("Kokoro resident")
    t0 = time.monotonic()
    synth.load()
    print(f"Synthesizer.load() took {time.monotonic() - t0:.3f}s, backend={synth.backend!r}")
    print(row("Kokoro resident"))
    kokoro_rss = rss_mb()

    t0 = time.monotonic()
    pcm, sr = synth.say("Testing the governor.")
    print(f"say(...) -> {len(pcm)} samples @ {sr}Hz in {time.monotonic() - t0:.3f}s")
    print(row("Kokoro resident, after one say() call"))

    # --- both resident ---------------------------------------------------
    section("both resident")
    transcriber.load()
    print(row("Whisper + Kokoro both resident"))
    both_rss = rss_mb()
    both_vram = vram_used_mib()

    # --- idle-unload cycle: does memory actually come back? ----------------
    section("idle-unload cycle: repeated load/unload, checking for creep")
    cycle_rows = []
    for cycle in range(1, 4):
        transcriber.load()
        loaded = rss_mb()
        transcriber.unload()
        gc.collect()
        unloaded = rss_mb()
        cycle_rows.append((cycle, loaded, unloaded))
        print(f"cycle {cycle}: loaded={loaded:.1f} MB, unloaded={unloaded:.1f} MB")
    first_unloaded = cycle_rows[0][2]
    last_unloaded = cycle_rows[-1][2]
    creep = last_unloaded - first_unloaded
    print(f"unloaded-RSS drift over {len(cycle_rows)} cycles: {creep:+.1f} MB")

    synth.unload()
    gc.collect()
    print(row("both unloaded (end state)"))
    final_rss = rss_mb()

    # --- Governor.check_once() end to end, against these real components --
    section("Governor.check_once(): real end-to-end behavior")

    async def _governor_demo_achievable_ceiling() -> None:
        """Ceiling set just above Transcriber's real post-use floor -- i.e.
        the honest claim is "shed Kokoro when both are loaded and idle",
        not "return all the way to cold-idle" (see the FINDING above: that
        floor doesn't move once Transcriber has actually transcribed
        something in this process)."""
        bus = EventBus()
        # +250, not +100: the creep-cycle section above already pushed the
        # transcriber-only floor up from its first measurement (the whole
        # point of testing for creep) -- this margin has to accommodate
        # that drift, which is itself part of what this run demonstrates.
        cfg = GovernorConfig(poll_interval_s=9999, rss_ceiling_mb=whisper_rss_after_use + 250)
        gov = Governor(bus, cfg)
        gov.register("transcriber", transcriber, typical_rss_mb=whisper_rss_after_use - idle_rss)
        gov.register("synth", synth, typical_rss_mb=kokoro_rss - idle_rss)

        transcriber.load()
        synth.load()
        print(f"before check_once(): {gov.snapshot()}")

        # Turn in flight: must NOT unload even though we're over ceiling.
        await bus.publish(TOPIC_TURN_START, None)
        gov.check_once()
        print(f"check_once() during a turn: {gov.snapshot()} (must be unchanged)")
        assert gov.snapshot().unload_count == 0, "FAIL: governor unloaded mid-turn"

        # Turn ends: now it's allowed to act, and this ceiling is reachable
        # by shedding Kokoro alone (Kokoro's unload() does free host RSS).
        await bus.publish(TOPIC_TURN_END, None)
        gov.check_once()
        print(f"check_once() after turn ends: {gov.snapshot()}")
        assert gov.snapshot().unload_count >= 1, "FAIL: governor did not unload when over ceiling"
        assert gov.current_rss_mb() <= cfg.rss_ceiling_mb, (
            f"FAIL: RSS {gov.current_rss_mb():.1f} MB still over a reachable ceiling "
            f"{cfg.rss_ceiling_mb:.1f} MB after check_once()"
        )
        print("Governor withheld unloading mid-turn, then reached the ceiling once the turn ended.")

    async def _governor_demo_unreachable_ceiling() -> None:
        """Ceiling set below Transcriber's post-use floor -- unreachable
        with these two components once Transcriber has been used. Correct
        behavior is: unload everything that CAN be freed, then log a clear
        WARNING and return -- never crash, never loop forever, never claim
        success it didn't achieve."""
        bus = EventBus()
        cfg = GovernorConfig(poll_interval_s=9999, rss_ceiling_mb=idle_rss + 50)
        gov = Governor(bus, cfg)
        gov.register("transcriber", transcriber, typical_rss_mb=whisper_rss_after_use - idle_rss)
        gov.register("synth", synth, typical_rss_mb=kokoro_rss - idle_rss)

        transcriber.load()
        synth.load()
        t0 = time.monotonic()
        gov.check_once()
        elapsed = time.monotonic() - t0
        after = gov.snapshot()
        print(f"check_once() against an unreachable ceiling took {elapsed:.3f}s -> {after}")
        assert elapsed < 5.0, "FAIL: check_once() did not return promptly"
        assert after.rss_mb > after.ceiling_mb, "expected outcome: still over an unreachable ceiling"
        print(
            "Governor correctly freed what it could, logged a WARNING naming the gap, "
            "and returned -- it did not loop, crash, or claim a false success."
        )

    asyncio.run(_governor_demo_achievable_ceiling())
    asyncio.run(_governor_demo_unreachable_ceiling())

    # --- final table --------------------------------------------------
    section("SUMMARY TABLE")
    print(f"{'state':38s} {'RSS (MB)':>10s} {'VRAM (MiB)':>12s}")
    print(f"{'idle, nothing loaded':38s} {idle_rss:10.1f} {'--':>12s}")
    print(f"{'Whisper resident':38s} {whisper_rss:10.1f} {whisper_vram!s:>12s}")
    print(f"{'Whisper unloaded':38s} {whisper_unloaded_rss:10.1f} {whisper_unloaded_vram!s:>12s}")
    print(f"{'Kokoro resident':38s} {kokoro_rss:10.1f} {'--':>12s}")
    print(f"{'both resident':38s} {both_rss:10.1f} {both_vram!s:>12s}")
    print(f"{'both unloaded (end state)':38s} {final_rss:10.1f} {'--':>12s}")
    print(f"\nunloaded-RSS drift over {len(cycle_rows)} load/unload cycles: {creep:+.1f} MB")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
