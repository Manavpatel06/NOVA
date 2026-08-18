# QUICKSTART

How to set up and run NOVA on this machine (Manav's Windows 11 laptop, `D:\NOVA`, interpreter `D:\NOVA\.venv\Scripts\python.exe`, Python 3.13).

**Provenance note, read this first:** this document was written by an agent with no shell access this session (Read/Write/Edit/Glob/Grep only — no Bash tool was available). Every step below was checked by reading the script it runs and, where one exists, an artifact a prior run left on disk (`data/*.json`, `data/*.wav`, `.pytest_cache/`). **None of the commands in this document were executed by the agent that wrote it.** Where a step's *expected output* is shown, it's transcribed from a prior recorded run, not from this session — run it yourself and compare. If your output differs from what's shown here, trust your run, not this doc, and say so when you report back.

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
Records ~1s from every input-capable device on the system, prints a ranked table (host API, sample rate, RMS, verified/rejected), and plays a short audible tone through the default output device as a playback smoke test — you should hear it. **Not run this session**, but a prior run's result is cached at `data/audio_profile.json`: on this machine the winner is device 22 ("Microphone Array (Realtek HD Audio Mic Array input)", WDM-KS, RMS 0.0663), and device 17 (same physical hardware, different endpoint) is correctly rejected as returning exact digital zeros. If your run picks a different device or shows `verified: false`, that's a real finding — run with `--force` to ignore the cache and re-probe, and check `docs/CONTRACTS.md`'s "lying-endpoint trap" section for why some endpoints open but return silence.

**If this fails:** no input device at all means `Capture.start()` will raise `RuntimeError: Capture: no usable input device`. Check Windows' own sound settings (Settings → System → Sound) confirm a microphone is enabled and not muted at the OS level first — the prober can only see what Windows exposes.

## 3. Probe the GPU / STT path

```
D:\NOVA\.venv\Scripts\python.exe scripts\probe_gpu.py
```
Adds the CUDA DLL directories (`os.add_dll_directory`, required before `ctranslate2` is ever imported — Windows does not search `PATH` for extension-module DLLs), probes for a genuinely working compute type by loading a real model and running a real transcription (never trusting CTranslate2's advertised capability list), then loads `large-v3-turbo` and transcribes a synthetic tone end to end, printing load/transcribe latency. **Not run this session**; the cached result at `data/gpu_profile.json` (read directly) shows `device: cuda, compute_type: float16, verified: true` on this GPU (driver 610.62, ct2 4.8.1) — note the cache records `verified_model: tiny.en`, meaning the *cached* verification ran against the small probe model, not `large-v3-turbo`; a fresh `--force` run is what actually re-verifies against the real model NOVA uses.

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
No real human-speech fixture exists in this repo, so this feeds Kokoro's own synthesized speech back into Whisper — not a substitute for testing against a real microphone recording, but it does prove Whisper returns real words (not a stray period on silence) on this machine. **Not run this session.**

## 6. Run the test suite

```
D:\NOVA\.venv\Scripts\python.exe -m pytest tests/ -v
```
**Not run this session — no shell tool available to this agent.** What's known instead, from reading `.pytest_cache/v/cache/nodeids` (the most recent recorded full collection) and `.pytest_cache/v/cache/lastfailed`, plus reading the current source directly:
- 365 tests were collected across all 18 files in `tests/` as of the last recorded run.
- `.pytest_cache`'s `lastfailed` records 17 failures from that run, all in `tests/test_skills_open_app.py` (15) and `tests/test_skills_time.py` (2).
- Reading current source: `tests/test_skills_open_app.py` has since been fixed — it now passes a placeholder `ladder=_UnusedLadder()` into every `SkillCtx` it builds. `tests/test_skills_time.py` has **not** been fixed as of this writing: both its tests construct `SkillCtx(config=Config())` with no `ladder=`, and `nova/skills/registry.py`'s `SkillCtx` has no default for `ladder` (deliberately — see its docstring). Expect those two tests to fail with `TypeError: __init__() missing 1 required positional argument: 'ladder'` until that's fixed.
- Some tests are explicitly hardware-dependent (e.g. `test_real_hardware_probe_produces_a_verified_profile`, `test_real_transcriber_loads_and_transcribes_real_audio`) — they exercise the real mic/GPU, not a mock, and will take real wall-clock time (model load + inference) rather than running instantly.

Run it yourself and compare against the above — if the count or the failure set differs, that's real signal, not a doc error to paper over.

## 7. Run NOVA

```
D:\NOVA\.venv\Scripts\python.exe -m nova
```
Boots `NovaApp` (`nova/app.py`): constructs the security kernel (`AuditLog`, `Ladder`), the router, and arms the `Ctrl+Alt+Space` hotkey. Construction loads no models — `Transcriber`/`Synthesizer` are lazy and only load on the first real turn. **Not run this session.**

To use it: **hold `Ctrl+Alt+Space`**, speak, **release**. That triggers `ptt.start`/`ptt.end` on the event bus, which starts/stops `Capture`, runs the captured audio through `Transcriber.transcribe()`, `Router.route()`, the matched skill through `SkillSpec.invoke()` (which is where the permission ladder and the audit log both fire), and speaks the reply through `Synthesizer`/`Player`. A quick tap instead of a hold latches the mic open until the next tap or a 30s timeout — see `nova/interfaces/hotkey.py`'s module docstring for the two modes.

Say "what time is it" (routes to the `time` skill, READ tier) or "open notepad" (routes to `open_app`, ACT tier, launches from a closed allowlist in `config/nova.yaml`'s `security.allowed_apps`).

**If it doesn't respond to the hotkey:** check the console/`data/nova.log` for a WARNING naming why — `Hotkey.start()` degrades loudly (never silently) if the `keyboard` package's global hook can't be installed, which on Windows usually means it's not running as the interactive desktop user, or the hook is blocked by policy.

**If it hears you but says nothing back:** `Synthesizer` falls back to SAPI automatically; total silence means *both* engines failed, which is logged as an ERROR naming both.

**No hardware demo exists yet.** `docs/ARCHITECTURE.md` requires this exact loop to be recorded in `docs/EVIDENCE.md` before a milestone is considered closed — that file doesn't exist in this repo yet, so step 7 above is the first time anyone should actually try it end to end and record what happens.

## Troubleshooting index

| Symptom | Run this diagnostic |
|---|---|
| No response to hotkey, or capture never starts | `scripts/probe_audio.py` |
| Transcription errors, CUDA DLL errors, wrong device (CPU when GPU expected) | `scripts/probe_gpu.py` |
| NOVA never speaks, or speech sounds wrong | `scripts/probe_tts.py` |
| Speech comes out but transcribed text looks wrong | `scripts/probe_roundtrip.py` |
| Config won't load, or a typo in `nova.yaml` isn't caught | `scripts/verify_core.py` |
