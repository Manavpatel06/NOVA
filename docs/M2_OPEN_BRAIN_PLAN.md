# M2 — The Open Brain

**Status: proposed, awaiting Manav's approval. No code yet.**

This milestone pulls the old M3 (brain) and M4 (Claude arm) forward and defers
broad system control. That reorder is deliberate and Manav's call: the assistant
is more useful with one capability it can reason about than with ten it can only
pattern-match.

M0 closed 2026-08-20 when a human spoke to it and it worked. Two things that test
exposed are in scope here: **~5-8 s felt slow**, and **"what can you do" produced
a clarifying question**, which is the wrong answer to the one question an
assistant should always be able to answer about itself.

---

## The governing constraint

> Autonomy grows, the security boundary does not move.

Everything below is designed backwards from that sentence. The rule that makes it
concrete:

**Claude proposes. NOVA disposes.**

Claude never executes anything. It returns either an answer, or a *proposal* —
the name of an already-registered skill plus parameters. That proposal is bound
and authorized through **the same** parameter binder, permission ladder, and
audit log as a locally-routed skill. There is no second path to a tool call, and
Claude cannot invent a capability NOVA doesn't already have.

---

## T2.1 — Self-knowledge (first, because it tests the architecture)

`nova/skills/capabilities.py`, READ tier, answering from the **live registry** —
never a hardcoded list, which would be a second source of truth that drifts the
first time someone adds a skill.

Scope: "what can you do", "what are your skills", "help", "what do you know how
to do", and near phrasings. Must never fall to a clarifying question.

**This is also the architecture's own claim under test.** If adding a capability
really costs one file plus one test with no edits anywhere else, this lands that
way. If it needs a router change or a `SkillCtx` field, that's a finding worth
more than the feature — nova-docs already flagged `SkillCtx` as the likely
pressure point.

Open design question I'll settle with measurement, not opinion: the router scores
on skill `examples`, and meta-questions may sit near the confidence floor. If
"what can you do" scores below 0.42 even with good examples, the fix is *not* to
lower the floor — it's a router change, and that's a bigger conversation.

## T2.2 — Latency, instrumented before it is optimised

Per-stage spans on the bus, so the number is decomposed rather than a single
total:

```
ptt.end / wake.end  ->  transcript ready  ->  route decided
                    ->  skill returned    ->  FIRST AUDIO OUT
```

**"First audio out" is the metric, not turn-complete.** Manav's "felt slow" is
time-to-first-sound; a turn that finishes in 5 s but starts speaking at 1.2 s
feels entirely different from one silent for 4 s.

Reported as **first-turn and steady-state separately** — they differ by the
one-time model load and conflating them hides both.

**Proposed steady-state budget for READ skills: first audio within 1.5 s of
speech ending.** That is a target to defend or revise with data, not a promise.
`time` currently returns in ~0.08 s, so the budget is dominated by transcription
and synthesis, not by skills.

**ADR required, not a silent choice:** pre-warming models at boot buys latency
and costs idle RAM — and we already know the floor is ~740 MB once Whisper has
run, ~850-900 MB with Kokoro (F16, ADR 0006). Pre-warming means paying that at
boot rather than on first use. That trade gets written down with both numbers,
and Manav decides.

## T2.3 — Growing the app allowlist by voice (CONFIRM tier)

Today an unknown app is refused. That refusal is correct and stays — the closed
allowlist is the boundary, and it held against a real spoken request on 2026-08-20.

What changes is what happens *after* the refusal: NOVA offers to add it, and
Manav can approve **by voice**.

Security shape, because this is a config write that grants future launch rights:

- NOVA resolves a **concrete path** before proposing anything — Start Menu
  shortcut resolution, not a filesystem sweep. If it can't resolve one, it says
  so and stops. It never proposes a path it hasn't verified exists.
- The CONFIRM prompt shows **the resolved absolute path**, not the app's friendly
  name. Consent is to launching `C:\...\Spotify.exe`, not to the word "Spotify".
- The write **re-validates with `realpath` on both sides at write time**
  (constitution rule 3) — the path resolved during the conversation is not
  trusted to still mean the same thing when written.
- The new entry is audited with the resolved path.

**A voice confirmation channel is new, and it is the risky part.** Approving a
new executable by saying "yes" is a high-value target, and it is the acoustic
arming problem (F28) wearing a different hat. My proposal: voice approval is only
accepted within the same armed turn that made the request, with a
**repeat-back** ("add Spotify at *path* — say yes to confirm"), and it is
**refused entirely** if the turn was armed acoustically rather than by
push-to-talk, until F28 is settled. nova-security reviews this before it is built.

## T2.4 — Fallthrough to Claude (the largest, deliberately last)

When the router finds no skill, or is below the floor and it is not a
meta-question, the request goes to Claude via the Claude Code CLI / Agent SDK on
Manav's existing Max plan. **$0 recurring, per the constitution.**

**Local skills always run first.** Claude is the fallback, never the default —
both for latency and because a deterministic answer is better than a generated
one whenever we have it.

### What crosses the boundary

Everything leaving goes through `Egress.release_for_process()` — the method
nova-security specified for exactly this, currently unused. Redacted, audited,
length-and-shape logged rather than content.

**Minimal context, stated explicitly:** the transcript, plus the list of
registered skill names and their descriptions so Claude can propose one. Nothing
else. No file contents, no conversation history, no machine details in the first
version. Anything added later is an ADR, not a quiet expansion.

### What comes back

A structured response, not free text: either

- `answer(text)` — spoken, no action taken; or
- `propose(skill_name, params)` — bound and authorized exactly like a local route.

`skill_name` must name an **already-registered** skill. An unrecognised name is a
refusal, not an attempt. Params are validated against the handler's signature by
the same binder, and pass the same `Untrusted` guard.

### Tier rule

**Claude-proposed actions are authorized at `max(declared_tier, CONFIRM)`.**

Local `open_app` is ACT — logged, no confirmation, because Manav pressed a key
and said it. The same action proposed by a model that inferred it is a different
event and gets confirmed. This is the concrete form of "autonomy grows, the
boundary does not move", and it can be relaxed per-skill later *with evidence*,
never by default.

### The honest costs

- **It is slow.** Claude round-trips in seconds. Fallthrough gets its own latency
  budget, and NOVA should say something immediately rather than going silent.
- **It leaves the machine.** This is the one sanctioned egress path, and the
  constitution's privacy rule is kept by redaction and audit, not by pretending
  otherwise. `docs/ADR 0010`'s lesson applies: the AST test proves no *other* file
  opens a socket; it says nothing about what the spawned process does. The
  reserved fourth slot in the spawn allowlist is for this adapter.
- **Offline, NOVA must say so plainly** — not fail silently, not hang.

---

## Order, and why

1. **T2.1 self-knowledge** — small, immediately useful, and tests the one-file
   claim while the stakes are low.
2. **T2.2 latency** — measurement before optimisation, and before Claude
   fallthrough makes the numbers harder to read.
3. **T2.3 app allowlist** — needs the voice CONFIRM channel, which T2.4 also
   benefits from.
4. **T2.4 Claude fallthrough** — largest, and it gets nova-security's threat model
   *before* implementation, not after.

## What I need from Manav

- **Approval of this plan**, or corrections to it.
- **The F28 decision** still blocks the wake word being wired in, and it now also
  shapes T2.3's voice confirmation.
- **The pre-warm vs idle-RAM trade** (T2.2's ADR) once the numbers are measured.
