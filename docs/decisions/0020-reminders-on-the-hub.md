# 0020 — Reminders and timers live on the hub; Bark rings the iPhone

Date: 2026-11-24. Builds on ADR 0019 (the hub) and the Resource Map's free swap: Bark instead of Pushover
(free, critical alerts ring through silent mode).

## Decision
- **Skills** in `nova/skills/reminders.py`:
  - `remind_me` (text, when, urgent), `set_timer` (duration) and `cancel_reminders` are ACT; `list_reminders`
    is READ.
  - Spoken times are parsed on the laptop by `nova/when.py`, using local clock time:
    - Supported: "in 20 minutes", "at 7", "tomorrow at 9", "tonight at 8", "an hour and a half".
    - A bare 1–5 o'clock means pm.
    - Anything unclear or in the past returns None, and NOVA asks "when?".
- **The hub keeps them** in `data/reminders.json` (`nova/hub/reminders.py`), behind the owner-only endpoints
  `/v1/reminders`, `/v1/reminders/list` and `/v1/reminders/cancel`. It fires them from a 10-second scheduler
  thread, so they work with the laptop closed.
  - **Normal:** one Time Sensitive notification.
  - **Urgent:** a Critical alert with `call=1`, repeated every 60 s until acknowledged, at most 10 times.
  - **Missed:** if the hub was off for more than 12 h past a reminder, it is sent once, marked late.
  - **Cleanup:** finished reminders are kept 7 days, then dropped.
- **Bark** (`nova/hub/notify.py`):
  - The hub's Egress allows exactly one internet host, `api.day.app`.
  - The device key lives in `data/bark_key` (mode 600, set with `nova-hub bark <key>`) and is never logged.
- **Privacy by default** (`hub.alert_text: false`): the notification says only "Reminder -- tap to see it".
  - Tapping opens `https://<hub>/v1/ack/<id>?t=<token>` over Tailscale. That page is owner-only, checks the
    Host header, compares the token in constant time, escapes the text, and sends a CSP header. It shows the
    words and stops an urgent reminder ringing.
  - Bark's end-to-end encryption was not used because its format couldn't be verified from the docs.
- **Not n8n.** Reminders and future automations stay in NOVA's own code, under its single egress, audit and
  approval ladder. A second automation engine would hold its own credentials and network access outside
  those.

## Open
- Bark tap-to-open behaviour and critical-alert permission: verify on the iPhone with `nova-hub ping` and
  a 1-minute urgent reminder.
- Spoken reminders on the laptop too (when it's on), in addition to the phone.
