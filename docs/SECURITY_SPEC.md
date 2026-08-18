# NOVA — Security kernel implementation spec (T0.S2)

Companion to `docs/THREAT_MODEL.md`, which established *what* `Untrusted` and
`Egress` must achieve and *why*. This document is the *how*, precise enough
that a dev implementing T0.5 has no invention left to do. Where this spec and
`docs/CONTRACTS.md` disagree, `CONTRACTS.md` is the seam — flag the conflict
rather than picking one silently.

Grounded against what's actually landed: `nova/security/__init__.py` and
`nova/skills/__init__.py` are still empty. `nova/stt/gpu.py:61` already spawns
a subprocess (`nvidia-smi`) — this exists on disk today and the AST test
spec'd here must not break it; it's folded into the allowlist below rather
than treated as a hypothetical.

---

## 1. `Untrusted[T]` — full type design

**File:** `nova/security/untrusted.py`

### 1.1 What it wraps and what it refuses

```python
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?$")  # e.g. "mail.body", "web.fetch"


@dataclass(frozen=True, slots=True, eq=False)
class Untrusted(Generic[T]):
    """A value that came from outside NOVA's control. Not a str subclass —
    that is the whole point (see docs/THREAT_MODEL.md §3). Passing this where
    a plain str/int/etc. is expected fails immediately and loudly, because
    this class deliberately implements none of the protocols that would let
    it stand in for its wrapped value.
    """

    _value: T
    source: str  # "mail.body", "calendar.invite", "web.fetch", "document.text", ...

    def __post_init__(self) -> None:
        if isinstance(self._value, Untrusted):
            raise TypeError("Untrusted may not wrap an Untrusted (no re-labelling to hide provenance)")
        if not _SOURCE_RE.match(self.source):
            raise ValueError(f"Untrusted.source must be a dotted lowercase tag, got {self.source!r}")

    # -- deliberately NOT implemented: __add__, __radd__, __contains__,
    # -- __iter__, __getitem__, __bool__. Each is a way "just use it a little"
    # -- would work by accident. Absence is the enforcement; do not add any
    # -- of these later without re-reading docs/THREAT_MODEL.md §3.

    def __len__(self) -> int:
        raise TypeError("len(Untrusted) is not defined — call reveal() explicitly if you need the content")

    def __repr__(self) -> str:
        return f"Untrusted(source={self.source!r}, digest={self._digest()})"

    __str__ = __repr__  # str(x) / f"{x}" / "..".format(x) all return the LABEL, never the content

    def __eq__(self, other: object) -> bool:
        # Equality is identity-of-provenance, not content comparison — two
        # Untrusted wrapping the same bytes from different sources are not
        # interchangeable, and this must never accidentally become a way to
        # compare against a plain str (`Untrusted(...) == "some string"` is
        # always False, never a content leak via timing or otherwise).
        if not isinstance(other, Untrusted):
            return NotImplemented
        return self.source == other.source and self._digest() == other._digest()

    def __hash__(self) -> int:
        return hash((self.source, self._digest()))

    def _digest(self) -> str:
        raw = self._value if isinstance(self._value, (bytes, bytearray)) else str(self._value).encode("utf-8", "replace")
        return hashlib.sha256(raw).hexdigest()[:12]
```

Construction is the only place provenance is assigned: `Untrusted(raw_body,
source="mail.body")`. Whatever module first receives outside content (the
mail client, the calendar client, the web fetcher, the document reader) wraps
it there, at the boundary, before it touches anything else in NOVA. There is
no "upgrade" path from `str` to `Untrusted` anywhere else — if a module needs
to treat something as untrusted, it wraps it itself; it never receives a
pre-wrapped value from a function whose signature promised `str`.

### 1.2 `reveal()` — who may call it, what gets audited

**File:** same module.

