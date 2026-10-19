# 0017 — The brain runs on Manav's Claude Max plan, not an API key

Date: 2026-10-15. Manav: "I don't want to use the API key. I want it to link with my Claude Max plan."

## Decision
- `brain.mode: max` (default): NOVA keeps ONE long-lived `claude` process (the official, unmodified Claude Code
  CLI, already signed in with his Max subscription) in `--input-format/--output-format stream-json` mode
  (`nova.brain.claude_arm.ClaudeSession`), so the process and the conversation stay warm between sentences.
- Same hardening as the existing fallthrough: `--safe-mode`, `--strict-mcp-config`, `--no-session-persistence`,
  API-key env vars scrubbed (no accidental metered billing), no local tools (`--tools ""`, or only the
  server-side `WebSearch` when `brain.web_search` is on), empty working dir `data\brain`, no console window,
  every message through `Egress.release_for_process` (redacted + audited), kill + tree-kill on timeout.
- NOVA's skills are a catalogue in the system prompt; Claude answers in a small JSON protocol
  (`say` / `actions` / `final`). `final: true` + all actions OK = one round trip. NOVA executes every action
  through `SkillSpec.invoke()` (ladder, audit, spoken approval). Plain-text replies are spoken, never acted on.
- Trust as ADR 0016: web search or `untrusted_output` skill output taints the session → "Nova, confirm" for
  actions until the session is replaced (10 min idle or turn limit).
- `brain.mode: api` (ADR 0016) remains available; `router` = fixed commands only. The router is always the
  fallback when Claude is unreachable and nothing has run yet.

## Terms
Anthropic's Claude Code legal page: subscription (OAuth) sign-in is for ordinary use of Claude Code by the
subscriber; Max limits "assume ordinary, individual usage of Claude Code and the Agent SDK". NOVA is Manav's
personal tool driving his own signed-in, unmodified CLI; it never handles his credentials. Usage counts
toward his Max limits.

## Measured (cloud, 2026-10-15)
Warm round trip ~3 s (`--effort low` no faster). "can you open youtube in grave" → open_website(youtube, brave);
"now close it" resolved from conversation memory.

## Addendum 2026-10-16 — two-speed brain
Manav asked for speed; a local model on the old laptop (i5, 8 GB, no GPU) would be slower than Claude (CPU prefill
of a ~5k-token prompt), so instead: a short (≤ 8 words), single command (no "and/then/…") that exactly matches a
command pattern of a skill in `brain.instant_skills` (volume, media, brightness, time/date, battery, lock,
screenshot, Wi-Fi/Bluetooth, show desktop …) runs immediately (~0.7 s); the exchange is noted to the brain so
follow-ups work. Anything with a free-text name (apps, sites, searches) or any other phrasing goes to Claude.
A crashed instant skill is apologized for and never retried.
