# NOVA — Threat model addendum: the always-on transition

Authored by nova-security. Companion to `docs/THREAT_MODEL.md`, which
scoped M0 and explicitly deferred this exact question in §1.5:

> M1 adds an always-on wake word with no speaker verification, at which
> point "anyone in earshot" becomes the trust boundary for ACT-tier
> actions, not "anyone at the keyboard." Not blocking M0; should be
> revisited before M1 ships wake-word-triggered ACT actions.

This document is that revisit, done before M1 wires `nova/audio/wake.py`
into `nova/app.py` rather than after. Grounded against what's actually on
disk today: `WakeWord` is fully implemented and exported
(`nova/audio/__init__.py`) but **not called anywhere** outside its own
module, `nova/config.py`, and this analysis — confirmed by
`grep -rln "WakeWord" nova/`. Everything below describes what changes the
moment that wiring lands, checked against the code that exists now, not
assumed from the roadmap.

---

## 1. What actually changes

### 1.1 The assumption that quietly did double duty

`docs/CONTRACTS.md` T1.1 states the design goal plainly:

> **Wake and push-to-talk are interchangeable arming sources.** The turn
> loop must not care which fired. That interchangeability is the design
> goal — the wake word is a second door into the same room, not a parallel
> pipeline.

Checked against `nova/app.py`: `_on_ptt_start`/`_on_ptt_end` subscribe to
`TOPIC_PTT_START`/`TOPIC_PTT_END` and call `run_turn(pcm, source=f"ptt:{event.mode}")`.
The `source` string is used exactly once downstream — in a log line
(`app.py:238`, `_log.info("Turn (%s): heard %r", source, text)`). It is
**never read by `Ladder.authorize()`**: `nova/security/ladder.py:112`
derives `tier = skill.tier` from the skill's static declaration alone, and
`SkillCtx` (`nova/skills/registry.py:87-103`) carries `config` and `ladder`,
nothing about how the turn was armed. A wake-armed `_on_wake_start`/
`_on_wake_end` pair, built the way CONTRACTS.md instructs ("must not
care which fired"), would call the identical `run_turn()` with the
identical downstream path. There is no seam anywhere in the ladder for
"how was this turn armed" to matter, because none was ever built — M0 had
no reason to build one, since PTT's physical key-press *was* the presence
check, folded invisibly into "a turn started."

That's the double duty: the permission ladder's ACT tier was never
literally specified as "physical presence + logged," but it was
*calibrated* under a world where every turn implied one. `Ladder`'s own
docstring (`nova/security/ladder.py:1-27`) frames ACT vs. CONFIRM purely in
terms of "logged" vs. "logged and approved," with no third axis for *how
the turn came to exist at all*. Nothing in the ladder's code will break,
fail a test, or need to change for wake-word arming to reach ACT the
instant it's wired in — which is exactly the risk. This is the same shape
as the invariant register I reported yesterday: a promise that held simply
because nothing had yet exercised the gap.

### 1.2 Concretely: `open_app`

`open_app` (`nova/skills/open_app.py`) is ACT tier, chosen under M0's PTT
assumption (`docs/THREAT_MODEL.md` §1.1, written when PTT was the only
arming source that existed). Its own module docstring calls it "arbitrary
code execution wearing a hat." Under `Ladder.authorize()`
(`ladder.py:134-135`), ACT resolves to `_allow_with_required_audit` —
audited, never asked. Today that's safe because the only way to reach it
is a human's hand on `Ctrl+Alt+Space`. Once wake.py is wired in per
CONTRACTS.md's stated design, the exact same call reaches `subprocess.Popen`
gated only by:

- the wake detector's threshold firing (§4 below — a tunable, not a wall),
- STT transcribing *something* that resembles "open <allowlisted app
  name>" (the allowlist itself, `config.security.allowed_apps`, is a
  real and correctly-implemented control — see `docs/THREAT_MODEL.md`
  §1.1 — but it constrains *which app*, not *who may ask*),
- the router's confidence floor (a real, load-bearing mitigant — see §1.3),
- an audit record written *after* the process has already launched.

No confirmation step exists between "something in the room said something
plausible" and Notepad, Calculator, or Task Manager opening. On a machine
that (per `CLAUDE.md`) "drives Claude Code to build software on command,"
`open_app` is today's smallest example of a much larger category M4
introduces deliberately as CONFIRM-tier — this document is about whether
`open_app` should have stayed ACT once the presence assumption it was
priced under disappears.

### 1.3 Does READ/ACT/CONFIRM still partition correctly?

