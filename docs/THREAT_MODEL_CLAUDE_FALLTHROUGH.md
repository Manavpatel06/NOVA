# NOVA — Threat model: T2.4, the Claude fallthrough

Authored by nova-security. Companion to `docs/THREAT_MODEL.md` §2 (which
scoped the Claude-CLI egress boundary before any caller existed) and
`docs/THREAT_MODEL_ALWAYS_ON.md` (which found the identical missing-seam shape
in a different feature, F28). Written against `docs/M2_OPEN_BRAIN_PLAN.md`
T2.4, before implementation, per the lead's request.

Grounded against what's actually on disk today: `grep -rln "claude"
nova/ docs/CONTRACTS.md` finds nothing under `nova/` — no Claude-arm file
exists yet. `docs/CONTRACTS.md` names exactly one relevant contract line,
`Egress.release_for_process(purpose, payload) -> str`, already implemented
(`nova/security/egress.py:180-199`) and unused. Everything below is reviewed
against the real `Ladder`, `SkillSpec.invoke()`, `Router`, and confirm-channel
code as they exist right now, not against an imagined future version.

---

## Verdict, up front

**The shape — "Claude proposes, NOVA disposes," bound through the same
binder/ladder/audit as a local route — is the right design and I am not
vetoing it.** But three of the gaps below are foundational rather than
polish: implementing T2.4 today, against the code as it stands, would not
produce the boundary the plan describes. I would block a merge on findings
**1–3** specifically (decide + prove, not just decide) before any code lands;
4–8 are real but do not need to gate the start of implementation.

---

## 1. Does the spawned process itself have tools? — HIGH, undecided, the load-bearing question

`docs/M2_OPEN_BRAIN_PLAN.md` T2.4 says NOVA reaches Claude "via the Claude
Code CLI / Agent SDK." `docs/ARCHITECTURE.md`'s brain section separately
describes a "Claude Code arm" that "shells the local `claude` CLI headless,
**supervises the job**, and reports back by voice" for "build me an app that
X" — a fundamentally different, wider action than "propose one registered
skill call." The M2 plan's own opening line says it "pulls the old M3 (brain)
and **M4 (Claude arm)** forward," which means these two different-shaped
capabilities are now sharing a milestone and, on the current wording, quite
possibly sharing an invocation path.

This is not a wording nitpick. The entire "Claude proposes, NOVA disposes"
guarantee rests on Claude returning *structured output that NOVA then acts
on* — `answer(text)` or `propose(skill_name, params)` — and nothing else. The
Claude Code CLI, run in its normal interactive/agentic mode, has its own
tool-use loop: bash, file edit/write, MCP servers, web-fetch, all executing
**inside the child process**, before, during, or instead of ever emitting the
structured response NOVA is waiting to parse. If T2.4's invocation leaves
that tool-use loop enabled, then:

- Redaction/audit (`Egress.release_for_process`) protects the *prompt going
  out*. It says nothing about what the child does once it's running — this
  is `docs/THREAT_MODEL.md` §2.2(6)'s point, and ADR 0010's generalized
  lesson ("a check that is accurate about the surface it inspects, paired
  with a delegate that does more than the surface implied") applies exactly
  here, one more time.
- The AST no-socket/no-spawn test (`tests/test_no_network_or_process_spawn.py`)
  proves NOVA's own tree doesn't reach around `Egress` to spawn the child.
  It says nothing, and structurally *can* say nothing, about what the child
  does with its own bash/file/network tools once running — that's `claude`'s
  code, not NOVA's, and out of AST reach entirely.
- "Claude never executes" would be false as a factual claim about the
  process, true only as a claim about *NOVA's dispatch logic* — which is a
  much weaker and more dangerous thing to have believed while writing every
  other control in this document.

**This must be pinned down as an explicit, tested decision before
implementation, not left to whichever flags the implementer happens to pass
first**: T2.4's invocation of `claude` must run with tool-use structurally
disabled — no Bash, no Edit/Write, no MCP servers, no WebFetch — a plain
structured-completion call, whether that means the Agent SDK with an empty
tool list or `claude -p` with an explicit deny-list proven closed. If NOVA
ever wants the *other* thing — Claude actually acting with its own tools,
the "build me an app" arm `ARCHITECTURE.md` describes — that is a
categorically larger capability (predecessor's phrase: "closer to briefly
handing over an account than to sending a chat message") needing its own
CONFIRM-plus-workspace-jail control, not something T2.4 should already be
doing under the smaller-sounding name "fallthrough."

**Required before T2.4 ships:** a real-subprocess test in the style already
proven out and praised in `docs/THREAT_MODEL.md` §2.2(5)
(`_OLD_ARCHIVE/tests/test_claude_arm.py`'s `test_no_token_and_no_prompt_
reach_argv` etc.) — spawn the actual child with a prompt engineered to try to
write a file or run a shell command, and assert nothing happens on disk and
nothing runs. An assertion in a docstring that tools are disabled is not
evidence; the old build's own lesson (`_OLD_ARCHIVE/KNOWN_ISSUES.md`) is that
"discipline, not architecture" is exactly the failure mode that goes unnoticed
until something exploits it.

---

## 2. The tier-floor mechanism the plan needs does not exist — HIGH, blocks the stated tier rule

`Ladder.authorize()` (`nova/security/ladder.py:111-112`) does exactly one
thing to determine tier: `tier = skill.tier`. There is no parameter, no
`SkillCtx` field, no second input of any kind. The module's own docstring
already anticipated this exact need and left it unbuilt (`ladder.py:21-27`):
"if a future milestone adds [a per-call/param-derived tier signal], it must
be combined as `max(declared, computed)`." T2.4 is that future, and it is
not wired in.

Two concrete problems, not one:

- **There is no seam to say "this call was proposed by Claude."** `SkillSpec.
  invoke()` (`nova/skills/registry.py:126-154`) is the one entry point every
  caller — router match or Claude proposal alike — must go through, which is
  correct and exactly what makes the design's "no second path to a tool call"
  claim true. But `invoke()` calls `ctx.ladder.authorize(self, params)` with
  no way to pass "and elevate the floor to CONFIRM because this came from
  Claude." Implementing `max(declared_tier, CONFIRM)` as stated needs either
  a `floor` parameter on `authorize()`, an `origin` field threaded through
  `SkillCtx`, or equivalent — none of which exist today.
- **`Tier` has no ordering.** `nova/security/tiers.py` defines `Tier` as a
  plain `Enum`, not `IntEnum`, with no `__lt__`/rank. `max(declared_tier,
  CONFIRM)` is not currently a meaningful expression over `Tier` members —
  Python's `max()` needs an ordering it doesn't have. A rank mapping
  (`READ < ACT < CONFIRM`) has to be added deliberately, in one place, as
  part of this work.

**This is the identical missing seam `docs/THREAT_MODEL_ALWAYS_ON.md` already
found and named for a different feature (F28, "no seam for arming source at
all"), independently discovered.** That document's §3.3 already specifies the
right general shape — thread a floor input (there: `presence:
Literal["physical","acoustic"]`) into `Ladder.authorize()`, combined via
`max(declared, floor_for(...))` — and it is still open, unimplemented, per
`docs/TASKS.md` F28.

**Recommendation, stated plainly: build this once, generically, as shared
infrastructure both F28 and T2.4 plug into** — e.g. `Ladder.authorize(skill,
params, *, floor: Tier = Tier.READ)`, with the router's call site passing
`floor=floor_for(presence)` and the Claude-arm's call site passing
`floor=Tier.CONFIRM` (or `Tier.ACT`, see finding 4), both composed via the
same `max()` and the same rank table. Building two independent one-off floor
checks — one invented for T2.4, one invented later for F28/wake-word — is
the exact shape of mistake already on record as F14 ("two redaction
disciplines where there should be one"). Whichever of T2.4 or the wake-word
wiring lands first should build the general mechanism; the other should
find it already there and use it.

**Consequence for T2.3's "blocked on F28" plan:** the lead's stated mitigation
— voice-approval "refused entirely if the turn was armed acoustically" — is
true today only because wake-word is still unwired (`grep -n "wake\|_on_wake"
nova/app.py` finds nothing; confirmed directly, not assumed). It is not
enforced by any code, because there is no `presence`/`origin` signal anywhere
in `SkillCtx` or `Ladder` for it to be enforced by. "Refused entirely" is
currently a sentence in a planning document, not a checked property — the
exact shape `docs/THREAT_MODEL_ALWAYS_ON.md` §1.1 already warned about
("nothing in the ladder's code will break... because none was ever built").
**Verdict on T2.3's blocking: the policy call is right, but is not yet
sufficient as a control — it becomes sufficient only once the shared floor
mechanism above exists and the wake-word wiring is required to go through
it.** I'd land that mechanism before either T2.3b or the wake-word wiring,
not after.

---

## 3. CONFIRM does not show what the plan promises it will show — HIGH, reproduced against real code

Traced the actual control flow, not assumed it: `SkillSpec.invoke()`
(`registry.py:151-154`) calls `ctx.ladder.authorize(self, params)` **before**
`self.handler(ctx, **params)` ever runs. `Ladder._record()`
(`ladder.py:183-193`) and the CONFIRM channels' `format_confirm_prompt()`
(`nova/security/confirm.py:96-105`) both render whatever is in `params` at
that point — i.e., before the handler has done anything.

For `open_app` specifically, the realpath resolution that turns a symbolic
name into a concrete, verified absolute path happens entirely *inside* the
handler (`nova/skills/open_app.py:102-120`, via `_resolve_and_verify`), which
only runs *after* `authorize()` has already returned `allow`. So a CONFIRM
prompt for `open_app` — however it gets triggered, T2.3's voice-approval flow
or T2.4's Claude-proposal floor — would show `{'app': 'spotify'}`, never the
resolved absolute path.

This directly contradicts two explicit, already-written requirements:

- `docs/M2_OPEN_BRAIN_PLAN.md` T2.3: *"The CONFIRM prompt shows the resolved
  absolute path, not the app's friendly name. Consent is to launching
  `C:\...\Spotify.exe`, not to the word 'Spotify'."*
- `docs/THREAT_MODEL.md` §2.2(6): *"the CONFIRM prompt shown to Manav include
  the actual post-redaction text that will be sent, not a generic toggle...
  CONFIRM here is consent to a category, not to the specific disclosure."*

This is not a gap in a not-yet-built feature — it's a property of the
already-shipped `SkillSpec.invoke()` / `Ladder.authorize()` ordering that
both T2.3 and T2.4 inherit unmodified. Any Claude-proposed `open_app` call
elevated to CONFIRM by finding 2's floor would hit exactly this gap.

**Required fix, one of:**
(a) a `resolve(ctx, **params) -> dict`-shaped pre-authorize hook that
`invoke()` calls and merges into what's authorized/audited/shown, with the
handler still independently re-resolving and re-verifying at call time
regardless (constitution rule 3 — never trust a pre-CONFIRM resolution not to
have changed by the time the handler actually launches something); or
(b) restructure `open_app` (and any future CONFIRM-worthy skill) so
resolution is a separate step called before `ctx.ladder.authorize()`, with
the *resolved* value substituted into the `params` dict that gets audited and
shown. Either way, the handler must still re-validate at call time — this
closes the *disclosure* gap, not a replacement for the existing realpath
re-validation. I'd treat this as blocking for T2.3 and T2.4 alike, since
CONFIRM's entire value proposition here is "consent to the concrete thing,"
and today the plumbing cannot produce that concrete thing before consent is
asked.

---

## 4. `max(declared_tier, CONFIRM)` applied to READ is very likely to become the disabled control — MEDIUM, refine don't veto

Applied literally, the floor in finding 2 would also elevate READ-tier
proposals (`time`, the incoming `capabilities`/T2.1) to a modal CONFIRM
dialog the instant Claude — not the local router — is the one proposing them.
READ is defined, by declaration, as free/reversible/no-side-effect
(`nova/security/tiers.py:15-16`, `docs/THREAT_MODEL.md` §4: *"'Free' means UX
cost, not 'unchecked.'"*). Nothing about *who proposed* a call to `time`
changes what the world looks like afterward. Gating it behind a real dialog
buys no security — the action is inert either way — while manufacturing
exactly the friction the lead flagged as the risk worth avoiding: *"if it
makes NOVA annoying enough that Manav relaxes it, that's a control which will
be disabled."* A CONFIRM prompt for "what time is it, asked in a way Claude
had to parse" is close to a worst case for that erosion.

**Recommendation:** scope the floor to `declared_tier != READ`, i.e.
`effective = declared if declared is READ else max(declared, CONFIRM)`. ACT
becomes CONFIRM (the concrete case the plan's own example — `open_app` —
argues for); CONFIRM stays CONFIRM; READ stays READ, still audited (every
invocation already is, per the ladder's existing discipline), never gated. If
a future READ-tier skill turns out to have exfiltration-relevant behavior
despite the "free/reversible" declaration, that is a tier-*classification*
review problem to catch when the skill is written, not something the
Claude-floor should be relied on to compensate for after the fact.

---

## 5. The "no conversation history" line is doing more work than the plan states, and should be pinned as an invariant — MEDIUM, forward-looking

The plan is explicit and, as scoped for M2, correct: *"Minimal context...
the transcript, plus the list of registered skill names and their
descriptions... No file contents, no conversation history, no machine
details in the first version."* Because no mail/calendar/web skill exists
yet, there is no live path for outside content to reach "the transcript"
today, so this finding is not exploitable in M2 — flagging it because item 1
of the task specifically asked me to consider it, and because it is exactly
the kind of thing that "seems obviously safe to relax later" without anyone
re-deriving why it wasn't relaxed originally.

The concrete failure mode, traced through the actual `reveal()` contract: a
future `trusts_input=True` skill (mail-summary, say) is licensed to
`reveal()` an email body specifically "to inform the `Reply` it returns,"
and explicitly *not* "to construct... a different tool call" or "to decide
what to do next" (`nova/security/untrusted.py:136-142`,
`docs/SECURITY_SPEC.md` §1.2). The moment `reveal()` returns, the value is a
bare `str` — its `Untrusted` provenance is gone, by design, so it can become
spoken output. If that spoken `Reply.text` is later stored as "conversation
history" and replayed into a subsequent Claude fallthrough call as context,
an attacker who controls the email body has just turned "inform the Reply"
into "decide what to do next," one turn later, via Claude — precisely the
operation the `reveal()` license forbids, and Claude is neither the router
nor the parameter binder, the two places `docs/SECURITY_SPEC.md` §1.3 layer
3's AST test actually looks. That enforcement layer would stay green while
the promise it's meant to prove breaks — the same "check is true about the
surface, silent about the delegate" shape as ADR 0010, one level further out.

**Recommendation:** keep "no conversation history" as a hard, named
invariant (an ADR, or a line in `docs/CONTRACTS.md`'s eventual T2.4 section),
not an implementation detail of "the first version." If it's ever relaxed,
the design question that has to be answered explicitly at that point is:
does any prior turn's `Reply.text` that originated from a `trusts_input=True`
handler get excluded from what's replayed to Claude, or does it need to stay
`Untrusted`-wrapped (with Claude added as a third sanctioned — and
structurally distinct from "inform a Reply" — consumer of `reveal()`,
requiring its own review)? Don't let that get decided implicitly by whoever
next wants better multi-turn continuity.

---

## 6. Offline/hang/garbage-output handling: kill, don't abandon — MEDIUM–HIGH (severity depends on finding 1)

The plan states the right instinct ("Offline, NOVA must say so plainly — not
fail silently, not hang") without pinning down mechanism. The codebase
already has a proven pattern for "a blocking call that might never return":
`ConsoleConfirmChannel`/`DialogConfirmChannel` run the blocking operation on
a `daemon=True` thread and abandon it on timeout (documented at length in
`nova/security/confirm.py`, hardened against F30). **That pattern is safe
there specifically because the abandoned thing is a blocked read with no
side effects of its own.** A `claude` child process is not that: it is a
running process which — especially if finding 1 isn't resolved cleanly, but
even in principle — could still be doing things (writing output, holding
connections, spawning its own children) after NOVA has stopped waiting on it.
Abandoning a hung `claude` child on timeout, the way a hung `readline()` is
safely abandoned, is the wrong pattern here.

**Required:** a hard wall-clock timeout on the full round trip; on timeout,
**kill the child process (and its process tree)**, not merely stop
awaiting it; a response that fails to parse as `answer(text)` or
`propose(skill_name, params)` is a refusal, spoken plainly, never
partially or best-effort executed; absence of the `claude` binary
(`find_cli()`-shaped lookup, per `docs/SECURITY_SPEC.md` §3.2's reserved
allowlist row) is a plain spoken failure, never a silent fall-through to
guessing locally with the router's second-best score — that would be a
confidence-honesty violation with an unusual trigger, but the same violation.
No automatic retry loop: a single attempt per turn, explicit spoken failure
on any of the above, and Manav can simply ask again (which naturally starts
a fresh turn) rather than NOVA silently re-invoking a paid API call on his
behalf.

---

## 7. Signature/param validation happens after CONFIRM is already shown, not before — MEDIUM

`SkillSpec.invoke()` (`registry.py:126-154`) does, in order: the layer-2
`Untrusted` guard, then `ctx.ladder.authorize()` (which, for CONFIRM tier,
can mean a real dialog on Manav's screen), then — only inside `self.handler(
ctx, **params)` — Python's own call-time keyword-argument binding, which is
what actually rejects an unexpected or missing parameter name with
`TypeError`. Today this never matters: the only caller is `Router.route()`,
whose own `_extract_params` (`nova/brain/router.py:247-269`) guarantees
exactly the one correct parameter name a skill declares.

Once Claude's `propose(skill_name, params)` is a second caller supplying an
arbitrary `params` dict, this ordering means a malformed proposal (extra
keys, wrong types, a typo'd param name) can still reach `authorize()` and —
for an ACT/CONFIRM-tier skill — put a **real dialog in front of Manav**
before the eventual `TypeError` from the handler call ever fires. That's not
a bypass (the call still fails), but it is a way to spend Manav's attention
on a CONFIRM prompt for something that was never going to execute — exactly
the fatigue/erosion risk named in finding 4, from a different angle, and one
Claude (or content that influences it) doesn't even need to be trying to
exploit; a slightly malformed proposal is enough.

**Recommendation:** bind params against the handler's real signature (e.g.
`inspect.signature(handler).bind(**params)`) explicitly, at the top of
`invoke()`, before the `Untrusted` guard and well before `authorize()` — so
"the same binder validates the params" (as the plan already claims) is true
structurally and early, not true only once the handler eventually gets
called after a dialog has already been shown.

---

## 8. Hygiene items — real, not blocking, cheap to fold in now

- **Route the entire outbound payload — transcript *and* the skill-name/
  description list — through `Egress.release_for_process()`'s `redact()`,
  not just the transcript.** Skill descriptions are developer-authored today,
  but nothing enforces that they stay that way, and passing everything
  through the one shared redactor is free defense-in-depth consistent with
  "one shared implementation, no copies."
- **Environment/argv discipline** — `ANTHROPIC_API_KEY`/
  `CLAUDE_CODE_USE_BEDROCK`/`CLAUDE_CODE_USE_VERTEX` scrubbed from the child
  env, OAuth token via env never argv, prompt text on stdin via
  `release_for_process` never argv, every spelling of
  `--dangerously-skip-permissions`/`bypassPermissions`/`dontAsk`/`yolo`
  refused before spawn — already specified precisely in
  `docs/THREAT_MODEL.md` §2.2(5) and still correct. Not relitigated here; it
  needs to move from that document into a `docs/CONTRACTS.md` T2.4 section
  and real tests before this lands, the same way T0.5's amendments did,
  rather than staying a paragraph someone has to remember to re-read.
- **`argv[0]` must resolve through a `find_cli()`-shaped function, never a
  raw config/parameter string** — already specified in
  `docs/SECURITY_SPEC.md` §3.2's reserved allowlist row; still the right
  call, restated because it's load-bearing for the AST test's literal-argv
  technique (`test_gpu_py_nvidia_smi_argv0_is_a_literal_string_not_a_
  variable`'s pattern, applied to the fourth allowlist entry when it lands).
- **The registry has no runtime mutator** (`nova/skills/registry.py`:
  discovery is import-time-only, confirmed by reading the whole file — no
  `register_skill_at_runtime`-shaped function exists), so "an unrecognised
  `skill_name` is a refusal, not an attempt" is checking against a set that
  cannot move mid-process. Checked, not just assumed; no finding here, but
  worth recording that this specific TOCTOU shape was considered and closed
  by the existing design, not overlooked.
- **Audit a Claude-proposed-unknown-skill-name refusal as its own event
  kind** (e.g. `"claude.propose_unknown_skill"`), distinct from an ordinary
  ladder denial — a pattern of repeated unknown-name proposals (from Claude
  itself misbehaving, or from injected context steering it) is exactly the
  kind of thing that should be visible to whoever reviews the log later,
  same reasoning as every other "denial must itself be audited and loud"
  requirement already in this codebase.
- **Audit records should carry both the declared and the effective tier**
  once finding 2's floor exists (e.g. `declared_tier`, `effective_tier`,
  `origin`), not just one `tier` field — otherwise the log can't distinguish
  "ACT, elevated to CONFIRM because Claude proposed it" from "this skill's
  declaration changed," which matters for anyone debugging or auditing later.

---

## 9. Answering the lead's five questions directly

1. **Is the proposal channel a real boundary or does it move the problem?**
   Structurally real for M2's actual threat surface — no skill exists yet
   that lets outside content reach "the transcript," the registry can't
   change shape mid-process, and the layer-2 `Untrusted`/binder guards
   already apply to whatever params a proposal carries. It stops being real,
   and starts moving the problem, if any one of findings 1, 2, or 3 ships
   unresolved: an unconstrained child process (1) doesn't need the proposal
   channel at all to act; a floor that can't be expressed (2) makes "gets
   confirmed" untrue; a CONFIRM dialog that can't show the real disclosure
   (3) makes "confirmed" mean less than it should. Fix those three and the
   boundary holds as designed.
2. **Is `max(declared, CONFIRM)` right or theatre?** Right shape, needs the
   READ carve-out (finding 4) or it becomes exactly the kind of over-broad
   friction the lead already named as the risk of getting disabled — and it
   isn't implementable at all yet (finding 2) without adding the floor
   mechanism and an ordering to `Tier`.
3. **The egress boundary, now with a real caller — sufficient?**
   `release_for_process()` as implemented (`egress.py:180-199`) matches
   `docs/SECURITY_SPEC.md` §2.2 exactly: redacts, audits lengths and a
   boolean never the text, raises on write failure, no caller-identity
   check by design (the AST test is what restricts who can reach it). That
   half is sufficient. It was never meant to cover what the spawned process
   does with its own tools — `ARCHITECTURE.md` already marks the Claude arm
   "Always CONFIRM tier" for exactly this reason, and CONFIRM (not Egress)
   is supposed to be the control for that risk. Finding 1 is the question
   of whether the *process itself* has anything CONFIRM would even need to
   catch, and finding 3 is whether CONFIRM, once triggered, discloses enough
   to be meaningful consent.
4. **What must never be sent, concretely?** No file contents, no
   conversation history (finding 5), no machine details — as the plan
   already states; extend the existing `redact()` pass to the *entire*
   outbound payload including skill descriptions (finding 8), not just the
   transcript; hold the environment/argv discipline from
   `docs/THREAT_MODEL.md` §2.2(5) as a hard requirement, pinned into
   `docs/CONTRACTS.md` before this lands.
5. **Offline/failure behaviour as a security question?** Covered in finding
   6: kill on timeout (not the confirm-channels' safe-there-but-wrong-here
   abandon pattern), refusal on unparseable output, plain spoken failure on
   a missing binary, no silent local fallback, no automatic retry.

**On T2.3 specifically:** the resolve/CONFIRM/realpath-revalidate shape
(T2.3a) is sound and can proceed once finding 3's disclosure gap is fixed —
it affects `open_app`'s CONFIRM prompt identically whether the trigger is
voice-approval or a Claude proposal. Blocking voice-approval (T2.3b) for
acoustically-armed turns is the right call, but is not yet a *control* — see
finding 2's consequence section: it's true today only because wake-word
happens to be unwired, and needs the same shared floor mechanism to remain
true once that changes.

---

## 10. Requirements list, for nova-dev / nova-tester

Not implementing or testing any of this myself, per role. In rough landing
order:

1. Decide and prove (real-subprocess test) that T2.4's `claude` invocation
   runs with no tool-use capability at all. (Finding 1.)
2. Add a `Tier` ordering and a `floor` input to `Ladder.authorize()` (or
   equivalent), generic enough for both this feature and F28's presence
   floor to share; scope T2.4's floor to skip READ. (Findings 2, 4.)
3. Give `open_app` (and any future CONFIRM-worthy skill) a pre-authorize
   resolve step so what's audited/shown at CONFIRM time is the concrete,
   resolved thing being consented to — handler still re-validates at call
   time regardless. (Finding 3.)
4. Bind `params` against the handler's real signature at the top of
   `SkillSpec.invoke()`, before the `Untrusted` guard and `authorize()`.
   (Finding 7.)
5. Write `docs/CONTRACTS.md`'s T2.4 section covering: the env/argv
   discipline from `docs/THREAT_MODEL.md` §2.2(5), `find_cli()`, the
   redact-everything-outbound rule, the kill-on-timeout / refuse-on-garbage
   / no-retry failure contract, and the hard "no conversation history"
   invariant — before implementation starts, not after. (Findings 5, 6, 8.)
6. Audit events: `claude.propose_unknown_skill` on a refused unrecognized
   name; `declared_tier`/`effective_tier`/`origin` on any elevated-tier
   decision. (Finding 8.)

A test proving a Claude-proposed ACT-tier call is refused/escalated to
CONFIRM while a locally-routed one isn't — mirroring the test
`docs/THREAT_MODEL_ALWAYS_ON.md` §6 already asked for on the wake-word side
— is the end-to-end proof this whole document is asking for; both should
exist before either ships.
