# ADR 0006 (open question): memory does not return to baseline after unload

**Status: open question, not a decision.** No design choice has been made yet — this document exists to record the measurement precisely so the eventual decision has a fixed reference point, per the lead's request. Tracked as finding **F16** in `docs/TASKS.md` (HIGH severity), owned by nova-dev-core (the governor), diagnosis in progress as of this writing. Do not read anything below as resolved.

## The measurement

Lead-measured across one real 3-turn run on this machine (`docs/EVIDENCE.md`):

```
                              RSS          VRAM
start                       59.4 MB      264 MiB
after NovaApp()             64.8 MB      264 MiB   lazy loading confirmed
after 3 turns             1254.6 MB     2570 MiB
after app.stop()           912.9 MB      566 MiB   does NOT return to idle
```

Two things are true at once, and both matter:

1. **Lazy loading works as designed.** Constructing `NovaApp()` costs essentially nothing (59.4 → 64.8 MB RSS, VRAM untouched) — `Transcriber`/`Synthesizer` genuinely defer loading until first real use, matching `docs/ARCHITECTURE.md`'s resource-budget table and `nova/app.py`'s own docstring claim ("Construction is cheap and loads no models").
2. **Unloading does not restore idle.** After `app.stop()` calls `self.transcriber.unload()` and `self.synth.unload()`, RSS settles at 912.9 MB — not back to the ~60 MB idle baseline — and VRAM settles at 566 MiB, not back to 264 MiB. Roughly **850 MB of host memory and ~300 MiB of VRAM are retained** after both models report themselves unloaded.

## Why this matters

`docs/ARCHITECTURE.md`'s resource budget table states an idle target of "< 300 MB RSS, 0 VRAM." An assistant that goes idle → mid-turn → *"idle"-that-is-actually-~900 MB* after one conversation, on a 16 GB machine Manav also uses for other work, is a materially different product from the one that document describes — not a rounding error against the target, roughly 3x over it, and specifically after the point in the code where NOVA believes it has released its models.

## What's known so far, and what isn't

- `Transcriber.unload()` sets `self._model = None` and calls `gc.collect()` — the same pattern `docs/TASKS.md`'s earlier measured-facts table credits with genuinely freeing VRAM in isolation (2479 MiB resident → 367 MiB after unload, measured via `nvidia-smi` against `Transcriber` alone, not the full `NovaApp`). That isolated measurement and this full-app measurement disagree on outcome, which is itself a data point: something about the *combination* of components in a real run, not `Transcriber.unload()` in isolation, is retaining memory.
- The current working hypothesis (not yet confirmed) is onnxruntime session arenas, a retained CUDA context, or cached cuDNN kernels/workspaces surviving `gc.collect()` because they're held by native (non-Python-heap) allocators that a Python-level `del`/`gc.collect()` doesn't reach — **not** a Python object-reference leak, since `gc.collect()` is already in the unload path and RSS still doesn't return to baseline. This is stated as a hypothesis under active diagnosis, not a finding — nova-dev-core is investigating, and "it's probably X" is exactly the kind of unproven framing `CLAUDE.md`'s verification rules ask to be held to a higher standard, not a lower one, because it's the comfortable explanation.
- Not yet measured: whether the retained memory grows further across additional turns/unload cycles (a real leak, growing without bound) or plateaus after the first cycle (a one-time retained arena/context, bad but bounded). This distinction changes the severity and the fix, and neither has been checked yet.

## What would need to be true to close this

At minimum: a repeat of the same 3-turn measurement across multiple unload cycles to distinguish "plateaus" from "grows," and an isolated test of each of `Transcriber`, `Synthesizer`, and the CUDA context individually (not just the full `NovaApp`) to localize which component's unload path is retaining memory, since the isolated `Transcriber`-only measurement in `docs/TASKS.md` did not show this problem and the full-app measurement does.
