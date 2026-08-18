# NOVA

A voice-first, always-on personal assistant for Manav's Windows 11 laptop, running entirely locally. See `CLAUDE.md` for the constitution this project is held to and `docs/ARCHITECTURE.md` for the full design.

**Status: Milestone 0 — walking skeleton.** The thread `hold Ctrl+Alt+Space → speak → Whisper on the GPU → one real action → Kokoro says the answer aloud` is wired end to end (`nova/app.py`, entry point `python -m nova`). It has not yet been demonstrated live on this laptop by a human with a stopwatch — see "What hasn't been demonstrated" below. There is no wake word, no always-on listening, and no local LLM. Those are deliberate scope cuts for M0, not oversights; see `docs/ROADMAP.md`.

## What actually works today

Everything below corresponds to code that exists in `nova/` and either a passing test file or a directly-inspectable artifact on this machine — sources are named so you can check for yourself.

- **Push-to-talk capture.** `nova/interfaces/hotkey.py` installs a global `Ctrl+Alt+Space` hook (`keyboard` package) that fires regardless of window focus, with both hold-to-talk and tap-to-latch modes and a 30s latch failsafe.
- **Real microphone input.** `nova/audio/devices.py` empirically probes every Windows input device (opens it, records ~1s, measures RMS) rather than trusting that a device *opening* means it *works*. On this machine the winner is device 22, "Microphone Array (Realtek HD Audio Mic Array input)" on WDM-KS, RMS 0.0663 — read live from `data/audio_profile.json`. A separate WDM-KS endpoint (device 17, same physical hardware) returns exact digital `0.0` on every trial and is correctly rejected.
- **Real speech-to-text.** `nova/stt/` runs faster-whisper `large-v3-turbo` on this machine's RTX 5060, device `cuda`, compute type `float16`, `verified=True` — read live from `data/gpu_profile.json`. The probe itself only ever trusts a real completed transcription, never CTranslate2's advertised capability list (which claims `int8` support this GPU's compute capability 12.0 does not actually have — see `docs/decisions/0002-int8-refused-on-blackwell.md`).
- **Real text-to-speech.** `nova/tts/synthesizer.py` runs Kokoro ONNX and falls back to Windows SAPI (in-process COM, no subprocess) if Kokoro fails to load or synthesize, logging a WARNING naming Kokoro every time it does. `data/tts_probe_*_kokoro.wav` and `data/tts_probe_*_sapi.wav` on this machine are non-silent output from both code paths, from a prior run of `scripts/probe_tts.py`.
- **A deterministic router.** `nova/brain/router.py` matches spoken text against every skill's declared `examples` (no LLM, no second pattern list) using a blended Jaccard/character-similarity score, calibrated against a labelled phrase set (`scripts/verify_router.py`). Below a confidence floor, or when the top two skills score too close together, it asks a clarifying question instead of guessing — the constitution's confidence-honesty rule, enforced in code.
- **Two skills.** `time` (READ tier, no params) and `open_app` (ACT tier, one param) — see `docs/ADDING_A_SKILL.md` for how they're built and what adding a third actually costs.
- **A security kernel.** `nova/security/` implements the permission ladder (READ/ACT/CONFIRM), a hash-chained append-only audit log that fails closed on ACT/CONFIRM (never on READ, by design), the `Untrusted[T]` wrapper (not a `str` subclass — the whole point), the one shared `redact()` implementation, and `Egress` (the one module allowed to open a socket or hand text to a spawned process). An AST test walks every file under `nova/` and fails the build if anything outside two small, named, size-pinned allowlists imports a transport or spawns a process — see `docs/THREAT_MODEL.md` and `docs/SECURITY_SPEC.md` for why it's scoped that way.
- **The turn loop.** `nova/app.py`'s `NovaApp` wires all of the above through an `EventBus`: hotkey → capture → transcribe → route → ladder-gated skill invocation → speak. Every skill call goes through `SkillSpec.invoke()`, never a handler directly, so every invocation — including READ — produces an audit record.

## What's measured on this machine (not modelled)

| Fact | Value | Source |
|---|---|---|
| Selected mic | WDM-KS idx 22, "Microphone Array (Realtek HD Audio Mic Array input)", RMS 0.0663 | `data/audio_profile.json`, read directly (this session) |
| Rejected lying endpoint | WDM-KS idx 17, exact RMS 0.0, 3/3 trials | `data/audio_profile.json`, read directly |
| STT device | cuda, float16, verified=True | `data/gpu_profile.json`, read directly |
| GPU | RTX 5060 Laptop GPU, driver 610.62, ct2 4.8.1 | `data/gpu_profile.json`, read directly |
| STT probe caveat | probe verified against `tiny.en`, not `large-v3-turbo` — the two can diverge; `GpuProfile.verified_model` records which model actually ran | `data/gpu_profile.json` (`verified_model: "tiny.en"`) + `nova/stt/gpu.py` |
| Whisper load / transcribe latency | 1.48s load, 0.44s for 3.0s of synthetic audio | `docs/TASKS.md`'s measured-facts table — **not re-run by this agent**, no fresh timing artifact exists on disk to check against |
| VRAM resident / after unload | 2479 MiB / 367 MiB | `docs/TASKS.md` — **not re-run by this agent** |
| int8 on this stack | loads and runs without crashing, refused anyway by deliberate policy | `docs/decisions/0002-int8-refused-on-blackwell.md` |

The mic RMS above (0.0663) differs from the 0.048 recorded in `docs/TASKS.md`'s table — the device was re-probed since that entry was written and the winner's measured RMS moved, which is expected (RMS depends on ambient noise/mic gain at probe time, not a fixed constant); it's flagged here so the two numbers don't quietly disagree without explanation.

## What hasn't been demonstrated

- **No human has held the hotkey and heard NOVA answer on this machine yet**, as far as this agent can find: `docs/ARCHITECTURE.md` requires every milestone to close with a hardware demo recorded in `docs/EVIDENCE.md` with the exact command that produced it — that file does not exist in this repo yet. Until it does, M0's own exit bar ("Manav holds the hotkey, says 'open notepad', Notepad opens, NOVA says so out loud") is unmet even though the code path exists.
- **The turn loop (`nova/app.py`) has no dedicated test file.** It's exercised incidentally by `tests/test_source_hygiene.py` (no hardcoded paths, no `os.environ` reads) but nothing runs `NovaApp.run_turn()` against a fake or real turn.
- ~~**A currently-broken test file**~~ — **resolved.** `tests/test_skills_time.py` briefly constructed `SkillCtx(config=Config())` with no `ladder=`, which `SkillCtx` requires by design so a skill invocation can never silently skip authorization. nova-tester fixed it (`ladder=_UnusedLadder()`, lines 28/38/51) between this section being written and being committed. Left on the record rather than deleted, because the finding was correct when made and the required-field design is what surfaced it loudly instead of silently.

- **This agent has no shell access this session** (Read/Write/Edit/Glob/Grep only, no Bash tool) and could not run `pytest`, `scripts/probe_*.py`, or `python -m nova` itself. Every claim above that would normally come from running something instead comes from reading source, reading `data/*.json`/`data/*.wav` artifacts left by prior runs, and reading `.pytest_cache/v/cache/{nodeids,lastfailed}` from the most recent recorded pytest run. **Lead-verified at commit time: 367 passed, 0 failed, 3 skipped**, re-run on this machine. See `QUICKSTART.md` for exactly which steps that document's author executed versus only read.

## Decisions on record

Short ADRs for choices the team made deliberately, under `docs/decisions/`:
- [`0001-no-local-llm-in-m0.md`](docs/decisions/0001-no-local-llm-in-m0.md) — rules-first routing now, a local LLM slotted in later behind an interface seam, not built yet.
- [`0002-int8-refused-on-blackwell.md`](docs/decisions/0002-int8-refused-on-blackwell.md) — `int8`/`int8_bfloat16` are refused on this GPU's compute capability even though they were measured to work, because the failure mode if a driver update reintroduces the crash is a silent mid-conversation death.
- [`0003-ast-test-covers-process-spawn-not-just-sockets.md`](docs/decisions/0003-ast-test-covers-process-spawn-not-just-sockets.md) — the no-network promise is enforced against `subprocess`/`os.startfile`/etc., not just transport imports, after the retired build's version of this test proved blind to exactly that hole.

## What's next

M1 adds a wake word (`hey_jarvis` as a stand-in — model files for it already sit in `models/wake/`, but no `nova/audio/wake.py` exists yet), VAD endpointing, a tray icon, and a working CONFIRM UI. See `docs/ROADMAP.md` for the full milestone list and `docs/TASKS.md` for the live task board, findings register, and measured-facts table this README's numbers are drawn from.

## Getting started

See [`QUICKSTART.md`](QUICKSTART.md) for setup and running NOVA on this machine, and [`docs/ADDING_A_SKILL.md`](docs/ADDING_A_SKILL.md) for how to add a new capability.
