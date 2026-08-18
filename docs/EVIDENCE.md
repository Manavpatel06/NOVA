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

Command: `.venv/Scripts/python.exe -m pytest -q` → **367 passed, 0 failed, 3 skipped**

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
notepad', Notepad opens, NOVA says so out loud."* Until the three below are
observed, that bar is **unmet**, no matter how much of the code path is proven.

- [ ] **Speakers are audible.** Playback timing and cancellation are verified;
      that a human can hear it is not. No agent can close this.
- [ ] **A real human voice transcribes correctly.** All STT evidence to date is
      Kokoro's synthetic speech, which is real speech but not a human in a room
      with ambient noise.
- [ ] **The hotkey fires while an elevated (UAC) window has focus.** Unknown.
      `WH_KEYBOARD_LL` behaviour across the UIPI boundary was deliberately not
      asserted from memory. Run Task Manager as administrator and press
      `Ctrl+Alt+Space`. If it fails, that becomes a documented limitation.

Command for the human demo: `.venv\Scripts\python.exe -m nova`

### Measurement caveat, learned the hard way

`nvidia-smi` reports **machine-wide** VRAM, not per-process. The 264 MiB
"baseline" is Windows' own desktop with no NOVA running at all. Any VRAM figure
taken while another GPU process is resident is contaminated — which is exactly
how this file came to claim 566 MiB retained when the reproducible number is
367 MiB. Take VRAM readings on a quiet machine, or read per-process usage.

Host RSS via `psutil` does not have this problem: it is genuinely per-process.