```python
from nova.security.audit import AuditLog  # forward reference; wire at boot

_AUDIT: AuditLog | None = None


def bind_audit(audit: AuditLog) -> None:
    """Called once by the security kernel's boot sequence. Before this is
    called, reveal() still works (fails open on missing audit, not on the
    unwrap itself) but logs a WARNING naming the gap every time — matching
    the "every module degrades loudly" cross-cutting rule. This is a narrower
    exception than the Ladder's audit-failure-means-deny rule: reveal() has
    no side effect on the world by itself, only what the caller does with the
    text does, so it fails open here and the caller's own tool-call path is
    what must fail closed.
    """
    global _AUDIT
    _AUDIT = audit


_CALLER_RE = re.compile(r"^[a-z_][a-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")  # "nova.skills.mail_summary:handler"


def reveal(u: Untrusted[T], *, caller: str, purpose: str) -> T:
    """The one sanctioned way to get the wrapped value back out.

    `caller` is `"<module>:<function>"` of the call site — not free text, so
    it can't itself become a place to smuggle attacker content into the audit
    log. `purpose` is a short human-readable reason ("summarize for the user",
    "check for a meeting time"). Every call is audited with source, caller,
    purpose and a content digest — never the content itself.
    """
    if not isinstance(u, Untrusted):
        raise TypeError(f"reveal() requires an Untrusted value, got {type(u).__name__}")
    if not _CALLER_RE.match(caller):
        raise ValueError(f"reveal(caller=...) must be 'module:function', got {caller!r}")
    if not purpose.strip():
        raise ValueError("reveal(purpose=...) must not be empty")

    record = {"kind": "untrusted.reveal", "source": u.source, "caller": caller,
              "purpose": purpose, "digest": u._digest()}
    if _AUDIT is not None:
        _AUDIT.record(record)  # raises on write failure — propagates; see §1.4
    else:
        import logging
        logging.getLogger("nova.security").warning("reveal() called before bind_audit(): %s", record)

    return u._value
```

**Who may call `reveal()`, structurally, not by convention:**

1. A skill handler decorated `trusts_input=True` — this is exactly what that
   flag means: "this handler is specifically written and hostile-content
   tested to read untrusted content in order to *produce an answer*." The
   revealed text may inform the `Reply` the handler returns. It must never be
   used, inside that same handler, to construct a *different* tool call, a
   file path, a recipient, or any other action parameter — revealing content
   to summarize it and revealing content to decide what to do next are not
   the same operation, and only the first is licensed by `trusts_input=True`.
2. The reply-composition / humanizer layer, to quote or paraphrase content
   already destined for speech output — never to decide what to say *do*.

**Who may not**, enforced by §1.3's AST test: the router (intent/skill
selection) and the skill-parameter binder (whatever turns a routing decision
into `handler(ctx, **params)` kwargs). Both are named explicitly because they
are the two places a revealed value could become a tool-call parameter — that
is the one thing the constitution forbids absolutely.

### 1.3 Surviving the round trip — three enforcement layers, not one rule to remember

1. **Runtime guard at the router's public entrypoint.** Whatever function is
   the router's entry (`route(text: str, ...) -> RouteDecision` per
   `ARCHITECTURE.md`'s brain layer) starts with:
   ```python
   if isinstance(text, Untrusted):
       raise TypeError("router received Untrusted content directly — reveal() it explicitly "
                        "for an answer, or do not route on it at all")
   ```
   This is the first line of defense and it's a `TypeError` at the boundary,
   exactly as requested — not a docstring, not a code review comment.

2. **Runtime guard in the skill-parameter binder.** Every value about to be
   bound to a skill handler's kwargs is `isinstance`-checked; an `Untrusted`
   value there raises `TypeError` naming the parameter and the skill. This
   is the backstop for content that reaches the binder by some path other
   than the router (e.g. a future proactive/scheduled skill that reads a
   calendar entry directly) — the binder doesn't need to know *how* untrusted
   content arrived, only that it must never become a bound parameter.

