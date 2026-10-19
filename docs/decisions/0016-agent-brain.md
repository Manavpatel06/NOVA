# 0016 — Claude is NOVA's brain; skills are its tools

Date: 2026-10-15. Manav: "It's just like an if/else ... I want it completely dynamic."

## Decision
- Every addressed sentence (VOICE / TYPED / PTT armed) goes to Claude through the Anthropic Messages API
  (`brain.model`, default `claude-haiku-5-5`, the fastest current model) with every registered skill as a tool,
  the last `history_turns` exchanges (in memory only, forgotten after `history_idle_s`), a context line (time,
  place, the app in front -- never window titles), and Anthropic's web search server tool.
- Claude chooses: answer, ask back, or call tools (chained). Every call goes through `SkillSpec.invoke()` --
  tiers, audit, spoken approval unchanged. WAKE-armed turns never reach the brain.
- The deterministic router is the fallback when there is no key, no network or an API error -- only if no tool
  ran yet (never twice).
- Key: Windows Credential Manager (`NOVA/anthropic-api-key`), set with `scripts/set_api_key.py`. Transport:
  `Egress.post_json` (allowlisted api.anthropic.com, audited by size/status only). Outbound text (sentence,
  context, tool results) is redacted.

## Trust
- Tool calls on Manav's own addressed sentence run like a router match (no approval for ACT).
- Taint: web results, or a reply from an `untrusted_output` skill (weather place names, window titles), taints the
  rest of the turn and every turn while that exchange is remembered. Tainted calls carry the Claude-origin floor
  AND need "Nova, confirm" (a plain "yes" is not enough). Values read aloud in approval questions are capped.
- CONFIRM-tier skills always need "Nova, confirm".
- Known residual risk: a "Nova, ..." spoken by a video on the laptop is a real command; no speaker verification.

## Cost / privacy
Haiku tokens are cents per day at personal use; web search $10 per 1,000 searches. Sentences addressed to NOVA
(and its tool results) leave the machine to Anthropic; un-addressed speech never does.
