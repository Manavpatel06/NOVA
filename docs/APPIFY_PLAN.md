# Appify NOVA — make it indistinguishable from a professional Windows app

Manav's directive (2026-10-13): NOVA must not look like a Python script in
any surface a user sees. Done so far (config/installer level, no code):

- Start Menu + optional Startup shortcut, launched windowless (no console)
- Branded shortcut icon: `assets/nova.ico`
- ~~Own process name via a renamed venv launcher copy (`NOVA.exe`)~~ —
  **reverted 2026-10-14**: after the switch, Start Menu launches produced no
  log lines at all (nova.log shows no start after the change), so the
  shortcut is back on `pythonw.exe`. A real `NOVA.exe` comes from item 3.
- install.ps1 now launches NOVA and waits for `NovaApp ready` in
  `data/nova.log`, so a silent failed start is reported, not hidden

## Remaining work — needs code + on-machine builds (team)

1. **Tray icon from assets.** `nova/interfaces/tray.py` draws its icon
   programmatically; load `assets/nova.ico` instead so the tray, taskbar
   and notifications all show the brand mark. Trivial, test on hardware.
2. **AppUserModelID.** Call
   `shell32.SetCurrentProcessExplicitAppUserModelID("Manav.NOVA")` at boot
   so Windows groups windows/toasts under NOVA, not Python.
3. **Real executable (PyInstaller, onedir).** Build `NOVA.exe` with
   `--icon assets/nova.ico`, embedded VERSIONINFO (ProductName "NOVA",
   CompanyName "Manav Patel", version from pyproject), console disabled.
   Known hazards on this stack: ctranslate2/CUDA DLL discovery
   (`os.add_dll_directory` paths change under the bundle), onnxruntime and
   kokoro model paths, `keyboard` hook. Budget real test time; the probe
   scripts are the acceptance suite. Models/caches stay OUTSIDE the bundle
   (they're multi-GB and already live in D:\NOVA).
4. **Setup wizard (Inno Setup, free).** A `setup.exe` that installs the
   PyInstaller bundle, creates shortcuts, offers auto-start, uninstalls
   cleanly. This replaces install.ps1 for strangers; install.ps1 remains
   the dev path.
5. **First-run experience.** If models are missing, NOVA says what it is
   downloading and shows progress, instead of a stack trace.

Order: 1 and 2 are an afternoon and remove the last visible Python traces
on Manav's own machine. 3 and 4 are for distributing to other people —
schedule after T2.4 (Claude wiring), not before.


## 0. DONE 2026-10-14 — superseded by the NOVA window (ADR 0013)

Shipped as a full window instead of an overlay. Original notes kept below.

### (original) a visible interface — "the NOVA orb"

Manav's words: "I don't see an interface... I want an interface coming when I
activate NOVA." Today NOVA's only surface is a tray icon, so a working NOVA
looks identical to a dead one. Build a small always-on-top overlay:

- **Hidden at idle.** Appears (fade/slide in, bottom-center or top-right) on
  `ptt.start` / `wake.start`; shows state with the brand mark
  (`assets/nova.png`): *Listening* (pulsing teal arcs) → *Thinking* →
  *Speaking* (star glow) → hides ~3 s after the reply.
- Shows the transcript ("heard: …") and NOVA's reply text — this also makes
  mis-hearings visible (see finding below).
- Click-through when not focused, never steals keyboard focus, Esc dismisses,
  respects reduced-motion. No network, no new egress.
- Subscribes to the existing bus topics only (ptt.*, wake.*, turn events,
  player start/stop) — UI must not be able to trigger actions (security
  review: the overlay is display-only).
- Tray menu gets "Open NOVA" which shows the orb with a text box, so Manav
  can also type to NOVA.
- Toolkit: tkinter (already required for CONFIRM dialogs) is fine for v1;
  measure idle RSS/CPU — budget: <30 MB, ~0% CPU when hidden.

## Finding (2026-10-13 log): short-clip mis-transcription — FIXED 2026-10-14 (ADR 0013)

A 0.81 s push-to-talk clip was transcribed as `'¡Gracias!'` despite
`stt.language: en`. Check that `language` is actually passed to
faster-whisper, and add a minimum-speech / no-speech-probability guard so
near-silent clips are dropped ("I didn't catch that") instead of producing
Whisper's classic silence hallucinations.
