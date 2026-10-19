# 0018 — NOVA's memory: plain files, separate from Claude chat memory, linked by reading

Date: 2026-10-19. Manav: memory "stored somewhere as txt or json file for backup"; NOVA's memory and his Claude
chats "should not collide ... stored separately ... correlate to each other"; then "no double storing, just
context aware".

## Decision
- NOVA's memory = one folder, `data/memory` (`nova.MEMORY_DIR`): `facts.json` (remember / recall / forget skills;
  the brain is told to remember lasting, non-sensitive facts on its own), `journal/<day>.md` (every answered,
  addressed sentence; never wake-armed turns; `brain.journal` switch), `from_chat.md` (read-only for NOVA).
- Claude in Cowork never copies NOVA's facts or journal into its own memory: it reads `D:\NOVA\data\memory`
  through the device link when NOVA comes up (live, when the laptop is reachable).
- NOVA cannot reach Claude's chat memory (the `claude` CLI has no access to it), so Claude in Cowork keeps ONE
  short context card, `from_chat.md` (≤ 4,000 chars), rewritten in place — a view, not a second history.
- The brain's system prompt carries: remembered facts (newest 60), the card, the last 12 journal lines — labelled
  as information, never instructions; outbound text redacted as before.
- Plain UTF-8 JSON/Markdown, atomic writes, no OS paths inside → moves to the hub (Windows or Ubuntu) by copying
  the folder; nightly backup to the main laptop comes with the hub service.
