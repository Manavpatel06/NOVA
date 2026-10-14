# NOVA

## Using NOVA (2026-10-14)

1. Press **Win**, type **NOVA**, press Enter. The NOVA window opens and NOVA starts listening.
2. Start a sentence with its name: **"Nova, what time is it?"**, **"Nova, open Notepad."** Saying just "Nova" gets a "Yes?" — then say the request.
3. Anything said *without* "Nova" at the start is ignored and never stored.
4. You can also type in the window, or hold **Ctrl+Alt+Space** to talk.
5. **Close the window to quit** — the microphone turns off with it.

Requests no built-in skill handles go to Claude (needs the `claude` CLI signed in). Risky actions, and anything Claude suggests doing, ask for your click first.

A voice-first, always-on personal assistant for Manav's Windows 11 laptop, running entirely locally. See `CLAUDE.md` for the constitution this project is held to and `docs/ARCHITECTURE.md` for the full design.

**Status: Milestone 0 — walking skeleton.** The thread `hold Ctrl+Alt+Space → speak → Whisper on the GPU → one real action → Kokoro says the answer aloud` is wired end to end (`nova/app.py`, entry point `python -m nova`) and has been verified by an agent using Kokoro-synthesized speech in place of a live microphone (`docs/EVIDENCE.md`). **M0's exit bar is still explicitly unmet** — `docs/EVIDENCE.md` names three things that cannot be closed without Manav (audible speakers, a real human voice, the UAC hotkey question) and a proven code path is not the same as a met milestone. There is no wake word, no always-on listening, and no local LLM. Those are deliberate scope cuts for M0, not oversights; see `docs/ROADMAP.md`.

## What actually works today

Everything below corresponds to code that exists in `nova/` and either a passing test file or a directly-inspectable artifact on this machine — sources are named so you can check for yourself.

