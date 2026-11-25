# 0023 — NOVA lives on the hub

Date: 2026-11-25. Manav: "I meant everything on the hub. So even the Nova app will be loaded in the hub. So I will
talk over there... so my current laptop can stay free of memory." He wants to talk to it through all four:

- a phone web page
- a real phone number
- the hub's own mic and speaker
- his laptop's mic, with the hub doing the work

He also wants "full control over all my devices".

## Phases
1. **NOVA on the hub, talked to from the phone (this change).**
2. A small laptop helper (no models): the hub controls the laptop through it, and the laptop mic passes audio
   to the hub. The laptop's NOVA app is then retired.
3. The hub's own mic and speaker.
4. A real phone number (Twilio or Plivo; Manav creates the account; about $1 a month plus per-minute).

## Phase 1: what runs on the hub
- **`nova/hub/assistant.py` — HubAssistant.** The laptop app's thinking, without the laptop.
  - The instant lane runs first, then the Claude conversation (MaxBrain with the hub's own `claude`).
  - Every action goes through `SkillSpec.invoke()`: the same ladder, audit and spoken approval
    (VoiceConfirmChannel).
  - Only `HUB_SKILLS` are offered: time, date, weather, capabilities, memory, reminders and timers, and
    building/discarding upgrades. A laptop skill gets "That needs your laptop", never a silent no-op.
  - Turns are one at a time and journaled like the laptop's.
- **Channels.** An approval question is asked on the channel the sentence came from, and the next thing said
  there is the answer. "Nova, confirm" is still the only approval for risky actions. Tested: yes, no, silence
  and "Nova, confirm and also …" all decline, and the action never runs.
- **`nova/hub/talk.py` + `talk.html` — the talk page** at `https://nova-hub.<tailnet>.ts.net/`.
  - Tap, talk, tap again, or type. The page sends 16 kHz PCM and gets back the words plus a WAV.
  - It is owner-only like every route.
  - It runs only its own nonce'd script under a strict CSP (`default-src 'none'`, `connect-src 'self'`,
    `frame-ancestors 'none'`).
  - It can be added to the iPhone home screen.
- **`nova/hub/voice.py` — HubVoice.**
  - Whisper `hub.stt_model` (small.en, int8, CPU) and Kokoro, read from `~/NOVA/models` only.
  - `nova-hub voice` installs the libraries (same versions as the laptop) and downloads the models once.
    The Kokoro files are checked against the laptop's SHA-256 sums. Audio is never written to disk.
- **In-process calls.** Skills that call the hub (reminders, upgrades) reach `HubService.call()` directly
  (`hub_tier.use_local_service`), through the same route table the listener serves (`HubService.ROUTES`, one
  source of truth).
- **Memory.** The first time it starts, NOVA's memory on the hub begins as the laptop's mirrored copy:
  regular files only, realpath-checked on both sides.
- **Egress.** The hub allowlist adds Open-Meteo (weather) to Bark. Nothing else.
- **The local model lane is off by default** (`hub.local_tier: false`): measured at 8–16 s per sentence on the
  hub's CPU, which is slower than Claude. The hub unloads it at start to give the memory to the voice.

## Also in this change: cross-site requests refused
Tailscale identifies the *device*. So any website open in a browser on one of Manav's devices could send
requests to the hub. It couldn't read the replies, but it could start a build or add a reminder. The listener
now:
- refuses any request a browser marks as coming from another site (`Sec-Fetch-Site`, `Origin`);
- requires JSON bodies, which browsers can't send cross-site without a CORS preflight that the hub never
  grants.

## Measured (cloud container, 2 cores; the hub has 4)
- Whisper small.en loads in about 2.5 s; Kokoro in about 1.5 s; together they use about 940 MB.
- Hearing a 1–2 s sentence takes about 2.5–3 s; speaking a short reply takes about 1.7 s.
- A Kokoro sentence was transcribed back correctly: "Remind me in 10 minutes to call my sister."
- End to end through the real route:
  - "what time is it" (typed): 1.4 s
  - a spoken question: 6.6 s
  - a Claude poem: 10.2 s, mostly speaking a long reply

## Next
- Speak long replies sentence by sentence, so the first words start sooner.
- Phase 2: the laptop helper, and applying upgrades from the hub (the hub fast-forwards itself and
  restarts).