3. **AST test, mirroring the egress/process-spawn test's technique exactly.**
   Walk `nova/brain/router.py` (or wherever the router entrypoint lives once
   T0.1's brain layer exists) and every skill module where `trusts_input` is
   `False` or absent; fail if any imports or calls `reveal`. This is
   redundant with layers 1–2 on purpose — if a future refactor accidentally
   deletes the runtime guard, the AST test still catches a `reveal()` call
   appearing somewhere it shouldn't, and vice versa.

None of this requires a skill author to remember a rule: a `trusts_input=False`
skill (the default) simply cannot import `reveal` without the AST test
failing the build, and a `trusts_input=True` skill's handler is exactly where
the constitution already requires hostile-content tests to exist before it
ships. The two requirements — "may call `reveal`" and "must have injection
tests" — are the same flag, which is why `trusts_input=True` is the right
place to hang both.

### 1.4 Interaction with the audit-failure-means-deny rule

`reveal()` propagates `AuditLog.record()`'s exception rather than swallowing
it (per §1.2, once `bind_audit` has been called). A skill handler calling
`reveal()` whose audit write fails should let that exception surface as a
failed skill invocation — the same "no record means it didn't happen, so it
doesn't get to happen" discipline `docs/CONTRACTS.md`'s amended T0.5 section
already applies to `Ladder.authorize()`. Do not add a `try/except` around
`reveal()` inside a skill handler that swallows the error and proceeds with
the revealed text anyway — that would be exactly the silent-proceed pattern
the constitution forbids, one layer up.

---

## 2. `Egress` — allowlist format, `release_for_process`, redaction rules

**File:** `nova/security/egress.py` (the one file permitted to import a
transport — see §3).

### 2.1 Allowlist format and location

Already has a config surface: `nova/config.py`'s `SecurityConfig.egress_allowlist:
list[str]`, loaded from `config/nova.yaml`'s `security.egress_allowlist`. That
type-checks as "a list of strings" — nothing more; `Egress` must not trust
that a syntactically valid `list[str]` is a semantically valid allowlist.
Normalize and validate every entry **at `Egress.__init__`**, not at config-load
time (`nova/config.py`'s job ends at "typed and structurally valid," not
"safe to use as a security boundary" — keep that seam where it is).

```python
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

def normalize_host(raw: str) -> str | None:
    """Bare hostname only: no scheme, no port, no path, no wildcard, no
    trailing dot, no leading/trailing whitespace, no IP literal. Returns the
    lowercased host, or None if `raw` doesn't parse as exactly that.
    """
    candidate = raw.strip().lower()
    if not candidate:
        return None
    # Reject scheme, path, port and wildcard shapes outright. ":" alone also
    # catches every IPv6 literal, so there is no separate IPv6 branch below.
    if "://" in candidate or "/" in candidate or ":" in candidate or "*" in candidate:
        return None
    if candidate.endswith("."):
        return None
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", candidate):  # IPv4 literal
        return None  # a literal IP has no hostname to allowlist against — refuse, don't guess
    if not _HOST_RE.match(candidate):
        return None
    return candidate
```

At construction, every entry in `config.security.egress_allowlist` is passed
through `normalize_host`; an entry that returns `None` is a **hard
`ConfigError` at boot**, not a silently-dropped entry — a malformed allowlist
entry is exactly the kind of thing that should stop NOVA from starting rather
than quietly narrowing (or, worse, widening — see below) what it will talk to.

`fetch(url)` resolves `url`'s host, runs it through the same
`normalize_host`, and checks membership against the normalized allowlist set
— never a substring/`endswith` check (`"evil-notion.so".endswith("notion.so")`
is exactly how a suffix check gets bypassed; use an exact set membership test
against the fully-normalized host, or explicit subdomain entries in config if
subdomains are needed, never wildcard matching).

No runtime mutator exists on the class — no `add_allowed_host`, `allow`,
`permit`, or similarly-shaped method, at all, checked by
`hasattr`-enumeration in a test (ported directly from the old build's
`test_the_egress_guard_has_no_runtime_allowlist_mutator`, which is exactly
the right shape of test). The allowlist is fixed at process start; changing
it means editing `nova.yaml` and restarting.

### 2.2 `release_for_process(purpose, payload) -> str`

```python
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,63}$")

def release_for_process(self, purpose: str, payload: str) -> str:
    if not _PURPOSE_RE.match(purpose):
        raise ValueError(f"release_for_process(purpose=...) must be a short label, got {purpose!r}")
    if not isinstance(payload, str):
        raise TypeError(f"release_for_process(payload=...) must be str, got {type(payload).__name__}")

    redacted = redact(payload)  # nova.security.redact — the ONE shared implementation, see §2.3

    self._audit.record({
        "kind": "egress.release_for_process",
        "purpose": purpose,
        "chars_in": len(payload),
        "chars_out": len(redacted),
        "redacted": redacted != payload,
    })  # raises on write failure — propagates uncaught, same discipline as Ladder/reveal()

    return redacted
