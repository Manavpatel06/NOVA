# ADR 0007: Wake-word detection is vendored against onnxruntime, not the `openwakeword` package

**Status:** decided, implemented in `nova/audio/wake.py`.

## Context

M1 needs a wake-word detector for `hey_jarvis` (the stand-in until a "hey nova" model is trained — see `docs/TASKS.md`'s decisions register). `openwakeword` is the obvious, standard PyPI package for this — it's what the on-disk model files (`models/wake/melspectrogram.onnx`, `embedding_model.onnx`, `hey_jarvis_v0.1.onnx`) were originally designed to run under.

**The disqualifying finding:** `openwakeword`'s own `utils.py` does `import requests` unconditionally at module load — not inside a function that's only called for a specific feature (e.g. downloading a model), but at import time, every time anything imports the package. The lead independently confirmed the consequence: `requests` is not installed in this venv at all, i.e. the team did not quietly let it in as a transitive dependency to make `openwakeword` work.

## Decision

`nova/audio/wake.py` is a from-scratch `onnxruntime` inference pipeline, not a wrapper around the `openwakeword` package. The algorithm was read from `openwakeword`'s source (Apache 2.0 — reading and reimplementing is licensed, redistributing their code isn't needed), and every tensor shape was verified against the actual on-disk ONNX models at runtime rather than trusted from the source or from memory of it:

- `melspectrogram.onnx` — confirmed to want raw INT16-magnitude float32 input, not normalized `-1..1`, by reading `openwakeword`'s input validation and then actually running it.
- `embedding_model.onnx` — confirmed via `get_inputs()` to want exactly 76 mel-frames × 32 bins, producing a 96-dim embedding.
- The classifier's expected embedding count (`N`) is **read from the model's own input shape at load time**, not hardcoded — so a differently-shaped `hey_nova.onnx`, whenever it's trained, works with zero code changes here.

A second, smaller decision rides along with the first: `wake.py` does **not** replicate `openwakeword`'s incremental buffer-indexing algorithm, because their own code comment admits streaming vs. full-recompute results differ slightly and it's "unclear if [the difference is] significant." Recomputing the melspectrogram over a small bounded ring of recent audio on each detection step costs more CPU than the incremental version, but its correctness is easy to reason about directly rather than inherited as an unverified assumption from someone else's comment. Whether that CPU cost is acceptable is a measured question (`docs/TASKS.md`'s T1.1 report), not an assumed one.

## Why

This is a precedent, not a one-off library swap. The constitution's egress promise ("all network egress goes through ONE sanctioned module... a test proves no other file opens a socket") is enforced today by an AST scan of `nova/*.py` (`tests/test_no_network_or_process_spawn.py`, extended per `docs/decisions/0003-ast-test-covers-process-spawn-not-just-sockets.md`). That scan is scoped to files under `nova/` — it has no visibility into what a third-party dependency does at its own import time. A general-purpose HTTP client entering `nova/`'s dependency graph, even unused, even "just for a utility function nobody calls," moves the actual guarantee from **"provably true, checked on every commit"** to **"true as long as nobody calls the function that uses it"** — a promise about restraint, not a promise the test suite can verify. That degradation is invisible to the AST test specifically because the test can't see inside `site-packages`.

Concretely, for future contributors choosing a dependency: **check what a candidate package imports at its own module load time, not just what `nova/` would import to use it.** `pip show`/reading `utils.py` directly is cheap; discovering a transitive `requests` import after it's already shipped is not.

## Consequences

- `wake.py` costs real engineering time and ongoing maintenance that a dependency would have absorbed — model-shape assumptions, any future upstream bugfixes, and edge cases are now this codebase's responsibility to track, not `openwakeword`'s.
- The payoff already showed up once: reading the on-disk model's real input shape instead of hardcoding a constant `N` means the eventual `hey_nova.onnx` swap (whenever it's trained) needs zero code changes in `wake.py` — a property that fell out of doing the shape-verification work carefully, not something planned as a separate feature.
- This is the same category of decision as `docs/decisions/0002-int8-refused-on-blackwell.md`: refusing a technically-working shortcut because of what it costs the surrounding guarantee, not because it doesn't work.
