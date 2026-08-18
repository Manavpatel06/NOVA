# F16 — where the retained memory after unload() actually lives

Owner: nova-dev-core. Diagnosis only, no fix and no workaround, per the
lead's explicit instruction. Every number below came from
`scripts/diagnose_memory_retention.py` on this machine:

```
D:\NOVA\.venv\Scripts\python.exe scripts\diagnose_memory_retention.py
```

Each measurement below ran in its own fresh subprocess (the script
re-invokes itself with `sys.executable`) so nothing is cross-contaminated by
whichever component happened to import first in a shared process — except
where the point of the test was specifically to combine components, which is
called out explicitly.

## The five questions, answered

### 1. Where is it — Python references, or native library memory?

**Native library memory, not a Python reference leak.** `tracemalloc`
(Python's own allocator tracker) shows 23-25 MB tracked after Whisper is
unloaded, while real process RSS sits ~737 MB above baseline at the same
moment. Python-tracked memory accounts for **~3% of the retained RSS** — the
rest is invisible to `tracemalloc` because it was never allocated through
Python's allocator in the first place. `del` and `gc.collect()` cannot
release memory neither of them can see.

The installed `ctranslate2.dll` (4.8.1) literally contains the strings
`CT2_CUDA_ALLOCATOR` and `CT2_CUDA_CACHING_ALLOCATOR_CONFIG` (confirmed by
grepping the binary directly, not from documentation) — CTranslate2 ships
with a **caching allocator by design**: memory it frees internally is kept
in a pool sized to the largest allocation it has seen, for reuse on the next
call, rather than returned to the OS/driver. This is a real, intentional
performance optimization in the library (avoiding `cudaMalloc`/`cudaFree`
call overhead on every inference), not a bug — but it means Python-level
`unload()` cannot see or clear it, because it lives inside CTranslate2's own
C++ allocator state, one layer below anything Python's `gc` walks.

### 2. Is Kokoro/onnxruntime part of the VRAM story at all?

**No.** `onnxruntime.get_available_providers()` on this machine returns
`['AzureExecutionProvider', 'CPUExecutionProvider']` — **no CUDA execution
provider is installed.** Confirmed further: importing `onnxruntime` loads
zero CUDA-related DLLs (checked via `psutil`'s `memory_maps()` diff before/
after import). Kokoro runs entirely on CPU. Every MiB of VRAM measured
anywhere in this investigation comes from CTranslate2/Whisper alone.

Kokoro does have its own, much smaller, host-RSS floor: isolated test shows
**110.9 MB retained** after `unload()` (baseline 22.6 MB → loaded 509 MB →
used 558.5 MB → unloaded 133.6 MB) — and notably, Kokoro's `unload()`
**does** release the large majority of what it used (~76%, 558.5 → 133.6 MB),
unlike Whisper's near-total retention. Whatever Kokoro's floor is, it is not
the same mechanism as Whisper's.

### 3. Retained-but-reusable floor, or a per-cycle leak?

**Floor, not a leak — confirmed over 10 cycles, not the 3 the governor task
used.** One process, 10 sequential load→transcribe→unload cycles:

| cycle | RSS (MB) | VRAM (MiB) |
|---:|---:|---:|
| 1 | 767.4 | 367 |
| 2 | 709.8 | 375 |
| 3 | 710.2 | 383 |
| 4 | 710.3 | 482 *(likely contaminated — see caveat below)* |
| 5 | 731.9 | 399 |
| 6 | 731.8 | 407 |
| 7 | 711.4 | 415 |
| 8 | 712.0 | 423 |
| 9 | 712.1 | 431 |
| 10 | 712.6 | 439 |

**Host RSS: flat, not growing** — cycle 1→mid→last was 767.4 → 731.8 →
712.6 MB, i.e. a slight *decrease* from the first cycle, then stable within
a ~20 MB band for the remaining 9. No linear climb. This is a floor
established on first use, not a per-cycle leak.

**VRAM: a slower, real drift that has not obviously plateaued** — 367 → 439
MiB over 10 cycles, roughly +7-8 MiB/cycle on average (cycle 4's 482 MiB
looks like an outlier, not the trend). Small in absolute terms, but 10
cycles is not enough to rule out a slow climb over a full day of many turns.
**Recommend extending this specific check to 30-50 cycles before declaring
it safe** — the host-RSS side is resolved, the VRAM-creep side isn't fully
closed.

A second test — one load, three `transcribe()` calls, one unload (matching
"three real turns, one session," not three separate reload cycles) — ended
at **734.5 MB**, actually *below* the single-transcribe floor measured in
the same run series (759-771 MB range). This argues against "more uses in
one session raises the floor further": the floor looks like it's set by the
*first* real inference, not accumulated per call. Run-to-run variance
(~735-770 MB across separate process launches) is larger than any
within-session-reuse effect I could detect.

### 4. Does this explain the isolated-vs-in-app gap nova-dev-stt and the lead each saw?

**Host RSS: yes, cleanly — the two components' floors are additive.**
One process, real turn order (Whisper `transcribe()` → Kokoro `say()` →
unload both):

| stage | RSS (MB) | VRAM (MiB) |
|---|---:|---:|
| baseline | 34.9 | 264 |
| after STT (Whisper transcribe) | 768.7 | 2479 |
| after TTS (Kokoro say) | 1187.4 | 2479 |
| after unloading both | 897.4 | 367 |

Predicted combined retained RSS (Whisper's own floor + Kokoro's own floor,
each measured in isolation earlier): **867.1 MB**. Actual: **862.5 MB** —
within 5 MB, a ~99.5% match. This closes the gap between my small isolated
tests and the full-app numbers: the lead's 912.9 MB and nova-dev-stt's
919 MB are consistent with "Whisper's floor + Kokoro's floor + the fuller
app's larger import surface" (`nova.app` imports skills, security, router,
ladder, audit, hotkey — none of which this minimal two-component script
does), not a separate, unexplained mechanism.