```

Notes for the implementer:

- The audit record carries **lengths and a boolean, never the text** — same
  discipline `AuditLog` payload-sanitization already needs generally (see
  `docs/THREAT_MODEL.md` §4's `AuditLog` requirements). The redacted string
  itself is what the caller pipes to the child's stdin; the audit log is not
  a second copy of it.
- `release_for_process` does not know or care which file calls it — there is
  no caller-identity check here. The guarantee that *only* the sanctioned
  process-spawn allowlist (§3) can reach the point of piping anything to a
  child at all comes from the AST test, not from this function trying to
  authenticate its caller (an `inspect.stack()`-based check would be both
  fragile and exactly the kind of thing a determined bypass finds a way
  around). This function's job is narrower and it should stay narrow:
  redact, audit, return.
- An audit-write failure here must propagate uncaught, exactly as specified
  for `Ladder.authorize()` and `reveal()`. The allowlisted caller (whichever
  file that ends up being) must treat that exception as "do not spawn the
  child" — this is a requirement on that caller, written here so nova-tester
  has something concrete to assert against once that caller exists.

### 2.3 Redaction rules — the one shared implementation

**File:** `nova/security/redact.py`. Both `Egress.fetch`'s future outbound-body
parameter (none exists yet — `fetch(url) -> bytes` is inbound-only per the
current contract) and `release_for_process` call `redact()` from here. Nothing
else implements its own copy; grep for `def redact` finding more than one hit
anywhere under `nova/` is a bug.

```python
def redact(text: str) -> str:
    ...