Mechanically: yes, unchanged. Every tier's code path is source-agnostic by
construction and none of it needs to change for wake-word turns to work at
all — that's the problem, not evidence against one. The confidence-floor
clarification path (`nova/brain/router.py`, `docs/THREAT_MODEL.md` §1.6) is
a real, independent mitigant that survives the transition untouched: it
doesn't know or care how the turn was armed, and gibberish or an
off-vocabulary utterance from a TV still routes to a spoken clarifying
question rather than a guess, regardless of arming source. That is a
genuine backstop, not nothing — but it is calibrated (`confidence_floor
= 0.42`, F15) against a two-skill vocabulary that includes exactly the
plain-English shape ("open notepad", "launch calculator") a TV ad, a video
call, or a stranger is entirely capable of producing by accident. The
floor filters noise; it does not filter *sense*. A clearly-spoken "open
task manager" from any source clears it exactly as intended.

**Verdict for this sub-question:** the tier partition is intact as *code*.
As a *security property*, ACT quietly redefines itself from "a human
performed a deliberate physical act, and we log what they asked for" to
"something in the room produced clear enough speech, and we log what it
asked for" — a strictly weaker claim, delivered by unchanged code, the
moment the arming source changes underneath it. This is exactly the
"invariant asserted in prose [an implicit design assumption], unenforced
in code" shape from the prior review, one layer up: not a missing check
on a stated rule, but a stated rule (ACT = logged, not confirmed) whose
unstated precondition (a human pressed a key to get here) silently stops
holding.

### 1.4 A second, smaller change: how much of the room reaches the log

`app.py:238` logs the full STT transcript at INFO for every turn,
unconditionally: `_log.info("Turn (%s): heard %r", source, text)`. This is
not new to M1 — PTT-armed turns already do this — but PTT bounds *how
often* it happens to "whenever Manav deliberately pressed the hotkey."
Wake-word arming removes that bound: every time the detector fires,
including false/nuisance triggers from a TV or ambient noise, whatever the
mic heard during that armed window gets transcribed and written verbatim
to a local plaintext log (`nova/log.py`; explicitly **not** the audit log
— `nova/security/audit.py:114-116`'s own docstring: "Not a general-purpose
logger — see nova.log for that," so none of `AuditLog`'s redaction/
sanitization applies here). `nova.interfaces.hotkey`'s own module docstring
already names the exact concern this generalizes: a log file "is a file
that later gets attached to a bug report." Wake-word arming multiplies how
often room audio the household never intended for NOVA lands there. Local-
only, so it does not touch the constitution's egress rule, but it is a
real increase in what's retained and worth a one-line mitigation: cap or
truncate wake-armed transcript logging the same way `AuditLog._sanitize`
already caps string length elsewhere, or gate full-transcript logging
behind a debug flag once M1 ships. Minor relative to §2–§3, flagged for
completeness since it's a direct consequence of the same wiring change.

---

## 2. The television adversary

### 2.1 The prior art is exact, and it isn't ambient folklore

