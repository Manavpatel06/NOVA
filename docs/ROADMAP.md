# NOVA — Milestone Roadmap

Every milestone ends the same way: tests green → security review → accuracy
re-verification → **hardware demo on this laptop** → docs → commit. No milestone
closes on test results alone.

---

## M0 — Walking skeleton (hardware truth)

One thin thread, end to end, on real hardware:

> hold `Ctrl+Alt+Space` → speak → Whisper on the GPU → one real action → Kokoro says the answer aloud

- repo skeleton, config loader, event bus, logging, pytest harness
- **mic device prober** — enumerate hosts, record 1 s from each candidate, reject
  any endpoint returning digital zeros (WASAPI lies; expect WDM-KS to win), write
  the winner to `data/audio_profile.json`
- **GPU prober** — `os.add_dll_directory` on every `nvidia/*/bin` *before*
  importing ctranslate2; then actually transcribe with each compute type in order
  and keep the first that returns. `verified=True` only after a real inference
- STT: faster-whisper `large-v3-turbo`, cuda/float16
- TTS: Kokoro ONNX, audible through the real speakers
- two skills: `time` (READ) and `open_app` (ACT)
- security kernel v0: tier enum, audit log, egress guard + AST no-socket test

**Exit demo:** Manav holds the hotkey, says "open notepad", Notepad opens, NOVA
says so out loud. Latency measured wake-to-voice.

## M1 — Always-on

Status as of 2026-08-18. Most of M1 is built and tested; it is **not** shippable,
for one specific reason recorded below.

| Item | State |
|---|---|
| Wake word (`hey_jarvis` stand-in) | **LIVE** — wired into the turn loop 2026-08-20 (`6d62171`). NOVA answers to its name. **Built, tested** (40 tests). Vendored rather than depending on `openwakeword`, whose unconditional `requests` import would have made the no-socket guarantee unenforceable (ADR 0007). Measured 0.99224 TP / 0.00004 control, independently re-verified. Was deliberately held back until both preconditions existed; they do |
| VAD endpointing, pre-roll ring, arm timeout | **Built, tested.** 1000 ms pre-roll derived from the classifier's ~760 ms receptive field, not a round number |
| Half-duplex ducking | **BUILT** (T1.7/F26) — was the gate on wiring the wake word. Finding F26; contract written as T1.7. No coordination exists between `Player` and `WakeWord` at all |
| Full-duplex AEC | **Explicitly descoped**, as the roadmap's own "no silent aspiration" clause demands. Neither backend has a reliable Windows wheel; half-duplex is sufficient and is stated as such rather than aspired to |
| Tray icon, hotkey, single-instance guard | **Built, tested** (33 tests). Guard verified to work independently of the tray icon existing |
| Governor with **measured** RSS/VRAM | **Built, tested** (17 tests), measurements published — and they contradicted the budget I had invented, so `ARCHITECTURE.md` was rewritten from them |
| Permission ladder with a working CONFIRM UI | **Built, tested.** Three distinct denial reasons; the dialog drives a real `Ladder` and produces real audit records. **Never confirmed by a human clicking it** — which is exactly how the retired build's CONFIRM died while shipping two working dialogs |

### Why M1 was held back, and why it no longer is

The wake word is finished and stays unwired until a duplex gate exists. The
retired build hit the self-echo loop — NOVA hears its own reply, the detector
catches its own name, and re-arms — documented it as *"a loop, not a glitch"*,
and shipped a fix. We have none. It is masked today only because push-to-talk
closes the microphone before NOVA speaks, which is a side effect of PTT's shape
rather than a control. Wiring it in first would reproduce a solved bug on purpose.

### Two open decisions for Manav

- **F28** — the ladder has no seam for *how a turn was armed*, so wake-armed
  turns reach ACT tier, and `open_app`, by unchanged code with no failing test.
  Capping acoustic arming at READ costs hands-free convenience.
- **F21** — VRAM leaks ~7.3 MiB per model reload, linearly, reproduced twice by
  independent instruments. Pinning Whisper resident stops it at the cost of
  ~2.4 GB held until restart.

### Exit demo — restated, because the original could no longer mean what it said

The original read: *"NOVA runs all day in the tray, answers to the wake word,
**idles under budget**, and a CONFIRM action visibly asks before acting."*

"Idles under budget" referred to a budget I invented and which measurement
disproved: once Whisper has transcribed once, the process floors at ~740 MB and
CTranslate2's caching allocator will not give it back. The honest exit bar is:

> NOVA runs all day in the tray, answers to the wake word without triggering on
> its own voice or the television, holds a **measured, documented** footprint
> rather than a hoped-for one, and a CONFIRM action visibly asks **a human**,
> who clicks it, and that gets recorded in `docs/EVIDENCE.md`.

## M2 — Control of this machine

- apps, windows, focus, volume, brightness, clipboard, screenshots
- filesystem skills with `realpath` re-validation on both sides at write time
- the **Skill SDK** formalized + `docs/ADDING_A_SKILL.md`, proven by a teammate
  adding an unfamiliar skill in under an hour with no core edits

**Exit demo:** ten system-control commands by voice, back to back.

## M3 — A brain worth talking to

- local LLM, lazy-loaded, for open language and natural replies
- memory: SQLite + FTS5, conversational context, "what did I say about X"
- confidence floor + clarifying questions, tested with deliberately vague input
- the humanizer — replies that sound spoken, not typed

**Exit demo:** a genuine multi-turn conversation with follow-ups and recall.

## M4 — NOVA builds software

- Claude Code arm: headless `claude` job runner, supervised, cancellable
- job lifecycle by voice: start, progress, "it's done", demo it back
- CONFIRM tier throughout; workspace jailed to `D:\NOVA\workspace`

**Exit demo:** "NOVA, build me an app that tracks my water intake" → scaffolded,
tested, running, demoed back — hands never touch the keyboard.

## M5 — Manav's life

- calendar, reminders, notes, documents
- email — **untrusted input**; ships with injection tests that prove hostile
  message bodies trigger zero tool calls, asserting the real path ran
- reach Manav on his phone through the one sanctioned egress module

**Exit demo:** NOVA reads the day ahead, takes a reminder, and pings the phone.

---

## Standing rules for the team

1. No module ships without hardware evidence when hardware is involved.
2. nova-tester writes the tests, never the implementer.
3. nova-accuracy re-runs every number before it is believed.
4. Commit each module as it lands.
5. "It's the environment" gets the highest standard of proof, per the constitution.
