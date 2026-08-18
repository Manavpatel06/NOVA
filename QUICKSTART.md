# QUICKSTART

How to set up and run NOVA on this machine (Manav's Windows 11 laptop, `D:\NOVA`, interpreter `D:\NOVA\.venv\Scripts\python.exe`, Python 3.13).

**Provenance note, read this first:** this document was written by an agent with no shell access this session (Read/Write/Edit/Glob/Grep only — no Bash tool was available). Every step below was checked by reading the script it runs and, where one exists, an artifact a prior run left on disk (`data/*.json`, `data/*.wav`, `.pytest_cache/`), or from a result the team lead ran live and reported (marked **lead-verified, 2026-08-17** below — attributed explicitly, never presented as this agent's own execution). Where a step's *expected output* is shown and isn't lead-verified, it's transcribed from an artifact a prior run left on disk, not from anyone running it this session — run it yourself and compare. If your output differs from what's shown here, trust your run, not this doc, and say so when you report back.

## 0. Prerequisites (already true on this machine)

- Windows 11, Python 3.13, everything under `D:\NOVA` — confirmed by `nova/__init__.py`'s `ROOT` constant and every path in `config/nova.yaml`.
- A CUDA-capable GPU is optional but this machine has one (RTX 5060 Laptop GPU) and NOVA is verified to use it; without one, `nova/stt/gpu.py` falls back to CPU and logs a WARNING (`large-v3-turbo` uses ~1.9 GB RAM on CPU).
- `D:\NOVA\.venv\` already exists on this machine with every dependency in `requirements.txt` / `pyproject.toml` installed — confirmed directly by finding `faster_whisper`, `kokoro_onnx`, and `keyboard` under `.venv\Lib\site-packages\`. If you're setting this up somewhere the venv doesn't already exist:
  ```
  cd D:\NOVA
  py -3.13 -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  ```
- **`models/`, `cache/`, and `data/` are gitignored** (see `.gitignore`) and are *not* fetched by any code path — `docs/CONTRACTS.md` forbids downloading model weights at import time. On this machine they're already populated (Kokoro ONNX weights, Silero VAD, the `hey_jarvis` wake-word stand-in, and both `large-v3-turbo` and `tiny.en` in the local HF cache — confirmed by listing `models/` and `cache/hf/hub/` directly). If you're on a different machine and these are missing, you need to copy them from wherever they were salvaged/backed up — there is no script that downloads them, by design.

## 1. Sanity-check config and the core layer

```
D:\NOVA\.venv\Scripts\python.exe scripts\verify_core.py
```
Loads `config/nova.yaml`, asserts a couple of known-good values, proves a malformed/unknown-key config is a loud `ConfigError` rather than a silent partial load, and exercises the event bus (a failing subscriber must not break the others). Prints `ALL CHECKS PASSED` at the end if everything holds. **Not run this session** — read in full instead; see `scripts/verify_core.py` for exactly what it checks.

## 2. Probe the microphone

```
D:\NOVA\.venv\Scripts\python.exe scripts\probe_audio.py
```
Records ~1s from every input-capable device on the system, prints a ranked table (host API, sample rate, RMS, verified/rejected), and plays a short audible tone through the default output device as a playback smoke test — you should hear it, though audibility hasn't actually been confirmed by a human yet (see README's "what hasn't been demonstrated"). **Not run this session by this agent**; the winner is consistently device 22 ("Microphone Array (Realtek HD Audio Mic Array input)", WDM-KS) across every recorded probe, including a lead-verified run (RMS 0.048) and two reads of the cache mid-session by this agent (RMS 0.0663, then 0.0557 minutes later — the device was re-probed again in between by someone else's run). **The RMS number itself is not stable between runs** — it depends on ambient noise at probe time — so don't treat any single figure as canonical; what's stable is which device wins and that device 17 (same physical hardware, different WDM-KS endpoint) is correctly rejected as returning exact digital zeros on every trial recorded so far. If your run picks a different *device*, that's a real finding — run with `--force` to ignore the cache and re-probe, and check `docs/CONTRACTS.md`'s "lying-endpoint trap" section for why some endpoints open but return silence.

**If this fails:** no input device at all means `Capture.start()` will raise `RuntimeError: Capture: no usable input device`. Check Windows' own sound settings (Settings → System → Sound) confirm a microphone is enabled and not muted at the OS level first — the prober can only see what Windows exposes.

## 3. Probe the GPU / STT path

```
D:\NOVA\.venv\Scripts\python.exe scripts\probe_gpu.py
```
Adds the CUDA DLL directories (`os.add_dll_directory`, required before `ctranslate2` is ever imported — Windows does not search `PATH` for extension-module DLLs), probes for a genuinely working compute type by loading a real model and running a real transcription (never trusting CTranslate2's advertised capability list), then loads `large-v3-turbo` and transcribes a synthetic tone end to end, printing load/transcribe latency. **Lead-verified:** `device=cuda, compute_type=float16, verified=True` on this GPU (RTX 5060 Laptop GPU, Blackwell sm_120, driver 610.62, ct2 4.8.1); VRAM 2479 MiB resident, 367 MiB after `unload()`. **Latency: quote as a range, not a point figure** — `large-v3-turbo` load 1.68–1.80s, transcribe 0.48–0.69s for 3.0s of audio, per nova-accuracy's re-measurement across multiple runs (a single-run figure of 1.48s/0.44s was reported earlier and was too optimistic; corrected here and in `docs/TASKS.md`/`docs/EVIDENCE.md`). Note the cache at `data/gpu_profile.json` still records `verified_model: tiny.en` for the *cached* candidate-selection pass — the probe's compute-type search always verifies against the small model for speed; it's the separate `transcription_smoke_test()` step (also in this script) that actually exercises `large-v3-turbo`.

**If this fails:** a `RuntimeError: Library cublas64_12.dll is not found` means something imported `ctranslate2`/`faster_whisper` before `nova.stt`'s package-level `ensure_dll_directories()` call ran — this should not happen through normal imports (`nova/stt/__init__.py` calls it first thing), but if you're importing `ctranslate2` directly in a script, call `nova.stt._dll.ensure_dll_directories()` yourself first.

## 4. Probe text-to-speech

```
D:\NOVA\.venv\Scripts\python.exe scripts\probe_tts.py
```
Loads the Synthesizer (Kokoro ONNX, falling back to Windows SAPI if Kokoro fails), synthesizes two phrases, and writes each to a WAV file under `data/` so you can listen. **Not run this session**; `data/tts_probe_0_kokoro.wav`, `data/tts_probe_1_kokoro.wav`, `data/tts_probe_0_sapi.wav`, and `data/tts_probe_1_sapi.wav` all exist from prior runs — the presence of both `_kokoro` and `_sapi` files means both the primary engine and the fallback have been exercised and produced non-silent output at some point, though this agent did not listen to them to judge quality (the script itself says it can't judge that either — that needs a human with ears).

**If this fails silently (no error, but a near-silent WAV):** `synth.say()` prints an RMS per phrase and warns if it's below `1e-4` — check that line in the script's output before assuming it worked.

## 5. Probe the full TTS → STT round trip

```
D:\NOVA\.venv\Scripts\python.exe scripts\probe_roundtrip.py
```
No real human-speech fixture exists in this repo, so this feeds Kokoro's own synthesized speech back into Whisper — not a substitute for testing against a real microphone recording, but it does prove Whisper returns real words (not a stray period on silence) on this machine. **Lead-verified, 2026-08-17:** `'what time is it'` → `'What time is it?'` and `'open notepad'` → `'Open Notepad.'`, both exact matches (case/punctuation differences from Whisper's own formatting aside). This is the strongest evidence so far that STT and TTS agree with each other on this machine — it is still synthetic speech, not a human voice, so it does not prove NOVA will understand Manav.

## 6. Run the test suite

```
D:\NOVA\.venv\Scripts\python.exe -m pytest tests/ -v
```
**Not run this session by this agent — no shell tool available.** Two lead-verified reports exist for this, and they disagree on the passed count: **"365 passed, 3 skipped, 0 failed"** in one message, **"367 passed, 0 failed, 3 skipped"** in another (both dated 2026-08-17, both described as a real run on this machine). Both agree the suite is fully green (0 failed) and 3 tests skip. This agent cannot run the suite itself to break the tie between 365 and 367 — flagging the discrepancy rather than picking one silently; whoever reconciles it should also correct `.pytest_cache/v/cache/nodeids`'s recorded collection count (365 entries as of the last snapshot this agent read) if the true total changed.

History, for context: the last recorded `lastfailed` (before the suite went green) listed 17 failures, all in `tests/test_skills_open_app.py` (15) and `tests/test_skills_time.py` (2) — both from `SkillCtx(...)` calls missing the now-required `ladder=` argument. Both files have since been fixed (`ladder=_UnusedLadder()`, a placeholder that raises if actually called, since these tests invoke skill handlers directly rather than through `SkillSpec.invoke()`).

Some tests are explicitly hardware-dependent (e.g. `test_real_hardware_probe_produces_a_verified_profile`, `test_real_transcriber_loads_and_transcribes_real_audio`) — they exercise the real mic/GPU, not a mock, and will take real wall-clock time (model load + inference) rather than running instantly.

## 6b. Run the turn loop without a human (agent-run evidence, already on record)

```
D:\NOVA\.venv\Scripts\python.exe scripts\probe_turn.py
```
Exercises `NovaApp.run_turn()` directly — the exact method the real `ptt.start`/`ptt.end` handlers call — with Kokoro-synthesized speech standing in for a live mic recording (the script's own docstring says so plainly). **Lead-verified, recorded in `docs/EVIDENCE.md`:** "what time is it" answered correctly; "open notepad" genuinely launched Notepad (pid-verified, then closed); "what's the weather today" scored 0.136, below the confidence floor, and correctly produced a spoken clarification with zero skills invoked and zero audit records. Audit chain after the run: `ChainStatus(ok=True, entries=4)`. Also reports idle vs. mid-turn RSS/VRAM — see `docs/decisions/0006-open-question-memory-does-not-return-to-baseline.md` for why the post-`app.stop()` numbers are a live open question, not a clean result.

This proves the wired pipeline runs correctly end to end on real hardware. It does **not** prove the hotkey-to-live-mic path with an actual human voice — that's step 7.

## 7. Run NOVA (the step that still needs a human)

```
D:\NOVA\.venv\Scripts\python.exe -m nova
```
Boots `NovaApp` (`nova/app.py`): constructs the security kernel (`AuditLog`, `Ladder`), the router, and arms the `Ctrl+Alt+Space` hotkey. Construction loads no models — `Transcriber`/`Synthesizer` are lazy and only load on the first real turn. **Not run this session by this agent.** The code path itself is proven by step 6b above; what's missing is specifically the three things `docs/EVIDENCE.md` lists as requiring a human and unable to be closed by any agent: audible speakers, a real human voice, and the elevated/UAC hotkey question. This step is where those get answered.

To use it: **hold `Ctrl+Alt+Space`**, speak, **release**. That triggers `ptt.start`/`ptt.end` on the event bus, which starts/stops `Capture`, runs the captured audio through `Transcriber.transcribe()`, `Router.route()`, the matched skill through `SkillSpec.invoke()` (which is where the permission ladder and the audit log both fire), and speaks the reply through `Synthesizer`/`Player`. A quick tap instead of a hold latches the mic open until the next tap or a 30s timeout — see `nova/interfaces/hotkey.py`'s module docstring for the two modes.

Say "what time is it" (routes to the `time` skill, READ tier) or "open notepad" (routes to `open_app`, ACT tier, launches from a closed allowlist in `config/nova.yaml`'s `security.allowed_apps`).

**If it doesn't respond to the hotkey:** check the console/`data/nova.log` for a WARNING naming why — `Hotkey.start()` degrades loudly (never silently) if the `keyboard` package's global hook can't be installed, which on Windows usually means it's not running as the interactive desktop user, or the hook is blocked by policy.

**If it hears you but says nothing back:** `Synthesizer` falls back to SAPI automatically; total silence means *both* engines failed, which is logged as an ERROR naming both.

**If it doesn't respond and you're in an elevated (UAC / "Run as administrator") window:** this is a known open question, not a known-working case — the global hotkey's coverage of elevated-window focus has not been tested by anyone yet. If you hit this, that's a real, reportable finding, not user error.

**No hardware demo exists yet.** `docs/ARCHITECTURE.md` requires this exact loop to be recorded in `docs/EVIDENCE.md` before a milestone is considered closed — that file doesn't exist in this repo yet, so step 7 above is the first time anyone should actually try it end to end and record what happens.

## Troubleshooting index

| Symptom | Run this diagnostic |
|---|---|
| No response to hotkey, or capture never starts | `scripts/probe_audio.py` |
| Transcription errors, CUDA DLL errors, wrong device (CPU when GPU expected) | `scripts/probe_gpu.py` |
| NOVA never speaks, or speech sounds wrong | `scripts/probe_tts.py` |
| Speech comes out but transcribed text looks wrong | `scripts/probe_roundtrip.py` |
| Config won't load, or a typo in `nova.yaml` isn't caught | `scripts/verify_core.py` |