- **Push-to-talk capture.** `nova/interfaces/hotkey.py` installs a global `Ctrl+Alt+Space` hook (`keyboard` package) that fires regardless of window focus, with both hold-to-talk and tap-to-latch modes and a 30s latch failsafe.
- **Real microphone input.** `nova/audio/devices.py` empirically probes every Windows input device (opens it, records ~1s, measures RMS) rather than trusting that a device *opening* means it *works*. On this machine the winner is consistently device 22, "Microphone Array (Realtek HD Audio Mic Array input)" on WDM-KS (RMS varies by probe run — see the measured-facts table below, don't cite a single RMS as fixed). A separate WDM-KS endpoint (device 17, same physical hardware) returns exact digital `0.0` on every trial and is correctly rejected.
- **Real speech-to-text.** `nova/stt/` runs faster-whisper `large-v3-turbo` on this machine's RTX 5060, device `cuda`, compute type `float16`, `verified=True` — read live from `data/gpu_profile.json`. The probe itself only ever trusts a real completed transcription, never CTranslate2's advertised capability list (which claims `int8` support this GPU's compute capability 12.0 does not actually have — see `docs/decisions/0002-int8-refused-on-blackwell.md`).
- **Real text-to-speech.** `nova/tts/synthesizer.py` runs Kokoro ONNX and falls back to Windows SAPI (in-process COM, no subprocess) if Kokoro fails to load or synthesize, logging a WARNING naming Kokoro every time it does. `data/tts_probe_*_kokoro.wav` and `data/tts_probe_*_sapi.wav` on this machine are non-silent output from both code paths, from a prior run of `scripts/probe_tts.py`.
- **A deterministic router.** `nova/brain/router.py` matches spoken text against every skill's declared `examples` (no LLM, no second pattern list) using a blended Jaccard/character-similarity score, calibrated against a labelled phrase set (`scripts/verify_router.py`). Below a confidence floor, or when the top two skills score too close together, it asks a clarifying question instead of guessing — the constitution's confidence-honesty rule, enforced in code.
- **Two skills.** `time` (READ tier, no params) and `open_app` (ACT tier, one param) — see `docs/ADDING_A_SKILL.md` for how they're built and what adding a third actually costs.
- **A security kernel.** `nova/security/` implements the permission ladder (READ/ACT/CONFIRM), a hash-chained append-only audit log that fails closed on ACT/CONFIRM (never on READ, by design), the `Untrusted[T]` wrapper (not a `str` subclass — the whole point), the one shared `redact()` implementation, and `Egress` (the one module allowed to open a socket or hand text to a spawned process). An AST test walks every file under `nova/` and fails the build if anything outside two small, named, size-pinned allowlists imports a transport or spawns a process — see `docs/THREAT_MODEL.md` and `docs/SECURITY_SPEC.md` for why it's scoped that way.
- **The turn loop.** `nova/app.py`'s `NovaApp` wires all of the above through an `EventBus`: hotkey → capture → transcribe → route → ladder-gated skill invocation → speak. Every skill call goes through `SkillSpec.invoke()`, never a handler directly, so every invocation — including READ — produces an audit record.

## What's measured on this machine (not modelled)

Numbers below are marked either **read directly** (this agent has no execution tools; it read a JSON/log artifact a prior run produced) or **lead-verified** (the team lead ran it live on 2026-08-17 and reported the result — see `nova-docs`'s report for the full attribution chain). Nothing in this table is a number this agent produced by running anything itself.

| Fact | Value | Source |
|---|---|---|
| Test suite | 367 passed, 0 failed, 3 skipped | **lead-verified**, `docs/EVIDENCE.md` (resolves an earlier 365-vs-367 discrepancy this agent flagged and could not reconcile itself — see git history for the correction) |
| Selected mic | WDM-KS idx 22, "Microphone Array (Realtek HD Audio Mic Array input)" | consistent across every reading of `data/audio_profile.json` and the lead's report |
| Selected mic RMS | **varies by probe run**: 0.048 (lead-verified, 2026-08-17) / 0.0663 and 0.0557 (this agent, reading `data/audio_profile.json` at two different points in the same session) | RMS depends on ambient noise at probe time, not a fixed constant — three different real readings on the same device is expected, not a documentation error; don't treat any single one of these as "the" RMS |
| Rejected lying endpoint | WDM-KS idx 17, exact RMS 0.0, 3/3 trials | consistent across every reading + lead-verified |
| Same mic via WASAPI/MME | ~1e-4, AGC-suppressed, non-zero but useless for speech | lead-verified |
| STT device | cuda, float16, verified=True | consistent across `data/gpu_profile.json` and lead-verified (re-run from cold cache) |
| GPU | RTX 5060 Laptop GPU (Blackwell sm_120), driver 610.62, ct2 4.8.1 | lead-verified |
| STT probe caveat | `probe_gpu()` verifies against `tiny.en`, not `large-v3-turbo` — the two can diverge; `GpuProfile.verified_model` records which model actually ran | read directly, `nova/stt/gpu.py` + `data/gpu_profile.json` |
| Whisper load / transcribe latency | 1.68-1.80 s load, **0.48-0.69 s transcribe** (widened again after nova-accuracy split cold from warm runs — 0.48 s is a genuine cold-run figure, not an error; quote as a range, the spread is real) | lead-verified, corrected twice |
| VRAM resident / after unload (Transcriber alone) | 2479 MiB / 367 MiB | lead-verified — `Transcriber` measured in isolation via `nvidia-smi` |
| Memory after a full turn loop's `app.stop()` | RSS 912.9 MB (from a 59.4 MB baseline) — a real, structural floor, not a leak (see below); VRAM retention corrected to **~103 MiB**, not the ~300 MiB first reported — that figure was `nvidia-smi` machine-wide contamination from another agent's GPU process, not NOVA's own retained memory | lead-verified + nova-dev-core's diagnosis, `docs/decisions/0006-open-question-memory-does-not-return-to-baseline.md` (finding F16) — mechanism now identified (CTranslate2's own CUDA caching allocator, confirmed by inspecting the DLL directly); VRAM creep across many cycles is the one part still open |
| int8 on this stack | loads and runs without crashing, refused anyway by deliberate policy | `docs/decisions/0002-int8-refused-on-blackwell.md` |
| **TTS → STT round trip** ("what time is it" → "What time is it?"; "open notepad" → "Open Notepad.", both exact) | Kokoro-synthesized speech transcribes correctly through the real STT path | **lead-verified** — the strongest evidence to date that STT+TTS actually agree on this machine, though it's synthetic speech, not a human voice (see below) |
| Router: "open" alone | asks for clarification, does not guess a param | lead-verified |
| Router: "what's the weather today" | scores 0.136 against the nearest skill (well below the 0.42 floor) — clarifies | lead-verified |
| Router: `Untrusted` input | raises `TypeError` at the router boundary | lead-verified |

## What hasn't been demonstrated

Named explicitly, per the lead's 2026-08-17 review:

- **Nobody has heard NOVA speak.** Playback *timing* is confirmed (the stream starts/stops on schedule, `cancel()` cuts audio within one callback block), but nothing has confirmed the audio is actually audible at a sensible volume through real speakers. That needs Manav's ears.
- **Nobody has spoken to NOVA with a real human voice.** Every STT result to date — including the round-trip evidence in the table above — is Kokoro-synthesized speech, not a human's. It proves the pipeline agrees with itself, not that it understands Manav.
- **Elevated/UAC window hotkey coverage is unknown.** Whether the global hotkey still fires when a UAC-elevated window has focus needs one real UAC click from a human. Do not assert it works.
- **No end-to-end *human* demonstration exists yet.** `docs/EVIDENCE.md` (added after this agent flagged that `docs/ARCHITECTURE.md` promised it and it didn't exist) records an agent-run demo of the full turn loop with Kokoro-synthesized speech standing in for a live microphone — real skill invocations, a verified 4-entry audit chain, `open_app` genuinely launching and being pid-verified — and explicitly marks M0's exit bar unmet pending the three human-only items listed above. That resolves the earlier open question in this doc about whether `nova/app.py` was complete: it is, and it's now been run, by an agent, with the result on record; what's still missing is a human doing it.
- **The turn loop (`nova/app.py`) has no dedicated test file.** It's exercised incidentally by `tests/test_source_hygiene.py` (no hardcoded paths, no `os.environ` reads) but nothing runs `NovaApp.run_turn()` against a fake or real turn end to end.
- ~~**A currently-broken test file**~~ — **resolved.** `tests/test_skills_time.py` briefly constructed `SkillCtx(config=Config())` with no `ladder=`, which `SkillCtx` requires by design so a skill invocation can never silently skip authorization. nova-tester fixed it (`ladder=_UnusedLadder()`, lines 28/38/51). Left on the record rather than deleted: the finding was correct when made, and the required-field design is what surfaced it loudly instead of silently.
- **This agent has no shell access this session** (Read/Write/Edit/Glob/Grep only, no Bash tool) and could not run `pytest`, `scripts/probe_*.py`, `python -m nova`, or `git` itself. Numbers above attributed "lead-verified" were run and reported by the team lead, not by this agent; everything else comes from reading source and reading artifacts (`data/*.json`, `data/*.wav`, `.pytest_cache/`) left by prior runs. See `QUICKSTART.md` for the same discipline applied step by step.

## Decisions on record

Short ADRs for choices the team made deliberately, under `docs/decisions/`:
- [`0001-no-local-llm-in-m0.md`](docs/decisions/0001-no-local-llm-in-m0.md) — rules-first routing now, a local LLM slotted in later behind an interface seam, not built yet.
- [`0002-int8-refused-on-blackwell.md`](docs/decisions/0002-int8-refused-on-blackwell.md) — `int8`/`int8_bfloat16` are refused on this GPU's compute capability even though they were measured to work, because the failure mode if a driver update reintroduces the crash is a silent mid-conversation death.
- [`0003-ast-test-covers-process-spawn-not-just-sockets.md`](docs/decisions/0003-ast-test-covers-process-spawn-not-just-sockets.md) — the no-network promise is enforced against `subprocess`/`os.startfile`/etc., not just transport imports, after the retired build's version of this test proved blind to exactly that hole.
- [`0004-wdm-ks-host-api-ranking-not-silence-floor-alone.md`](docs/decisions/0004-wdm-ks-host-api-ranking-not-silence-floor-alone.md) — mic candidates are ranked by host API tier first, RMS second, because the AGC-suppressed WASAPI/MME duplicates of the working mic are non-zero and would otherwise sometimes win a straight loudness contest.
- [`0005-untrusted-is-not-a-dataclass.md`](docs/decisions/0005-untrusted-is-not-a-dataclass.md) — `Untrusted[T]` is hand-rolled, not a `@dataclass`, because `dataclasses.asdict()`/`astuple()` bypass every string-protocol defense a dataclass version had, silently and recursively (finding F12, CRITICAL).
- [`0006-open-question-memory-does-not-return-to-baseline.md`](docs/decisions/0006-open-question-memory-does-not-return-to-baseline.md) — **open question, not a decision**: ~850 MB RSS / ~300 MiB VRAM stay retained after `app.stop()` unloads both models. Diagnosis in progress (finding F16, HIGH).
- [`0007-wake-word-vendored-not-openwakeword-dependency.md`](docs/decisions/0007-wake-word-vendored-not-openwakeword-dependency.md) — `nova/audio/wake.py` is a from-scratch onnxruntime pipeline, not the `openwakeword` package, because that package imports `requests` unconditionally at module load — a general-purpose HTTP client entering `nova/`'s dependency graph would be invisible to the AST no-socket test, which only scans `nova/*.py`, not `site-packages`.
- [`0008-ptt-hook-callback-structurally-cannot-keylog.md`](docs/decisions/0008-ptt-hook-callback-structurally-cannot-keylog.md) — the global keyboard hook's callback is split so the raw per-keystroke event exists in scope for two reviewed lines only; everything downstream (the tap/hold/latch state machine) receives a stripped, non-identifying tuple and has no way to reach for raw key data even by accident.
- [`0009-confirm-denial-reasons-are-not-interchangeable.md`](docs/decisions/0009-confirm-denial-reasons-are-not-interchangeable.md) — CONFIRM denials get three distinct audit reasons (`confirm_declined`/`confirm_timed_out`/`confirm_channel_error`), because an audit entry that says "a human declined" when no human was ever consulted is a false record that will be believed, not just an imprecise one.
- [`0010-egress-blocks-all-redirects.md`](docs/decisions/0010-egress-blocks-all-redirects.md) — `Egress.fetch()` blocks every HTTP redirect rather than re-validating each hop, because the allowlist check validated the URL given, not what `urlopen()` does with it afterward (finding F13, demonstrated with two local servers).

## What's next (M1, in progress)

`nova/audio/wake.py` (wake-word detection, `hey_jarvis` stand-in) and `nova/audio/vad.py` (VAD) both now exist in code — this README previously said neither did; that was stale the moment M1 work landed, and this section is a live target, not a fixed snapshot. Check `docs/TASKS.md`'s task board for current state before trusting this paragraph over it. M1 also adds a tray icon and a working CONFIRM UI. See `docs/ROADMAP.md` for the full milestone list and `docs/TASKS.md` for the live task board, findings register, and measured-facts table this README's numbers are drawn from.

## Getting started

See [`QUICKSTART.md`](QUICKSTART.md) for setup and running NOVA on this machine, and [`docs/ADDING_A_SKILL.md`](docs/ADDING_A_SKILL.md) for how to add a new capability.
