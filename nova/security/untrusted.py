"""The `Untrusted[T]` wrapper (docs/SECURITY_SPEC.md §1, docs/THREAT_MODEL.md
§3, docs/CONTRACTS.md T0.5).

No M0 skill uses this yet — `time` and `open_app` both declare
`trusts_input=False`. It exists now because it needs to be structurally
correct before anything (a future mail/calendar skill) is built on top of it.

Why this is not just a `NewType` or a `str` subclass: a naming-convention
wrapper is defeated by the first careless `str(x)`, f-string, or `.format()`
call — all of which succeed silently on a `str` subclass, so the router or a
skill can work correctly on hostile input and nobody notices the type check
meant nothing. `Untrusted` is deliberately NOT a `str` subclass and
implements none of the protocols (`__add__`, `__radd__`, `__contains__`,
`__iter__`, `__getitem__`, `__bool__`) that would let it stand in for its
wrapped value by accident — their absence is the enforcement. Do not add any
of them later without re-reading docs/THREAT_MODEL.md §3.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from nova.security.audit import AuditLog

T = TypeVar("T")

#: e.g. "mail.body", "web.fetch", "calendar.invite", "document.text"
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?$")

#: "<module>:<function>" of the reveal() call site, e.g.
#: "nova.skills.mail_summary:handler". Not free text, so a caller can't
#: smuggle attacker content into the audit log through this argument.
_CALLER_RE = re.compile(r"^[a-z_][a-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class Untrusted(Generic[T]):
    """A value that came from outside NOVA's control (email bodies, calendar
    invites, web content, document text — CLAUDE.md rule 2). Not a str
    subclass — that is the whole point. `source` identifies where the
    content came from, for logs — assigned once, at construction, by
    whatever module first receives the outside content. There is no
    "upgrade" path from `str` to `Untrusted` anywhere else: a module that
    needs to treat something as untrusted wraps it itself at the boundary;
    it never receives a pre-wrapped value from a function whose signature
    promised `str`.

    Deliberately NOT a `@dataclass` (docs/SECURITY_SPEC.md §1 amendment,
    2026-08-17): `dataclasses.asdict()`/`astuple()` walk `dataclasses.
    fields()` and `getattr()` the raw attributes directly, entirely
    bypassing `__str__`/`__repr__`/`__format__` — a dataclass-based
    Untrusted was a *complete, silent, unaudited* unwrap via a standard-
    library call nobody had to write, and it is recursive: an Untrusted
    nested inside another dataclass unwraps the instant anyone calls
    asdict() on the *outer* object. Hand-rolling as a plain class with
    `__slots__` removes it from `dataclasses.is_dataclass()` entirely, which
    is what asdict/astuple gate on — this closes the whole class of bypass,
    not just the one call site that was found.
    """

    __slots__ = ("_value", "source")

    def __init__(self, value: T, source: str) -> None:
        if isinstance(value, Untrusted):
            raise TypeError("Untrusted may not wrap an Untrusted (no re-labelling to hide provenance)")
        if not _SOURCE_RE.match(source):
            raise ValueError(f"Untrusted.source must be a dotted lowercase tag, got {source!r}")
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "source", source)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"Untrusted is frozen — cannot set {name!r} after construction")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Untrusted is frozen — cannot delete {name!r}")

    # -- deliberately NOT implemented: __add__, __radd__, __contains__,
    # -- __iter__, __getitem__, __bool__. Each is a way "just use it a
    # -- little" would work by accident. Absence is the enforcement.

    def __len__(self) -> int:
        raise TypeError("len(Untrusted) is not defined — call reveal() explicitly if you need the content")

    def __repr__(self) -> str:
        return f"Untrusted(source={self.source!r}, digest={self._digest()})"

    __str__ = __repr__  # str(x) / f"{x}" / "..".format(x) return the LABEL, never the content

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


_AUDIT: "AuditLog | None" = None


def bind_audit(audit: "AuditLog") -> None:
    """Called once by the security kernel's boot sequence. Before this is
    called, reveal() still works (fails open on missing audit, not on the
    unwrap itself) but logs a WARNING naming the gap every time — matching
    the "every module degrades loudly" cross-cutting rule. This is a
    narrower exception than the Ladder's audit-failure-means-deny rule:
    reveal() has no side effect on the world by itself, only what the caller
    does with the text does, so it fails open here and the caller's own
    tool-call path is what must fail closed.
    """
    global _AUDIT
    _AUDIT = audit


def reveal(u: Untrusted[T], *, caller: str, purpose: str) -> T:
    """The one sanctioned way to get the wrapped value back out.

    `caller` is `"<module>:<function>"` of the call site — not free text, so
    it can't itself become a place to smuggle attacker content into the
    audit log. `purpose` is a short human-readable reason ("summarize for
    the user", "check for a meeting time"). Every call is audited with
    source, caller, purpose and a content digest — never the content itself.

    Structurally restricted to two call sites (docs/SECURITY_SPEC.md §1.2,
    enforced by the AST test in docs/SECURITY_SPEC.md §1.3, not by this
    function): a skill handler decorated `trusts_input=True`, to inform the
    `Reply` it returns; and the reply-composition/humanizer layer, to quote
    or paraphrase content already destined for speech output. Never the
    router or the skill-parameter binder — revealed text must never become a
    tool-call parameter.
    """
    if not isinstance(u, Untrusted):
        raise TypeError(f"reveal() requires an Untrusted value, got {type(u).__name__}")
    if not _CALLER_RE.match(caller):
        raise ValueError(f"reveal(caller=...) must be 'module:function', got {caller!r}")
    if not purpose.strip():
        raise ValueError("reveal(purpose=...) must not be empty")

    record = {
        "kind": "untrusted.reveal",
        "source": u.source,
        "caller": caller,
        "purpose": purpose,
        "digest": u._digest(),
    }
    if _AUDIT is not None:
        _AUDIT.record(record)  # raises on write failure — propagates, see docs/SECURITY_SPEC.md §1.4
    else:
        logging.getLogger("nova.security").warning("reveal() called before bind_audit(): %s", record)

    return u._value
