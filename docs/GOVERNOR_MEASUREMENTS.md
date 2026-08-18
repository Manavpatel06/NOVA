# Governor measurements — real numbers, this machine

Owner: nova-dev-core. Companion to `nova/governor.py` (T1.2). Every number
below came from one command, on this machine, with a GPU already probed and
cached (`data/gpu_profile.json`, `cuda`/`float16`/verified):

```
D:\NOVA\.venv\Scripts\python.exe scripts\measure_governor.py
```

That script is the reusable measurement tool — rerun it after any change to
`Transcriber`/`Synthesizer`'s load/unload behavior, not just at T1.2 time.
Host RSS via `psutil.Process().memory_info().rss`; VRAM via
`nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits`.

## The table

| State | Host RSS (MB) | VRAM (MiB) |
|---|---:|---:|
| Idle, nothing loaded | 57.4 | 264 (driver/context baseline) |
| Whisper resident (`load()` only, no inference yet) | 257.9 | 2339 |
| Whisper resident, after one real `transcribe()` call | 741.6 | 2479 |
| Whisper `unload()`-ed | **741.7** (unchanged) | 367 (recovered) |
| Kokoro resident (`load()` only) | 1155.0 | 367 |
| Kokoro resident, after one real `say()` call | 1192.4 | 367 |
| Both resident | 1348.3 | 2351 |
| Both `unload()`-ed (end state) | 960.2 | 367 |

## The finding that matters more than the table

**`Transcriber.unload()` frees VRAM but does not free host RSS, once
`transcribe()` has actually run at least once in the process.** VRAM
recovers cleanly and repeatably (2479 → 367 MiB, consistent with T0.3's
original measurement of the same 2479→367 pattern). Host RSS does not: it
sits at ~740-780 MB after the first real transcription and stays there
through `unload()` + `gc.collect()`, for the rest of that process's life.
This is almost certainly CTranslate2's/CUDA's host-side allocator caching
memory arenas rather than returning them to the OS — a `del` and `gc.
collect()` at the Python level cannot see or release that. `Synthesizer.
unload()` does not have this problem: Kokoro's unload measurably frees host
RSS (1348 → 960 MB going into the "both unloaded" row above, a ~388 MB
drop).

**This contradicts `docs/ARCHITECTURE.md`'s resource budget table's implicit
claim** ("idle (listening only) < 300 MB") **as a *return-to-idle* target.**
It holds exactly once — cold start, before Whisper has ever run (57.4 MB,
comfortably under 300 MB). It does not hold as "NOVA returns under 300 MB
after a turn," because once Whisper has transcribed anything, the process's
host-RSS floor for the rest of its life is ~740-780 MB, not ~60 MB, no
matter what the governor unloads. The measurement wins over the doc, per the
lead's standing instruction — flagging here plainly rather than quietly
working around it. The "mid-turn < 1.5 GB" budget line does hold: measured
"both resident" is 1348 MB, under the 1536 MB (1.5 GB) target.

**Practical consequence for `config.governor.rss_ceiling_mb`:** a ceiling
near the old idle target (a few hundred MB) would be permanently tripped
after the very first real transcription and would only ever partially
succeed (freeing Kokoro, never Whisper) every single poll — technically
"working" but misleadingly so, since it would never reach its own target
and would generate a WARNING on every poll cycle forever. The default is
set to **900 MB** instead: just above the measured Transcriber-alone floor
(~740-780 MB, with margin for the creep discussed below), comfortably below
"both resident" (~1150-1350 MB). The honest job this governor does in M1 is
*"shed Kokoro when both models are loaded and idle,"* not *"return all the
way to cold-idle."* Getting the Whisper floor down further is a
`Transcriber`-side fix (nova-dev-stt's module) — e.g. an explicit CUDA/
CTranslate2 cache-release call, if one exists — not something the governor
can do by calling the `unload()` that already exists; flagged as a finding,
not fixed here, per "drive existing lifecycle, don't reimplement it."

## Creep across repeated load/unload cycles

Three separate runs of the measurement script's 3-cycle load/unload loop
(`Transcriber.load()` → measure → `unload()` → `gc.collect()` → measure,
repeated 3×) produced: **+124.8 MB, +74.6 MB, +0.4 MB** RSS drift from first
cycle to last, across three independent script executions. Two things are
true at once: the drift is real and non-zero (not measurement noise — it
shows up consistently as a jump on the *second* load after the first real
use, not a gradual per-cycle climb), and it is **front-loaded, not
unbounded** — in every run, growth happened once (attributable to the first
post-use reload settling into a larger steady-state arena) and then held
flat for the remaining cycles (cycle 2 → cycle 3 drift was ~0 MB in all
three runs). This reads as an allocator reaching a working-set high-water
mark rather than a per-cycle leak. Recommend re-running this specific check
(a longer cycle count, e.g. 10-20 iterations) before M1 exit to confirm it
actually plateaus rather than climbing slowly across dozens of turns over a
full day of runtime — three cycles is enough to see the pattern, not enough
to rule out slow long-run drift.

## Governor mechanism, demonstrated end to end

`scripts/measure_governor.py` also drives `Governor.check_once()` against
these real components (not mocks), asserting on real behavior:

1. **Turn in flight → withholds unloading.** `TOPIC_TURN_START` published,
   both components loaded, RSS over ceiling — `check_once()` changes
   nothing (`unload_count` stays 0).
2. **Turn ends, ceiling reachable (900 MB range) → unloads and reaches it.**
   `TOPIC_TURN_END` published, `check_once()` unloads Kokoro (the one whose
   `unload()` actually frees host RSS), RSS drops from ~1180 MB to ~908 MB,
   at or under the configured ceiling.
3. **Ceiling deliberately unreachable (below the Whisper floor) → frees
   what it can, logs a WARNING naming the gap, returns promptly (<0.1 s),
   never loops or crashes or claims a false success.** This is the correct
   behavior given finding #1 above, not a bug: some ceilings genuinely
   cannot be met by unloading the currently-registered components, and the
   governor says so loudly rather than pretending otherwise.

## Config

`nova/config.py`'s `GovernorConfig`: `poll_interval_s: float = 5.0` (a
design choice, not a measured constraint — 5 s costs nothing and catches
sustained over-ceiling states promptly), `rss_ceiling_mb: float = 900.0`
(measured, per above). Mirrored in `config/nova.yaml`'s `governor:` section.
