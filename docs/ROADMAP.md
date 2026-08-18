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

- wake word (openWakeWord + `hey_jarvis` stand-in until "hey nova" is trained)
- VAD endpointing, pre-roll ring buffer, arm timeout
- half-duplex ducking + barge-in via hotkey; full-duplex AEC only if a Windows
  wheel actually installs — no silent aspiration
- tray icon, global hotkey, single-instance guard
- governor: lazy load, idle unload, **measured** RSS/VRAM published to docs
- **permission ladder with a working CONFIRM UI, tested end to end**

**Exit demo:** NOVA runs all day in the tray, answers to the wake word, idles
under budget, and a CONFIRM action visibly asks before acting.

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
