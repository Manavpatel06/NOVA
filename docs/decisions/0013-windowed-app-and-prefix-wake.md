# ADR 0013 — NOVA is a windowed app that answers to "Nova, …"

**Date:** 2026-10-14 · **Decided by:** Manav · **Implemented by:** Cowork session (Claude), reviewed by an independent test agent and security agent

## Decision

1. **Windowed app.** Opening NOVA shows its window and starts listening;
   closing the window quits NOVA and releases the microphone. No auto-start
   at login, no hidden background mode (`ui.window: true`). If the window
   cannot open or dies on its own, NOVA shuts down rather than keep a live
   mic with nothing on screen.
2. **Prefix wake** (`wake.mode: prefix`). While the window is open, each
   utterance (Silero VAD segmentation) is transcribed locally and acted on
   only if the sentence **starts** with NOVA's name ("Nova, open Spotify",
   "Hey Nova, …"). Everything else is dropped: never logged, displayed,
   stored, or sent anywhere. A bare "Nova" gets "Yes?" and one follow-up
   sentence within 8 s needs no name; noise cannot extend that window.
3. **Voice may act.** `ArmedBy.VOICE` (and `TYPED`, for the window's text
   box) have no tier ceiling. CONFIRM-tier skills and every action Claude
   proposes still require a human click. The ceiling map is now an explicit
   allow-list: any future arming source defaults to READ.
4. **The "hey jarvis" detector stays** as `wake.mode: model` (READ-only).
   An unrecognised `wake.mode` fails closed to `model`.

## Why prefix wake instead of training a "Hey Nova" detector

It matches Manav's words exactly ("it will just do the work when I start the
sentence with Nova"), needs no model training, and handles "Nova, open
Spotify" in one breath. Cost: Whisper runs on every utterance while the
window is open (GPU, ~0.3–0.6 s each); acceptable because listening only
happens while he has the window open. A trained wake model can replace the
gate later without changing anything else.

## Also fixed in this change

- `stt.language` was never passed to Whisper (a near-silent clip was
  transcribed as Spanish, "¡Gracias!"). Now passed, plus a guard that drops
  Whisper's silence hallucinations (`no_speech_prob > 0.6` and
  `avg_logprob < -1.0`).
- Duplex hangover 200 → 500 ms so NOVA doesn't hear the tail of its own voice.
- Two tests broken by T2.4's required `SkillSpec.description`, and two M0
  tests that spawned the real `claude` CLI, were updated.
