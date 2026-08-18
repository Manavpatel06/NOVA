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

## Open findings (routed, not yet fixed)

| # | Finding | Severity | Owner | Status |
|---|---|---|---|---|
| F1 | Cached device index is not a stable identity; fingerprint sorts order away, so a device reorder silently rebinds capture to a different device | HIGH (undoes the host-API ranking that avoids the dead/AGC-crushed endpoints) | nova-dev-audio | queued after T0.6a |
| F8 | Hash-pinning deferred to M1: pip's `--require-hashes` is all-or-nothing and auto-enables when ANY line has a hash, so partial pinning breaks `pip install -r` entirely; needs the full transitive closure via pip-compile | MEDIUM (supply chain) | lead | deferred to M1, deliberate |
| F9 | `_on_event` needs the two-function shim so raw key events leave scope after two reviewed lines | HIGH sensitivity | nova-dev-audio | queued |
| F10 | `_on_event` has no `except Exception`; Windows silently removes a WH_KEYBOARD_LL hook that throws — kills PTT with no signal | MEDIUM (availability) | nova-dev-audio | queued |
| F11 | Elevated/UAC window hotkey coverage unverified — needs one human UAC click | UNKNOWN | Manav, at demo | open question |
| F2 | `_dll.py` junction TOCTOU | LOW | nova-dev-stt | **FIXED** cd6b8bf, defense proven with a real junction |
| F3 | `_paths.py` duplication | LOW | nova-dev-stt | **FIXED** 73a3226, now a thin re-export shim |
| F4 | Hotkey hook sees every keystroke system-wide; needs never-log guarantee, possibly structural | HIGH sensitivity | nova-dev-audio + nova-security | in design |
| F5 | LATCHED state could hold the mic open indefinitely after a stray keypress | HIGH | nova-dev-audio | fix required pre-commit |
| F6 | `Player` not safe for concurrent `play()`; needs docstring | LOW | nova-dev-audio | queued |
| F7 | `tts.speed` inf/nan **FIXED** 13b0b6f; `stt.model` membership check deferred (drags a filesystem dep into pure parsing) | LOW | nova-dev-core | partial, deferred |

| F12 | `Untrusted` fully unwrapped by `dataclasses.asdict()`/`astuple()` — silent, no audit, recursive through nested dataclasses | **CRITICAL** | nova-dev-sec | **FIXED** dbd77d8, verified by lead |
| F13 | `Egress.fetch()` validates the original URL's host then lets urlopen follow redirects unchecked — classic allowlist bypass via an open redirect on an allowlisted host; demonstrated with two local servers | HIGH (not yet reachable; nothing calls fetch) | nova-dev-sec | fixing now |
| F14 | `Ladder._record()` sanitizes audit params by key name only, not content, unlike `Egress.release_for_process` which runs real `redact()`. Harmless while params are short app names; records free text verbatim once M4/M5 skills take it. Two redaction disciplines where there should be one | MEDIUM (latent) | nova-dev-sec | scheduled before M4 |
| F15 | Router `confidence_floor=0.42` / `ambiguity_margin=0.08` calibrated against a 2-skill vocabulary; must be re-calibrated whenever the registry grows materially. The *method* (labelled phrase set, report worst clean match vs best off-topic) is what to preserve, not the numbers | LOW now, MEDIUM as skills grow | nova-dev-core | recorded, revisit at M2 |