**VRAM: not reproduced, still open.** The same combined test's unloaded
VRAM was 367 MiB — essentially identical to Whisper-alone's floor, not the
566 MiB the lead measured running the real turn loop. I could not reproduce
that gap with a minimal two-component script. Ruled out: Kokoro (confirmed
CPU-only, contributes 0 VRAM) and `probe_gpu()`'s own `tiny.en` verification
step (a forced-cold probe added only ~8 MiB to the eventual floor: 367 → 375
MiB). Remaining candidates, unconfirmed: more governor-triggered reload
cycles across a longer real session (the 10-cycle test above shows VRAM does
creep with repeated cycles), or `nvidia-smi` total-GPU-memory measurement
noise from a busier, longer-running real process. On that last point: during
this investigation I personally observed one clear instance of exactly this
kind of contamination — a transient reading of 4556-4662 MiB in the middle
of an otherwise-normal ~2479 MiB test run, with nothing in my own process
that could explain a 2000+ MiB jump. `nvidia-smi --query-compute-apps=pid,
used_memory` returns `[N/A]` for every process on this machine's driver
(WDDM mode doesn't expose reliable per-process VRAM accounting), so every
VRAM number in this investigation — mine and the earlier governor
measurements alike — is a **total-GPU delta from a freshly-measured
baseline, not a per-process figure**, and is only as clean as "nothing else
on the GPU changed between the two readings." That's a real methodology
limitation, not a finding about NOVA's code, but it means I'm not
confident the 566 MiB figure isn't partly measurement noise from a longer,
busier session. Flagging as genuinely open rather than closing it with a
guess.

### 5. What's the real achievable idle?

**Once `Transcriber.transcribe()` has run at least once in a process, that
process's host-RSS floor is ~735-770 MB and nothing `Transcriber.unload()`
does today can bring it lower** — the retained memory lives inside
CTranslate2's native caching allocator, a layer Python's garbage collector
cannot see or reach. Add Kokoro's own ~111-135 MB floor if TTS has also run
at least once, and the combined, measured, additive floor is **~850-900 MB**
— consistent with both the lead's 912.9 MB and nova-dev-stt's 919 MB
independent measurements of the real app. `docs/ARCHITECTURE.md`'s
"idle (listening only) < 300 MB" target holds exactly once: cold, before
either model has ever run (measured 22-57 MB across every worker's baseline
in this investigation, comfortably under 300 MB). It does not hold as a
return-to-idle claim after any real use, in-process, with the libraries as
currently integrated.

## What this means for the governor (data point, not a recommendation)

`nova/governor.py`'s mechanism itself is not wrong — `Governor.check_once()`
was demonstrated (in the T1.2 report) correctly withholding unloads
mid-turn and correctly, honestly reporting when a ceiling can't be reached
with what's registered. What this investigation adds: the *reason* a low
ceiling can't be reached isn't a bug in the governor or in `unload()`'s
bookkeeping — it's that `Transcriber.unload()`'s promise ("frees the
resource") is only ever partially true for host RSS, structurally, given
what CTranslate2 exposes today. No Python-level fix exists for that within
this process — I checked `ctranslate2`'s public API surface
(`dir(ctranslate2)`) for anything resembling a cache-release/reset call and
found nothing. If a smaller floor is required, the only lever I can see
(purely as a data point — this is the lead's design conversation, not
something to build) is a process boundary: running Whisper (and possibly
Kokoro) in a subprocess that can be killed and its memory actually
reclaimed by the OS, trading reload latency for a real return to near-zero.
That is a materially bigger change than the governor, exactly as flagged —
not started here.

## Methodology notes

- All measurements: `psutil.Process().memory_info().rss` for host RSS,
  `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` for
  VRAM (total GPU, not per-process — see the open item in §4).
- Every worker ran in a fresh `sys.executable` subprocess unless the test
  was specifically about combining components in one process (called out
  each time).
- `tracemalloc` was started before any model import and read via
  `get_traced_memory()` immediately before and after `unload()`.
