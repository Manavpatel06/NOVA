# 0019 — nova-hub: a local model lane in front of Claude, and NOVA's memory mirrored to the hub

Date: 2026-11-22. Manav: run "our own local LLM … along with the Claude, merging both, so it is more efficient
and also does not consume more tokens"; local model on the **hub only**; day one = "Brain + local LLM" (the hub
setup plus local models, the model router, NOVA's brain lane and memory on the hub, a health check and a
nightly memory backup); and "when I boot my laptop, one single command or two commands should do the work".
Builds on the hub plan (Ubuntu old laptop, 2026-10-26) and ADR 0018 ("nightly backup … comes with the hub
service").

## Decision
- **Three lanes per sentence.** The laptop's instant lane (exact patterns) comes first, then the **hub lane**:
  the hub's small local model (Ollama, `qwen3.5:2b` on CPU). Anything the hub isn't sure about goes to
  Claude on the Max plan, as before.
- **The hub only proposes.** Its decision (`tool` / `chat` / `escalate`) is forced into a JSON schema. The
  laptop re-checks it in `nova/brain/hub_tier.py` and sends anything doubtful to Claude:
  - Only READ/ACT skills are offered: no CONFIRM skills, no confirm phrase, no untrusted output, and never
    `set_radio`.
  - Params must be the skill's own.
  - Text values must appear in what Manav said, and numbers must be ones he said.
  - Sentences with a negation or condition are never acted on.
  - Small talk is recognised on the laptop, not by the model, and the reply may contain no digits, so the
    model can't make up facts.
  - Once checked, the skill runs through `SkillSpec.invoke()`: same ladder, audit and spoken approval as
    every other lane.
  - Claude is told afterwards (`MaxBrain.note`), so follow-ups still make sense.
- **Network shape.**
  - The hub service binds **127.0.0.1:8765** only. Its one socket lives in `nova/hub/listener.py`, which is
    now the second entry in the transport allowlist; a test pins it to loopback.
  - `tailscale serve` publishes it over HTTPS to Manav's tailnet and adds `Tailscale-User-Login`.
  - Route and memory requests need: (a) that header matching the owner recorded by `nova-hub login`
    (`data/hub_owner`; no file means nobody), and (b) a Host header that is the hub's own Tailscale name or
    127.0.0.1. Check (b) blocks DNS rebinding from a page open on the hub.
  - Other tailnet devices get only `{"ok": true}` from /v1/health.
  - The firewall only admits tailscale0.
  - The hub reaches Ollama through the new `Egress.local_json`: 127.0.0.1 only, with ports fixed at
    construction.
  - On the laptop, the hub's name joins the egress allowlist only when `hub.host` is set in nova.yaml, still
    fixed at start.
- **Memory leaves the laptop, to Manav's own hub only.** This updates CLAUDE.md's "data stays on this machine"
  for the hub he asked for.
  - On start and on exit, the laptop PUTs `data/memory` (facts, journal, chat card) to the hub's mirror. The
    upload is complete, so the mirror deletes what was forgotten on the laptop.
  - The hub snapshots the mirror at 03:30 and keeps 30 days of snapshots. Forgotten items survive in old
    snapshots for at most that long.
  - Memory content is not redacted: it goes to his own machine over Tailscale (WireGuard + HTTPS). The
    sentence sent for routing is redacted as usual.
- **Two commands.**
  1. `setup_hub.sh` does all of the following:
     - the OS settings
     - Tailscale and Claude Code
     - Ollama, set to 127.0.0.1 only, one model, a 4k context, and the model kept loaded
     - the qwen3.5:2b model
     - a clone of NOVA into ~/NOVA, with a venv containing only PyYAML plus NOVA with no dependencies
     - systemd user units for `nova-hub` and the nightly backup
  2. `nova-hub login` does all of the following:
     - Tailscale sign-in, recording the owner
     - HTTPS publish, recording the hub's name
     - Claude sign-in
     - the full checklist
     - a restart
  - After that: `nova-hub status | test | update | logs | backup`.

## Not decided yet / to measure on the hub
- Speed on the i5 is not measured. `nova-hub test` runs ten sentences and prints the time for each. On CPU the
  lane mainly saves Max-plan usage; it may not be faster than Claude for every sentence. Ollama's prompt cache
  keeps the fixed tool list from being re-read on each sentence.
- How accurate qwen3.5:2b is at picking tools is unmeasured. The laptop's checks make mistakes fall through
  to Claude rather than act.
- Two `tailscale serve` details need checking on the installed version: that it replaces any client-sent
  `Tailscale-User-*` headers, and which Host header it forwards. The Host check accepts both the hub's name
  and 127.0.0.1:8765.
- The laptop needs `hub.host` set once the hub is up; `nova-hub status` prints the value.
