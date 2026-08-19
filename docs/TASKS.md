# NOVA — Shared Task List

Maintained by the lead. Truthful at all times: a task moves to DONE only when
tests pass, security has reviewed, accuracy has re-run the claims, and — where
hardware is involved — it has been demonstrated on this laptop.

Legend: TODO · CLAIMED · IN REVIEW · **DONE**

## Milestone 0 — walking skeleton

| # | Task | Owner | State |
|---|---|---|---|
| T0.1 | Core: config, bus, logging, pyproject, pytest harness | nova-dev-core | **DONE** (f45093d) |
| T0.2 | Audio: device prober, capture, player, resampler | nova-dev-audio | **DONE** (9923863 + 7afe5d0 cache fix) |
| T0.6a | Push-to-talk hotkey (`nova/interfaces/hotkey.py`) | nova-dev-audio | **DONE** (aeb6805, d425b8e, 4da6851) |
| T0.3 | STT: CUDA DLL setup, empirical compute-type probe, Transcriber | nova-dev-stt | **DONE** (4f9045a + cd6b8bf) |
| T0.4 | TTS: Kokoro ONNX synthesizer + SAPI fallback + TTS→STT round trip | nova-dev-stt | **DONE** (73a3226) |
| T0.5a | Skill registry, Tier, hash-chained AuditLog, `time`/`open_app` | nova-dev-core | **DONE** (13b0b6f) |
| T0.5b | `Untrusted`, redact, Egress, Ladder | nova-dev-sec | **DONE** (a7cac32 + dbd77d8) |
| T0.6b | Turn loop wired end to end | lead | **DONE** (7f4db3f) |
| T0.T | Contract-first test suites for T0.1–T0.5 | nova-tester | **DONE** (93a5e2f, a502d79, 25b0b5b) |
| T0.S | Threat model + Claude-arm egress verdict | nova-security | **DONE** (d2cf9fa; contract amended) |
| T0.S2 | Implementable spec for `Untrusted` + `Egress` + spawn allowlist | nova-security | **DONE** (3439e00) |
| T0.S3 | Review landed audio + stt modules; DLL-path verdict | nova-security | CLAIMED |
| T0.A | Independent verification of every M0 claim | nova-accuracy | IN PROGRESS (nova-accuracy) |
| T0.D | README + QUICKSTART reflecting what actually runs | nova-docs | **DONE** (128bf21) |

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
| Mic selected | WDM-KS idx 22; RMS 0.048 at first probe, 0.0663 on re-probe | real capture (RMS varies with ambient noise; the device choice is the stable fact, not the number) |
| Lying endpoint | WDM-KS idx 17, exactly 0.0, 3/3 trials | real capture |
| Same mic via WASAPI/MME | ~1e-4, AGC-suppressed, non-zero but useless | real capture |
| Whisper load | 1.68-1.79 s (5 runs) | large-v3-turbo; an earlier single run said 1.48 s and was optimistic |
| Whisper transcribe | 0.68-0.69 s for 3.0 s audio (5 runs) | synthetic tone; earlier single run said 0.44 s |
| VRAM, model resident | 2479 MiB | nvidia-smi |
| VRAM after unload | 367 MiB | nvidia-smi — the C++ side really frees it |
| int8 on this stack | **does not crash** | contrary to prior art; refused anyway, deliberately |

## Open findings (routed, not yet fixed)

| # | Finding | Severity | Owner | Status |
|---|---|---|---|---|
| F1 | **FIXED** (7afe5d0), independently re-verified by nova-accuracy with its own adversarial index-swap test. Cached device index is not a stable identity; fingerprint sorts order away, so a device reorder silently rebinds capture to a different device | HIGH (undoes the host-API ranking that avoids the dead/AGC-crushed endpoints) | nova-dev-audio | queued after T0.6a |
| F8 | Hash-pinning deferred to M1: pip's `--require-hashes` is all-or-nothing and auto-enables when ANY line has a hash, so partial pinning breaks `pip install -r` entirely; needs the full transitive closure via pip-compile | MEDIUM (supply chain) | lead | deferred to M1, deliberate |
| F9 | two-function shim so raw key events leave scope after two reviewed lines | HIGH sensitivity | nova-dev-audio | **FIXED** (4da6851), AST-guarded by a test (ff1a87d) |
| F10 | `_on_event` had no `except Exception`; Windows silently removes a hook that throws | MEDIUM (availability) | nova-dev-audio | **FIXED** (4da6851), tested with a secret sentinel (ff1a87d) |
| F11 | Elevated/UAC window hotkey coverage unverified — needs one human UAC click | UNKNOWN | Manav, at demo | open question |
| F2 | `_dll.py` junction TOCTOU | LOW | nova-dev-stt | **FIXED** cd6b8bf, defense proven with a real junction |
| F3 | `_paths.py` duplication | LOW | nova-dev-stt | **FIXED** 73a3226, now a thin re-export shim |
| F4 | Hotkey hook sees every keystroke system-wide; needs never-log guarantee, possibly structural | HIGH sensitivity | nova-dev-audio + nova-security | in design |
| F5 | LATCHED state could hold the mic open indefinitely after a stray keypress | HIGH | nova-dev-audio | fix required pre-commit |
| F6 | `Player` not safe for concurrent `play()`; needs docstring | LOW | nova-dev-audio | queued |
| F7 | `tts.speed` inf/nan **FIXED** 13b0b6f; `stt.model` membership check deferred (drags a filesystem dep into pure parsing) | LOW | nova-dev-core | partial, deferred |

