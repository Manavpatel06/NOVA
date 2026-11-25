# 0028 — NOVA's live face on the hub's screen

Date: 2026-11-25. Manav: "I want the interface to look like Nova is live over there, and it moves whenever the
response is given, like the interface of my desktop ... I'm keeping my lid open, so I want it to be active."

## What it is
- **`/face`** is a full-screen page served by the hub's listener: the desktop app's star and arcs (same colors and
  motion), a clock and date, and the last thing said to NOVA with its reply.
  - It breathes when idle, pulses while listening, sweeps while thinking, and ripples while speaking.
  - The conversation fades after three quiet minutes.
  - The whole figure drifts slowly, so an always-on panel never holds one image.
  - It polls `/v1/face/state` every 300 ms.
- **What feeds it:** `nova/hub/face.py`, driven by Talk from every path — the phone page, the laptop and hub mics
  (addressed sentences only) and phone calls.
  - Speech not addressed to NOVA is dropped before it reaches the face. Tested: the screen never shows the room.
- **`nova-hub face on`** makes the face the hub's desktop background (Manav: "one app ... which goes as my
  background of the hub ... I can also access the terminal or apps"):
  - `hub/face/renderer.py` is a small GTK 4 + WebKitGTK app (system Python, apt `python3-gi gir1.2-gtk-4.0
    gir1.2-webkit-6.0`, asked y/n) showing `http://127.0.0.1:8765/face` in one borderless window. It retries every
    5 s while the hub service restarts.
  - The `nova-face@nova` GNOME Shell extension moves that window into the shell's background layer (above the
    wallpaper, below every window), on every workspace, and keeps it out of the overview and Alt+Tab. Every other
    app and the terminal open on top of it. Disabling the extension gives the window back.
  - Why our own: Ubuntu 26.04's GNOME is Wayland-only, and the existing live-wallpaper extensions (Hanabi, GNOME
    Wallpaper Engine) play videos, not web pages. This uses the same technique as Hanabi.
  - GNOME Shell on Wayland only loads a newly installed extension after one sign-out/sign-in; until then the face
    shows as an ordinary full-screen window.
  - The app starts at sign-in (`~/.config/autostart`); the old Firefox kiosk is removed.
  - It keeps the screen awake: no blanking, dimming or sleep.
  - It asks y/n before turning on automatic sign-in after a restart, and says that anyone who opens the hub then
    sees the desktop.
  - `nova-hub face off` undoes all of it, extension included.

## Who can see it
The state carries what was said, so it is served only to two kinds of request:
- the hub itself: Host exactly `127.0.0.1:8765` and no Tailscale identity;
- Manav's own devices: the owner check.

Refused (tested): a tailnet device with no user, another user, DNS rebinding, Funnel, and a cross-site page.

## Also in this change: speaking in pieces
- Long replies are split into sentences, with a short first piece. The first is synthesized and returned at once;
  the rest are synthesized in the background and fetched in order (`/v1/talk/more`).
- The phone page schedules the pieces back-to-back, and the laptop and hub mics prefetch the next piece while
  one plays.
- Measured with the real voice model (2-core build environment): the first sound for a two-sentence poem reply
  went from 8.2 s to 1.5 s after the words were ready.
