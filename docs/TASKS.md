# NOVA — Shared Task List

Maintained by the lead. Truthful at all times: a task moves to DONE only when
tests pass, security has reviewed, accuracy has re-run the claims, and — where
hardware is involved — it has been demonstrated on this laptop.

Legend: TODO · CLAIMED · IN REVIEW · **DONE**

## Milestone 0 — walking skeleton

| # | Task | Owner | State |
|---|---|---|---|
| T0.1 | Core: config, bus, logging, pyproject, pytest harness | nova-dev-core | IN REVIEW (built, lead-verified; awaiting tests+security) |
| T0.2 | Audio: device prober (WASAPI trap), capture, player | nova-dev-audio | CLAIMED |
| T0.3 | STT: CUDA DLL setup, empirical compute-type probe, Transcriber | nova-dev-stt | CLAIMED |
| T0.4 | TTS: Kokoro ONNX synthesizer + SAPI fallback | nova-dev-audio | TODO (after T0.2) |
| T0.5 | Security kernel v0 + skill registry + `time`/`open_app` skills | nova-dev-core | TODO (after T0.1) |
| T0.6 | Turn loop + push-to-talk hotkey, wired end to end | lead | TODO (after all) |
| T0.T | Contract-first test suites for T0.1–T0.5 | nova-tester | CLAIMED |
| T0.S | Threat model + review of security kernel and Claude-arm egress boundary | nova-security | CLAIMED |
| T0.A | Independent verification of every M0 claim | nova-accuracy | TODO (after T0.6) |
| T0.D | README + QUICKSTART reflecting what actually runs | nova-docs | TODO (after T0.6) |

## Decisions taken (do not re-litigate)

- Model weights salvaged from the retired build and byte-verified. No downloads.
- **No local LLM.** Deterministic rules handle commands; open-ended language goes
  to the Claude CLI. The brain sits behind an interface so a local model can drop
  in later without a rewrite.
- Wake word ships as `hey_jarvis` until "hey nova" is trained in Colab.
- STT: `large-v3-turbo`, cuda/float16. int8 is broken on Blackwell.
