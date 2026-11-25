# 0032 — The right model per question, NOVA warns first, update/reboot by voice, addresses you say

Date: 2026-11-25. Manav: "Start building ... rest of the things you can build."

## The right model for each task (api mode)
- Questions that need real thinking go to `brain.hard_model` (Claude Sonnet 5.5); commands and small talk stay on
  `brain.model` (Haiku 5.5). "Needs thinking" = 28+ words, or explain / why / compare / pros and cons / plan / write
  an email or essay / summarize / solve / calculate / should I / brainstorm / teach me / "think hard".
- A short follow-up within two minutes stays on the stronger model ("and what about mRNA ones?").
- Prices from Anthropic's pricing page (2026-11-25): Sonnet 5.5 $2 in / $2.50 cache write / $0.10 cache read / $10
  out per MTok (no long-prompt surcharge), Opus 5.5 $4 / $5 / $0.20 / $20; Haiku 5.5 unchanged. Spend records the
  model actually used, so the $90 budget stays honest (Sonnet is ~20x Haiku).
- `hard_model: ""` = always the fast model.

## NOVA warns first (`nova/hub/watch.py`, every minute, through Bark)
- Hub on battery for 2 minutes -> "is it unplugged?"; again at 20 % and 10 %; "plugged in again" when it's back.
- 90 C or hotter twice in a row -> once an hour at most. Under 5 GB free -> once a day at most.
- Each morning at `hub.heads_up_hour` (8; -1 = off), if Canvas is connected: how many assignments are due in the
  next two days (and how many are already submitted). Only counts go through Bark and Apple; titles stay on the hub
  ("Nova, what's due?"). Once a day, remembered across restarts.

## Update and reboot the hub by voice (`nova/hub/ops.py`)
- `update_nova` (CONFIRM): runs `nova-hub update` as a transient systemd unit, so restarting NOVA doesn't cut the
  update off.
- `reboot_hub` (CONFIRM): `sudo -n systemctl reboot`, allowed by one sudoers line that Manav adds himself with
  `nova-hub voice-reboot on` (y/n, checked with visudo; `off` removes it). Without it NOVA says how to turn it on.
- Fixed argument lists only; joins the pinned process allowlist with a test that pins both commands.

## Addresses Manav says (`nova/brain/aliases.py`)
- Everything sent to Claude has email addresses redacted, so a dictated address couldn't be used. Now an address in
  Manav's own sentence becomes `[EMAIL-1]` for Claude; NOVA swaps the real address back into the tool's parameters
  on this machine, and into NOVA's spoken reply. Claude never sees it (tested).
- Addresses in outside content (an email, a web page) stay plain `[EMAIL]` and can't be used as parameters (rule 2,
  tested). Stand-ins are forgotten with the conversation.

## Phone
Plivo alongside Twilio: see the update in ADR 0025.
