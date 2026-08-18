# ADR 0002: `int8`/`int8_bfloat16` refused on compute capability ≥ 12, even though they work

**Status:** decided, recorded in `docs/CONTRACTS.md` (T0.3) and `nova/stt/gpu.py` (`_REFUSED_ON_CC12`).

## Context

`nova/stt/gpu.py`'s `probe_gpu()` never trusts CTranslate2's advertised compute-type support (`ctranslate2.get_supported_compute_types("cuda")` is a compile-time list, not a runtime test) — it loads a real model and runs a real transcription for every candidate, in order, and keeps the first that genuinely completes.

Prior art (the retired build) recorded `int8` loading fine on this GPU class and then dying with `CUBLAS_STATUS_NOT_SUPPORTED` on the first encoder run — a silent-until-inference failure. When this project re-measured on the actual hardware in use (RTX 5060 Laptop GPU, compute capability 12.0, driver 610.62, cuBLAS 12.9.2, cuDNN 9.24, ct2 4.8.1), **that failure did not reproduce**: both `int8` and `int8_float16` completed a full transcription on `tiny.en` and on `large-v3-turbo`.

## Decision

`int8` and `int8_bfloat16` are refused outright as probe candidates on any GPU with compute capability ≥ 12 (`_REFUSED_ON_CC12`, `_REFUSED_MIN_COMPUTE_CAPABILITY = 12.0`), regardless of whether they're currently observed to work. `float16` is the verified, used compute type; `float16 → int8_float16` is the only candidate order tried, and `int8_float16` is not in the refused set (untested territory, not the same code path as the confirmed-historically-broken plain `int8`).

## Why

This is **not** "int8 crashes on this stack" — measured, it doesn't. It's a deliberate risk trade against a specific failure mode: `int8`'s VRAM saving over `float16` is real but small on an 8 GB card that already fits `float16` comfortably (~2.4 GB resident), and a driver update silently reintroducing the CUBLAS failure prior art already hit once would surface as a **mid-conversation crash with no warning**, not a boot-time refusal. The refusal costs nothing measurable today and removes that failure mode entirely. Revisit only if VRAM becomes the actual binding constraint — at which point this decision should be re-opened with a measurement showing the constraint is real, not assumed.

## Consequences

- `GpuProfile` records which model the verification actually ran against (`verified_model`), because the probe verifies with `tiny.en` for speed while the assistant runs `large-v3-turbo` — a compute type that works small and fails large would otherwise pass silently. This field exists specifically because of the same "don't trust what wasn't actually measured" discipline this ADR is an instance of.
- If this decision is ever revisited, it needs the same standard of proof it was made with: a real measured VRAM constraint on real hardware, not "int8 should save memory."