The retired build hit this precisely and documented it
(`_OLD_ARCHIVE/KNOWN_ISSUES.md:324-355`, "Echo cancellation is the least
certain component in the whole system"):

> Laptop speakers are ~15 cm from the laptop microphone. When NOVA talks,
> NOVA hears itself at roughly the same level as you. Without echo
> cancellation the VAD fires on NOVA's own voice, the pipeline calls
> barge-in, NOVA cuts itself off — and then the wake detector gets a clean
> shot at the word "nova" inside NOVA's own reply. That failure mode is a
> loop, not a glitch.

The retired build's mitigation was `_OLD_ARCHIVE/nova/audio/aec.py`: two
full-duplex backends (WebRTC APM, Speex MDF) neither of which had a
reliable Windows wheel, so it shipped **half-duplex** by default — mute
the mic entirely while the speaker is active, unmute after a 180ms
hangover. Documented tradeoff: "bulletproof, costs nothing, and VOICE
BARGE-IN DOES NOT WORK." Interruption still worked, but only via the
hotkey, which cancels playback inline and re-arms the mic directly — not
through any acoustic path.

### 2.2 What's actually landed today: nothing

`grep -rn "aec\|duck" nova/ --include=*.py` (and a direct file search)
finds **no `nova/audio/aec.py` and no ducking module of any kind** in the
current tree. `docs/ROADMAP.md` M1 line 34 plans to port this — "half-duplex
ducking + barge-in via hotkey; full-duplex AEC only if a Windows wheel
actually installs" — but that is scoped, unimplemented work, not landed
protection. I checked every place a mute-while-speaking coordination hook
could plausibly live and found none:

- `nova/audio/wake.py`: `process()` has zero awareness of playback state —
  no reference to `Player`, no bus subscription to anything TTS-related.
- `nova/audio/player.py`: `Player.play()`/`cancel()` know nothing about
  `Capture` or `WakeWord` — no signal is published when playback starts or
  stops.
- `nova/app.py`: `_speak()` calls `self.synth.say()` then
  `self.player.play()` with no coordination with `self._capture` or a wake
  detector at all (M0 has none to coordinate with, correctly, since PTT
  makes it moot — see §2.3).

### 2.3 Why this hasn't bitten yet, and why that's about to stop being true

Under PTT, `_on_ptt_end` calls `self._capture.stop()`
(`app.py:194-195`) **before** `run_turn()` — including before `_speak()`
runs. The mic is not open at all while NOVA talks, for the entire M0
design. This is real protection today, but it is a side effect of PTT's
shape (the mic is only ever open inside an explicit human-armed window),
not a purpose-built anti-echo control — there is no code anywhere that
says "mute because we are speaking," only "the human let go of the key."

Wake-word arming requires the opposite by definition: something must
continuously feed `WakeWord.process()` so it can hear the wake phrase at
all, which means a `Capture` stream (or an equivalent) has to be running
*during* `Player.play()`, not just between turns. The moment that wiring
lands with no AEC/ducking module present, NOVA reproduces the retired
build's exact documented failure with zero mitigation, not partial
mitigation: it will hear its own playback at full level, unattenuated, on
every reply.

### 2.4 Two concrete loops, one bounded, one unacceptable

**Self-loop (NOVA hears NOVA):** the wake phrase, or a plausible near-match
to it, appearing anywhere in NOVA's own synthesized speech — quoting a
command back, apologizing, saying its own name — re-arms mid-reply.
`WakeWord`'s own failsafes genuinely bound the damage here:
`DEFAULT_ARM_TIMEOUT_MS = 15_000` puts a hard ceiling on any one false-armed
turn, `asyncio.Lock` in `app.py:118` (`self._turn_lock`) serializes turns so
nothing overlaps, and VAD endpointing (`DEFAULT_VAD_SILENCE_MS = 800`) ends
a turn once whatever it heard stops. Worst case this degrades to an
annoying, self-limited "conversation with itself" for a few turns, not
unbounded resource exhaustion — I want to be precise that this specific
sub-case is a UX failure with a real ceiling, not a runaway process.

**External-source loop (a TV, a video call, a smart speaker, a person who
isn't Manav):** this is the one that matters for the ladder question in
§3. No self-referential wording is required — any external sound that
happens to contain the wake phrase followed by clear command-shaped
speech ("...jarvis, open outlook") arms and routes exactly as if Manav
said it, because nothing downstream of the wake detector knows or checks
the difference. Unlike the self-loop, there is no failsafe that bounds
*this* one — arm-timeout and turn-serialization bound how long a single
false arm lasts, not whether an external source's speech gets to
authorize an ACT-tier action once it does arm. This is "the television,"
exactly as posed: no adversarial cleverness, just an ordinary broadcast
or ambient sentence that happens to parse.

**Verdict:** wiring `wake.py` into `app.py` before an AEC/ducking control
lands is not a UX rough edge to demo around — it reproduces a failure mode
the team has already seen once, in detail, with a known fix that simply
isn't built yet. I'd treat "some mute-while-speaking mechanism exists" as
a precondition for landing the wiring, not a nice-to-have that ships
alongside it. The self-loop case (§2.4a) is tolerable to ship without it
and fix reactively; the external-source case, combined with §3's ACT-tier
question, is not.

---

## 3. Should wake-word arming be allowed to reach ACT tier?

I'll argue a position, as asked, rather than lay out both sides and stop.

**Recommendation: acoustic-only arming should cap at READ until something
re-establishes that a human — specifically Manav — is present. ACT/CONFIRM
actions triggered from a wake-armed turn should require an added factor
before they execute.**

### 3.1 Why

The constitution's own framing of what NOVA is: it "hears, speaks,
controls the system and apps, drives Claude Code to build software on
command, and manages Manav's digital life — all locally." `open_app`'s
own docstring calls it "arbitrary code execution wearing a hat," and it is
the *smallest* example of the class of actions this machine can take — M2
adds filesystem writes, M4 adds a supervised Claude Code process with real
repo access. The physical-presence factor PTT provided wasn't
incidental to how ACT got calibrated as "logged, not confirmed" — it was
the reason that calibration was safe. `docs/THREAT_MODEL.md` §1.5 flagged
this as the exact question to revisit before shipping wake-word-triggered
ACT actions; I think the answer it was gesturing at is correct: "anyone in
earshot" is not an acceptable trust boundary for a tier whose whole design
point is *not* asking permission.

### 3.2 The counter-argument, stated fairly

Capping every ACT action at CONFIRM for wake-armed turns partially defeats
M1's own value proposition — "NOVA runs all day in the tray, answers to
the wake word" (`docs/ROADMAP.md` M1 exit demo) reads differently if every
practical action still needs a spoken or physical confirmation, which
starts to look like PTT with extra steps rather than a hands-free
assistant. That's a real cost, not a strawman, and it's the reason I'm
recommending a mechanism rather than a blanket rule.

### 3.3 A concrete mechanism, not just a verdict

The pattern is already half-built and already named in the docs, just
never pointed at this input. `nova/security/ladder.py:21-27` already
states the combination rule for a future per-call tier signal: "it must be
combined as `max(declared, computed)` — a call can only end up more
restricted than its declaration, never less." That's precisely the shape
needed here, with the arming source as the second input:

1. Thread the arming source through — a field on `SkillCtx` or an argument
   to `Ladder.authorize()`, e.g. `presence: Literal["physical", "acoustic"]`,
   set by whatever handler builds the `SkillCtx` for a given turn
   (`_on_ptt_end` → `"physical"`, the future `_on_wake_end` → `"acoustic"`).
2. In `Ladder.authorize()`, compute an effective tier as
   `max(skill.tier, floor_for(presence))`, where `floor_for("acoustic")`
   pins ACT up to CONFIRM and leaves READ untouched (READ was already
   "free" — no presence check ever gated it, and shouldn't start now).
   `floor_for("physical")` is a no-op — physical presence already carries
   what PTT always assumed.
3. Route the resulting CONFIRM through the channel infrastructure that
   already exists (`nova/security/confirm.py`, `ConsoleConfirmChannel`) —
   this is not new plumbing, just a new caller of an existing seam that
   already fails closed with no channel registered.

This gets M1 most of its hands-free value (READ-tier queries — "what
time is it" — stay instant and unconfirmed exactly as designed) while
keeping the one property that actually matters: nothing that spawns a
process or touches the filesystem executes because a television said
something plausible. The cost lands exactly where the risk does.

### 3.4 What I'm not deciding

Whether the added factor should be "press the hotkey to confirm" (cheapest,
correctly conservative, re-uses the one component already proven to imply
physical presence) or a spoken yes/no through a CONFIRM channel (closer to
the hands-free goal, more plumbing, and reintroduces exactly the self-loop
risk from §2.4 if NOVA's own confirmation prompt can itself be
acoustically spoofed — "NOVA, yes" from a TV is not obviously harder to
produce than the original command) is a UX and architecture call for the
lead and whoever designs M1's tray/confirm work, not something I can
settle from a security review alone. I'd flag the spoken-yes option as
needing its own scrutiny before it's chosen for exactly that
reason — it doesn't obviously solve the problem it's meant to solve.
Also undecided: whether this floor applies to *every* future ACT skill or
just to `open_app` specifically (today it's moot — `open_app` is the only
ACT-tier skill that exists) — worth revisiting explicitly at M2 when new
ACT-tier skills (windows, volume, clipboard) are designed, since some of
those plausibly belong at a different risk tier than "launch a process."

---

## 4. False-accept rate as a security parameter, not a UX one

The measured numbers (0.997 detection on "hey jarvis", 0.00005 false-accept
on a control phrase, zero detections across ~15s of noise) are good and I
have no reason to doubt them as measurements. The problem is what they're
measurements *of*: behavior at one specific threshold value, and that
value is exposed to Manav as `WakeConfig.threshold`
(`nova/config.py:100`), documented in its own comment as "the one knob
Manav will actually tune."

Checked `nova/config.py`'s validation path (`_check_value`,
confirmed by code reading — it performs `isinstance` type checking only,
with the existing bool-vs-int guard for numeric fields, and nothing else):
**there is no range clamp on `threshold` at all.** A value of `0.01` loads
exactly as validly as `0.55` or `0.9`. Nothing in config loading, `WakeWord`
construction, or `WakeWord.start()` rejects, warns about, or clamps an
implausibly low threshold — it flows straight into
`score >= self._threshold` (`wake.py:364`) and changes the detector's
real-world false-accept rate with no floor.

This matters specifically because of §3: if wake-armed ACT actions ship
without the mitigation in §3.3, the false-accept rate *is* the access
control for every ACT-tier action in the house. If §3.3 lands, a lower
threshold "only" means more nuisance CONFIRM prompts and more of §1.4's
transcript-logging exposure — still worth a floor, but a materially
smaller stake than "threshold is the whole ACT gate."

**Recommendation:** the config comment already correctly warns Manav this
is "the one knob he'll actually tune" — it should also warn him what
tuning it down costs, at the point of the edit, not buried in this
document. Concretely: (a) a documented, empirically-chosen minimum in
`config.py`'s validation for `WakeConfig.threshold` (I'm not picking the
number — that's a measurement task for whoever owns the false-accept
curve, not something to invent in a threat model) that logs a loud warning
or refuses to boot below it, mirroring the pattern `Egress.__init__`
already uses for a malformed allowlist entry ("a malformed entry is a hard
failure at boot, never a silently-dropped entry"); (b) the config comment
itself states the tradeoff plainly — "lowering this increases both false
wake-ups and the population of ordinary sounds that can act on your
behalf" — so the person editing `nova.yaml` to fix a missed detection sees
the security cost in the same place they see the UX one.

---

## 5. Summary verdict

This is not over-thought. Two of the four questions resolve to concrete,
pre-existing gaps rather than speculative risk:

1. **§1/§3 — ACT tier and presence.** Real. The ladder's ACT/CONFIRM split
   was calibrated under an assumption (physical presence) that wake-word
   arming removes without touching a single line of ladder code. I
   recommend capping acoustic-only arming at READ until an added factor
   re-establishes presence, using the `max(declared, computed)` pattern
   already named (but unused) in `ladder.py`'s own docstring. This is a
   judgment call the lead should make deliberately, but I don't think
   "ship it as-is" is defensible for `open_app` specifically given what
   the skill's own docstring already says about itself.
2. **§2 — the television.** Not a risk to weigh, a reproduction of an
   already-documented failure. The retired build hit this, wrote it down
   in detail, and shipped a fix (half-duplex mute). The current build has
   ported the wake detector but not the fix. **I'd treat "a mute/ducking
   mechanism exists, even the cheap half-duplex one" as a precondition for
   wiring `wake.py` into `app.py`'s turn loop at all**, not a follow-up
   task — landing wake-word arming without it is landing a known bug on
   purpose.
3. **§1.4 — transcript logging volume.** Real but minor; a one-line
   mitigation (cap/gate full-transcript logging for wake-armed turns),
   not a blocker.
4. **§4 — threshold as a security parameter.** Real, and cheap to fix: a
   documented, enforced floor plus a comment that states the security cost
   next to the UX one, at the exact place Manav will edit it.

Nothing here says M1 shouldn't ship a wake word. It says two specific,
narrow things need to land alongside it — a mute/ducking control, and a
decision about ACT-tier gating for acoustic arming — rather than being
discovered afterward the way `docs/THREAT_MODEL.md` §1.5 explicitly warned
they would be if left unrevisited.

---

## 6. For nova-dev / nova-tester, if the lead accepts this

Not implementing or testing any of this myself, per my role — these are
requirements, not a design I'm handing over as final:

- Some playback-aware mute/ducking control between `Player` and
  `WakeWord`/`Capture` before `wake.py` is wired into `app.py`. Even the
  retired build's "bulletproof, no barge-in" half-duplex approach (mute
  during `Player.play()`, short hangover after) closes §2's gap; full AEC
  is not required to ship M1 safely, only *some* mute-while-speaking
  discipline is.
- An arming-source signal threaded from `_on_ptt_end`/(future)
  `_on_wake_end` through `SkillCtx` into `Ladder.authorize()`, and a
  `floor_for(presence)` combined via `max(declared, computed)` exactly as
  `ladder.py`'s own docstring already specifies for a future per-call tier
  signal — this is that future, arriving now.
- A test proving a wake-armed `open_app` call is refused/escalated to
  CONFIRM while a PTT-armed one isn't, once §3.3 lands — this is the kind
  of "confirm path reachable and tested end-to-end" requirement `CLAUDE.md`
  already states as non-negotiable for anything gated by CONFIRM.
- A documented minimum for `WakeConfig.threshold`, enforced in
  `nova/config.py`'s validation, with the number chosen from real
  false-accept measurement, not invented here.
- A cap or gate on full-transcript logging (`app.py:238`) for wake-armed
  turns specifically, or a decision that the existing behavior is
  acceptable and why.