```

Passes, applied in order (all case-insensitive; every path pattern matches
both `\` and `/` in the same expression, per the constitution's explicit
"both slash styles" requirement):

1. **User-home path shape**, not a hardcoded username. Rather than needing to
   already know Manav's Windows username (which would mean reading it from
   somewhere — `os.environ` is off-limits per the core rule that only
   `Config` may be a path/environment source, and hardcoding it in source
   defeats the point on any other machine), match the *shape* of a home
   directory and redact the captured segment, keeping the rest of the path:
   `r"(?i)([A-Za-z]:[\\/]Users[\\/])([^\\/]+)"` → `\1[USER]`, and the POSIX
   equivalent `r"(?i)(/(?:home|Users)/)([^/]+)"` → `\1[USER]`. This also
   catches `D:\NOVA` if a future entry adds it explicitly (see next point) —
   the home-path pattern alone doesn't need to know NOVA's own root.
2. **User-supplied private terms.** `SecurityConfig` needs a new field —
   `redact_terms: list[str]` (a flagged, small contract addition; the
   equivalent of the old build's private-terms file) — literal strings
   (Manav's name, employer, any term he wants scrubbed) matched
   case-insensitively and replaced with `[REDACTED]`. Empty by default; this
   is opt-in content Manav provides, not inferred.
3. **Secrets/tokens**, pattern-matched: `sk-ant-[A-Za-z0-9-]{20,}`,
   `sk-[A-Za-z0-9]{20,}`, `ghp_[A-Za-z0-9]{20,}`, `AKIA[0-9A-Z]{16}`,
   `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+` (JWT-shaped),
   `(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+` → replace the
   captured value with `[SECRET]`. Each pattern gets its own test fixture
   with a real-shaped fake credential (not a redacted-looking placeholder —
   the old build's own note about the base64 threshold is worth repeating:
   test with something that actually looks like the thing, not something
   that's obviously fake).
4. **Email addresses**: `[\w.+-]+@[\w-]+\.[\w.-]+` → `[EMAIL]`.
5. Every rule is bounded (`{0,N}`, never bare `*`/`+` next to an alternation)
   — these run over text that may originate outside the machine (once M5
   lands), and an unbounded quantifier next to an alternation is a
   ReDoS surface, not just a style preference.

`redact()` is pure: no I/O, no network, no clock, deterministic given
`redact_terms`. Safe to unit test exhaustively without any of the security
kernel's other pieces constructed.

---

## 3. Named allowlist for the rescoped AST test

Two separate exemption lists, for two different reasons — a file needing to
make an HTTP-shaped call is a different exception from a file needing to
spawn a process, and conflating them would let a transport-only file spawn
processes for free (and vice versa). Keep them distinct sets in the test.

### 3.1 Transport imports (`socket`, `ssl`, `urllib`, `http`, `requests`, `httpx`, …)

| File | Why it earns the exemption |
|---|---|
| `nova/security/egress.py` | The one module `fetch()` lives in. This is the entire point of T0.5's `Egress` — the AST test's job is to prove this is the *only* entry. |

Exactly one entry. If a second file ever needs one, that's a design decision
for the lead, not something that should silently pass because a new file
happened to get added near an existing exemption.

### 3.2 Process spawn (`subprocess`, `os.system`, `os.popen`, `os.exec*`,
`os.spawn*`, `os.startfile`, `asyncio.create_subprocess_*`)

| File | Why it earns the exemption | Constraint the test also asserts |
|---|---|---|
| `nova/stt/gpu.py` | **Already landed** (`nova/stt/gpu.py:61`, `_run_nvidia_smi`) — queries `nvidia-smi` for GPU identity during the compute-type probe. Read-only, no user/network input reaches argv. | `argv[0]` must be the literal string `"nvidia-smi"` (not a variable), the call must pass `shell=False` (implicit — never pass a single string), and every call site's `query` argument must be a literal string, not an f-string built from anything outside the module — mirrors the old build's literal-address check on `LOOPBACK_CALLS` (`_OLD_ARCHIVE/tests/test_no_direct_network.py:79-119`), same technique applied to argv instead of an address tuple. |
| `nova/skills/open_app.py` (or wherever T0.5 places the `open_app` handler — confirm exact path with nova-dev-core) | The one legitimate "launch a Windows application" surface, per `docs/CONTRACTS.md`'s amended T0.5 section and `docs/THREAT_MODEL.md` §1.1. | `Popen` (or equivalent) is called with a list, never a string; `shell` is never `True`; the resolved path passed as `argv[0]` must be traceable, in the same function, back through a `realpath()` call and a membership check against the allowed-directory set described in `docs/THREAT_MODEL.md` §1.1 — the AST test can at minimum assert the spawn call is not the first statement in the function (i.e. some validation precedes it lexically), which won't catch a broken validator but will catch a validator that got deleted entirely. |
| *(reserved)* the Claude CLI adapter M4 introduces | Not added until M4 actually lands the file — do not pre-approve a path that doesn't exist. When it does: `argv[0]` must resolve through a `find_cli()`-shaped function, never a raw config/parameter string, and the module must call `Egress.release_for_process()` on any text reaching the child's stdin before the spawn — enforced by an integration test in the style of the old build's `test_dispatch_puts_the_released_payload_on_stdin`, not by the AST test itself (AST can't easily prove *ordering* of "redact, then spawn" — that needs a real subprocess test, same as before). | — |

Three entries today (one reserved), not two — `nova/stt/gpu.py` was missed in
the original framing of this problem because it predates the threat model and
doesn't touch anything sensitive, but the AST test must know about it or T0.3
breaks the moment the rescoped test lands. Every entry here should stay
justified individually; an exemption nobody re-derives the need for at
review time is exactly the "exemption nobody notices going stale" pattern the
old build's own test comments warned about.

---

## 4. T0.1 review — config validation

Looked at `nova/config.py` (`f45093d`) with the lead's specific question in
mind: can config validation be fed something that makes it behave badly?

**No RCE/injection surface** — `yaml.safe_load` is used correctly (not
`yaml.load`/`unsafe_load`), so arbitrary Python object construction via YAML
tags is not reachable. Unknown keys at every level are hard errors naming the
offending key, matching the constitution's "unknown keys are an error, not a
shrug." Bool-as-int contamination is explicitly guarded
(`isinstance(item, elem_type) and not isinstance(item, bool)` — correct,
since `bool` is an `int` subclass in Python and a naive `isinstance(x, int)`
check would silently accept `true`/`false` as `0`/`1`). This module is not
attacker-reachable in M0 (it parses a local file Manav edits, not anything
from mail/web/calendar), so I'm not treating anything below as a blocker —
these are robustness notes for whoever consumes each field, not findings
against `config.py` itself.

**One real gap, relevant to Egress specifically:** `SecurityConfig.
egress_allowlist: list[str]` is type-checked (a list of strings) but not
semantically validated — `config.py`'s own docstring scopes its job to type
safety, correctly. That means `Egress.__init__` must not assume a
syntactically valid list is a safe one: an empty string, `"*"`, a URL with a
scheme, an IP literal, or trailing whitespace would all pass `config.py`'s
check today and need to be caught by `normalize_host` at `Egress`
construction (§2.1). This is the seam described above, not a bug — flagging
so nova-dev-core doesn't assume `config.py` already did this work.

**Two low-severity robustness notes, not security findings, worth one line
each rather than a blocking issue:** `tts.speed: float` accepts YAML's
`.inf`/`.nan` literals (`isinstance(value, (int, float))` is `True` for
both) and would hand `Synthesizer` a value it likely can't use sensibly;
`stt.model: str` accepts any string with no membership check against models
actually present in `cache/hf/hub`, so a typo fails at model-load time
instead of at config-load time. Neither is exploitable — `nova.yaml` isn't
attacker-reachable — but both would produce a confusing failure far from the
config file that caused it, which cuts against the "every module degrades
loudly, naming what's missing" rule. Worth a bounds/membership check
whenever someone's already in this file, not urgent enough to interrupt T0.2
or T0.3.
