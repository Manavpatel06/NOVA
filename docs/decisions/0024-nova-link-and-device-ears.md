# 0024 — NOVA Link: the hub's NOVA controls the laptop, and hears through it

Date: 2026-11-25. Phases 2 and 3 of ADR 0023. Manav: "full control over all my devices", and talk "over there" so
the laptop stays free of memory.

## The laptop helper (`nova/link.py`, `pythonw -m nova.link`)
- **Memory and connection.** Measured on the laptop: about 87 MB with every skill and the speech detector
  loaded (the full NOVA app is about 900 MB). It has no AI models. It connects out to the hub over Tailscale,
  long-polling `/v1/link/poll`, so the laptop opens no port and its egress allowlist is only the hub's name.
- **Every NOVA skill is offered on the hub.**
  - `HubAssistant.skills()` keeps the hub's own skills (ADR 0023) and wraps every other registered skill in a
    proxy: the same SkillSpec (tier, confirm phrase, patterns, untrusted_output) with a handler that queues a job
    for the laptop. The handler's signature is kept, so parameter binding still fails before the ladder.
  - **Approval happens on the hub, before anything is queued**, through the same ladder and spoken
    "Nova, confirm". Tested: "yes" to a CONFIRM-tier laptop action queues nothing; "Nova, confirm" runs it.
  - A skill with `resolve_for_confirm` (open_app) asks the laptop first. The question then shows the real path
    (verified live: notepad → `C:\Windows\System32\notepad.exe`), and the ladder's re-check after approval goes
    to the laptop too.
  - When the laptop is offline, NOVA says so straight away, without asking or queueing. Claude's context says
    whether the laptop is connected.
- **Jobs** (`nova/hub/devices.py`).
  - Kept in memory only, and dropped if not picked up within 30 s (a laptop that wakes later never runs a stale
    "shut down").
  - A result is accepted only from the device the job went to.
  - Results are capped at 4,000 characters.
- **On the laptop**, a job runs only if the skill is registered and the parameters bind. Each run is audited in
  `data/link-audit.log` with `approved_on: hub`.
- **Upgrades.** `apply_upgrade` runs on the hub as the hub's own skill (`hub_apply_upgrade`): CONFIRM, with the
  same read-back and re-check.
  1. The hub fast-forwards and pushes.
  2. It sends an `update` job, and the laptop runs `winops.git_update` for exactly that commit and then restarts
     itself (`winops.restart_link`).
  3. The hub restarts itself (systemd `Restart=always`).
- **Startup.** `scripts/install_link.ps1` asks before adding a sign-in shortcut, then starts the helper. The old
  NOVA app is untouched, but the two shouldn't run together, since both would listen to the mic.

## Device mics (`nova/ears.py`) — the laptop's, and the hub's own
- **On the device.** Only Silero VAD runs there; it cuts finished utterances out of the mic. Each one goes to
  the hub (`/v1/link/hear`, or in-process on the hub).
- **On the hub.**
  - It transcribes and acts only when the sentence starts with NOVA's name, follows a bare "Nova" (one
    sentence, 8 s), or answers NOVA's question. Everything else returns `{"ignored": true}`: the words are
    never logged or returned, the same rule as prefix mode on the laptop.
  - Turns from an open mic are `ArmedBy.VOICE`. The phone page's tap is `PTT`.
- **While NOVA is talking.** The mic is ignored while NOVA speaks and for 0.4 s after, and while a sentence is
  being handled.
- **The hub's own mic** is off by default. `nova-hub ears on` sets a flag file, so the tracked config stays
  untouched. The hub needs PipeWire, so it hears the room only while someone is signed in on it. With no mic,
  it logs that and carries on.
- **Speech detector.** `nova-hub voice` also downloads Silero VAD v5.1.2 (sha256 2623a295…). It matches the
  laptop's file's size and interface; it was checked to cut a Kokoro-spoken sentence correctly (one 2.85 s
  utterance).

## Verified
- Real laptop skills ran through the helper on the laptop: volume, battery, brightness, time and resolving
  open_app.
- A real `Link.serve()` long-polled a real hub listener over HTTP, and a laptop skill went there and back.
- Tests: `tests/test_hub_link.py` (34 tests).