| F12 | `Untrusted` fully unwrapped by `dataclasses.asdict()`/`astuple()` — silent, no audit, recursive through nested dataclasses | **CRITICAL** | nova-dev-sec | **FIXED** dbd77d8, verified by lead |
| F13 | **FIXED** (62470ec) — all redirects blocked at `redirect_request`, audited as a distinct event; independently reproduced by nova-accuracy before the fix. `Egress.fetch()` validated the original URL's host then lets urlopen follow redirects unchecked — classic allowlist bypass via an open redirect on an allowlisted host; demonstrated with two local servers | HIGH (not yet reachable; nothing calls fetch) | nova-dev-sec | fixing now |
| F14 | **FIXED** (2f433f6) — `redact()` composed into `AuditLog._sanitize()` rather than `Ladder._record()`, so it covers every caller present and future, not just the one call site. Was: `Ladder._record()` sanitizes audit params by key name only, not content, unlike `Egress.release_for_process` which runs real `redact()`. Harmless while params are short app names; records free text verbatim once M4/M5 skills take it. Two redaction disciplines where there should be one | MEDIUM (latent) | nova-dev-sec | scheduled before M4 |
| F15 | Router `confidence_floor=0.42` / `ambiguity_margin=0.08` calibrated against a 2-skill vocabulary; must be re-calibrated whenever the registry grows materially. The *method* (labelled phrase set, report worst clean match vs best off-topic) is what to preserve, not the numbers | LOW now, MEDIUM as skills grow | nova-dev-core | recorded, revisit at M2 |
| F16 | **Memory does not return to baseline after unload.** Lead-measured across a real 3-turn run: RSS 59.4 MB at start -> 1254.6 MB loaded -> **912.9 MB after `app.stop()`**; VRAM 264 -> 2479 -> **367 MiB** (reproduced twice by nova-accuracy). So ~850 MB of host RSS is retained; VRAM retention is **~103 MiB**, not the ~300 MiB this finding first claimed -- my 566/2570 readings were contaminated, because `nvidia-smi` reports machine-wide VRAM and another agent's GPU process was resident when I measured. Lazy loading works (65 MB after construction); *un*-loading does not restore idle. On a 16 GB machine Manav also uses, an assistant that settles at ~900 MB instead of ~60 MB after one conversation is a materially different product from the one ARCHITECTURE.md describes. Likely onnxruntime arenas / CUDA context / cuDNN kernels rather than a Python-level leak — needs diagnosis, not assumption | **HIGH** | nova-dev-core (governor) | open, diagnose before M1 closes |
| F17 | **`Untrusted._value` is a second, unaudited way out.** The AST test scans only for `reveal` as import/attr/name — it never checks `._value` access, so `text = u._value` defeats all three SECURITY_SPEC §1.3 layers silently, with no test failure. The documented "exactly one named `reveal()`" claim is therefore not structurally true. Found by nova-accuracy adversarially, not by the suite | **HIGH** | nova-tester (AST extension) | open |
| F18 | **Playback uses the OS default output device with no verification.** This machine has four output endpoints — separate Realtek "Headphones" and "Speakers", plus an Insignia TV over NVIDIA HDMI. The default moves (observed as NVIDIA HDMI by nova-dev-audio; as Realtek Headphones by the lead minutes later). If it lands on an unused endpoint, NOVA is inaudible while synthesis works perfectly — the constitution's "never go silently mute", violated from the output side. The input side already gets empirical probing; the output side gets none | **HIGH — blocked a trustworthy demo** | lead (team offline) | **FIXED** (df37f5e) — `nova/audio/output.py` probes and prefers a local endpoint over display audio; `scripts/probe_output.py` is the human diagnostic. Needs nova-dev-audio review + nova-tester coverage |
| F19 | Shared venv lost transitive deps (`tqdm`, `joblib`) and took the suite red with nothing in the manifest able to notice. `pip check` is the one-second guard; full-closure pinning deferred to M1 with F8 | LOW | lead | documented, deferred |
| F20 | **MME truncates device names to 31 characters.** This machine's TV enumerates as `NS-39D400NA14 (NVIDIA High Defi`, so display-endpoint markers written from the full driver name matched nothing and classified a television as a local speaker. Caught only by testing the detector against real enumerated names rather than documented ones. Generalizes: never pattern-match Windows audio device names against strings longer than 31 chars | MEDIUM (latent, now handled) | lead | **FIXED** in df37f5e, documented in-code |
| F21 | **VRAM leaks ~8 MiB per model reload, linearly, and does not plateau.** Lead-run 3-condition experiment (`scripts/measure_drift.py`, nova-accuracy's design). **A** (20 reloads x 1 use) and **B** (20 reloads x 5 uses) both leaked **+158 MiB over 20 cycles** — identical despite 5x the inference volume. **C** (1 load, 40 transcriptions, zero reloads) showed **zero** VRAM growth. So the leak tracks the **reload act**, not inference. Host RSS is flat in all three conditions (+0.5 to +0.7 MB — settled, not a leak). The sting: **the governor's unload/reload is precisely what triggers it**, so the component built to save memory is what exhausts the GPU. ~8 MiB/reload against ~5.4 GB of usable headroom is roughly 675 reloads to exhaustion — weeks, not months, for an always-on assistant | **HIGH — blocks always-on** | lead → decision needed | open |
