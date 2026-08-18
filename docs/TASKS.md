# NOVA — Shared Task List

Maintained by the lead. Truthful at all times: a task moves to DONE only when
tests pass, security has reviewed, accuracy has re-run the claims, and — where
hardware is involved — it has been demonstrated on this laptop.

Legend: TODO · CLAIMED · IN REVIEW · **DONE**

## Milestone 0 — walking skeleton

| # | Task | Owner | State |
|---|---|---|---|
| T0.1 | Core: config, bus, logging, pyproject, pytest harness | nova-dev-core | IN REVIEW (built, lead-verified; awaiting tests+security) |
| T0.2 | Audio: device prober, capture, player, resampler | nova-dev-audio | IN REVIEW (9923863; verified on real mic) |
| T0.6a | Push-to-talk hotkey (`nova/interfaces/hotkey.py`) | nova-dev-audio | CLAIMED |
| T0.3 | STT: CUDA DLL setup, empirical compute-type probe, Transcriber | nova-dev-stt | IN REVIEW (GPU verified by lead from cold cache: cuda/float16/verified) |
| T0.4 | TTS: Kokoro ONNX synthesizer + SAPI fallback + TTS→STT round trip | nova-dev-stt | CLAIMED (reassigned; audio dev still on T0.2) |
| T0.5a | Skill registry, Tier, hash-chained AuditLog, `time`/`open_app` | nova-dev-core | CLAIMED |
| T0.5b | `Untrusted`, redact, Egress, Ladder | nova-dev-sec | CLAIMED |
| T0.6b | Turn loop wired end to end | lead | TODO (after T0.4, T0.5a, T0.6a) |
| T0.T | Contract-first test suites for T0.1–T0.5 | nova-tester | CLAIMED |
| T0.S | Threat model + Claude-arm egress verdict | nova-security | **DONE** (d2cf9fa; contract amended) |
| T0.S2 | Implementable spec for `Untrusted` + `Egress` + spawn allowlist | nova-security | **DONE** (3439e00) |
| T0.S3 | Review landed audio + stt modules; DLL-path verdict | nova-security | CLAIMED |
| T0.A | Independent verification of every M0 claim | nova-accuracy | TODO (after T0.6) |
| T0.D | README + QUICKSTART reflecting what actually runs | nova-docs | TODO (after T0.6) |

## Decisions taken (do not re-litigate)

- Model weights salvaged from the retired build and byte-verified. No downloads.
- **No local LLM.** Deterministic rules handle commands; open-ended language goes
  to the Claude CLI. The brain sits behind an interface so a local model can drop
  in later without a rewrite.
- Wake word ships as `hey_jarvis` until "hey nova" is trained in Colab.
- STT: `large-v3-turbo`, cuda/float16. int8 is broken on Blackwell.

## Measured on this machine (not modelled)

| Fact | Value | How |
|---|---|---|
| STT device | cuda, float16, verified | probe re-run cold by lead |
| Mic selected | WDM-KS idx 22, RMS 0.048 | real capture |
| Lying endpoint | WDM-KS idx 17, exactly 0.0, 3/3 trials | real capture |
| Same mic via WASAPI/MME | ~1e-4, AGC-suppressed, non-zero but useless | real capture |
| Whisper load | 1.48 s | large-v3-turbo |
| Whisper transcribe | 0.44 s for 3.0 s audio | synthetic tone |
| VRAM, model resident | 2479 MiB | nvidia-smi |
| VRAM after unload | 367 MiB | nvidia-smi — the C++ side really frees it |
| int8 on this stack | **does not crash** | contrary to prior art; refused anyway, deliberately |
