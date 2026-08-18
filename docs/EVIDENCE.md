# NOVA — Hardware Evidence

`docs/ARCHITECTURE.md` requires every milestone to close with a demonstration on
this laptop, recorded here with the command that produced it. This file exists so
that "it works" is always traceable to something someone actually ran.

**The rule: nothing goes in this file that was not observed. Anything awaiting a
human stays in the pending section until a human does it.**

---

## M0 — walking skeleton

### Verified by an agent, on this machine

Command: `.venv/Scripts/python.exe scripts/probe_turn.py` (re-run by the lead)

Kokoro-synthesized speech stands in for a live microphone recording, which the
script states in its own docstring rather than implying a human spoke. This
proves the pipeline, not the human interface.

| Turn | Result |
|---|---|
| "what time is it" | `"It's 10:35 PM."` — correct |
| "open notepad" | `"Opening notepad."` — **Notepad genuinely launched**, pid 5516, verified by a psutil pid-diff, then closed |
| "what's the weather today" | confidence 0.136, below floor → spoken clarification, **zero skills invoked, zero audit records** |

Audit chain after the run: `ChainStatus(ok=True, entries=4, broken_at=None)`, with
real hash-chained records naming skill, tier and decision, e.g.
`ladder.authorize | open_app | act | allow | {'app': 'notepad'}`.

Command: `.venv/Scripts/python.exe scripts/probe_gpu.py` (re-run cold by the lead
after deleting `data/gpu_profile.json`)

```
device=cuda  compute_type=float16  verified=True
NVIDIA GeForce RTX 5060 Laptop GPU, driver 610.62, ct2 4.8.1
load 1.68-1.80s | transcribe 0.48-0.69s for 3.0s audio
(the first single run reported 1.48s/0.44s; nova-accuracy re-measured across
5 runs and those were ~20% and ~55% optimistic. Ranges, not points -- these
fluctuate run to run.)
```

Command: `.venv/Scripts/python.exe scripts/probe_roundtrip.py`

```
'what time is it' -> 'What time is it?'    exact
'open notepad'    -> 'Open Notepad.'       exact
```

Command: `.venv/Scripts/python.exe -m pytest -q` → **495 passed, 0 failed, 4 skipped**
(at commit `a8cc40e`. A passed-count is a timestamp, not a property — it rises as
coverage lands. The durable facts are 0 failed and a skip count you can account
for. Quote a count only with the commit it was measured at.)

Memory, measured across a real 3-turn run (see finding F16):

```
                              RSS          VRAM
start                       59.4 MB      264 MiB
after NovaApp()             64.8 MB      264 MiB   lazy loading confirmed
after 3 turns             1254.6 MB     2479 MiB
after app.stop()           912.9 MB      367 MiB   host RSS does NOT return
```

Warm turn latency ~2.3 s wake-to-voice, including audio playing to completion.
First turn ~5.1 s with the one-time model load folded in.

### Pending — requires a human, and cannot be closed without one

M0's exit bar in `docs/ROADMAP.md` is *"Manav holds the hotkey, says 'open
notepad', Notepad opens, NOVA says so out loud."* **One of the three below is now
closed; two remain.** Until they are observed, that bar is **unmet**, no matter
how much of the code path is proven.

- [x] **Speakers are audible — CLOSED 2026-08-18 by Manav.** He confirmed hearing
      NOVA's own voice during the 2026-08-17 development session, produced by
      `scripts/probe_tts.py` / `probe_turn.py` / `probe_roundtrip.py` playing real
      Kokoro audio through `Player` on this laptop. This is *this* build's
      playback path, not the retired build's — asked and answered explicitly,
      because the retired build also had Kokoro and hearing it would have proved
      nothing about this one.
      Caveat kept deliberately: this predates the F18 output-device fix
      (`df37f5e`), so it proves the laptop's speakers work and that Kokoro
      reaches them — not that the new prober picks the right endpoint every time.
      `scripts/probe_output.py` plays a tone through the current path if that
      ever needs re-confirming.
- [ ] **A real human voice transcribes correctly.** All STT evidence to date is
      Kokoro's synthetic speech, which is real speech but not a human in a room
      with ambient noise.
- [ ] **The hotkey fires while an elevated (UAC) window has focus.** Unknown.
      `WH_KEYBOARD_LL` behaviour across the UIPI boundary was deliberately not
      asserted from memory. Run Task Manager as administrator and press
      `Ctrl+Alt+Space`. If it fails, that becomes a documented limitation.

**Before running the human demo — run `scripts/probe_output.py` (F18 is now fixed).**
`Player` now probes rather than trusting the OS default, and prefers a local
sound card over display/HDMI audio. `scripts/probe_output.py` lists every
endpoint, says which NOVA will use and why, and plays a test tone. If you hear
the tone, playback is good. Lead-verified: it selects `Headphones (Realtek(R)
Audio)`, index 4, opened and accepted a buffer.

Audibility itself still needs your ears — no probe can close that.

### Remote control changes what the demo proves

Measured 2026-08-18: NOVA runs in the **physical console session**
(`SESSIONNAME=Console`, no RDP/TeamViewer/AnyDesk processes present). Its audio
devices are the laptop's own — so NOVA speaks into the room containing the
laptop and listens to that same room, wherever the human happens to be.

Consequences for anyone demoing over a remote-control tool:

- **Hearing NOVA remotely usually works** — most remote tools stream host audio
  to the client.
- **Speaking to NOVA remotely usually does not** — few forward the client's
  microphone to the host. The wake word and push-to-talk both capture the
  laptop's mic, in the laptop's room.

So a remote session can close the *speakers are audible* item but generally
cannot close *a real human voice transcribes correctly*. That one needs someone
in front of the machine, or a deliberate substitute (feeding recorded human
speech through `Transcriber` rather than through `Capture`).

Command for the human demo: `.venv\Scripts\python.exe -m nova`

### Measurement caveat, learned the hard way

`nvidia-smi` reports **machine-wide** VRAM, not per-process. The 264 MiB
"baseline" is Windows' own desktop with no NOVA running at all. Any VRAM figure
taken while another GPU process is resident is contaminated — which is exactly
how this file came to claim 566 MiB retained when the reproducible number is
367 MiB. Take VRAM readings on a quiet machine, or read per-process usage.

Host RSS via `psutil` does not have this problem: it is genuinely per-process.
