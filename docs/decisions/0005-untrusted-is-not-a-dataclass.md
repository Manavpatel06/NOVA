# ADR 0005: `Untrusted[T]` is a hand-rolled class, not a `@dataclass`

**Status:** decided, implemented in `nova/security/untrusted.py`. Amends `docs/SECURITY_SPEC.md` §1 (2026-08-17). Tracked as finding F12 in `docs/TASKS.md` — **CRITICAL**, fixed at `dbd77d8`, verified by the lead.

## Context

`docs/SECURITY_SPEC.md`'s original design for `Untrusted[T]` (§1.1) specified it as `@dataclass(frozen=True, slots=True, eq=False)`, with the enforcement resting on the class implementing none of the string protocols (`__add__`, `__contains__`, `__iter__`, etc.) and `__str__`/`__format__` returning a label instead of the wrapped content. That design was reviewed and initially landed as a dataclass.

**The gap:** `dataclasses.asdict()` and `dataclasses.astuple()` do not go through `__str__`, `__repr__`, `__format__`, or any of the deliberately-unimplemented string protocols at all. They walk `dataclasses.fields()` and call `getattr()` on the raw attributes directly — a mechanism entirely outside the one this design was built to close. Any code calling `asdict()` on a structure that happened to contain an `Untrusted` value — including a value nested several layers inside another dataclass — got the raw wrapped content back, silently, with no audit record, because `reveal()` was never called. This is exactly the "complete, silent, unaudited unwrap" the whole design exists to prevent, reachable through a single standard-library call nobody had to write maliciously — it's arguably the most likely accidental bypass of any considered, precisely because `asdict()` looks like an innocuous logging/serialization convenience.

## Decision

`Untrusted[T]` is a plain class with `__slots__` — not a `@dataclass`, not any decorator that would make `dataclasses.is_dataclass(Untrusted(...))` true. Immutability is hand-rolled (`__setattr__`/`__delattr__` raise `AttributeError` after `__init__`) rather than obtained from `frozen=True`.

## Why

`dataclasses.asdict()`/`astuple()` gate on `dataclasses.is_dataclass()` — removing `Untrusted` from that category by construction closes the *entire class* of bypass, not just the one call site where it was found. Patching the one discovered call site (or adding a custom `__getstate__`/`asdict`-override trick) would have left the same structural gap open for the next standard-library or third-party function that introspects dataclass fields directly instead of going through the object's own protocol methods — `copy.deepcopy` on a badly-configured class, certain serialization libraries, `pprint` in some configurations, and future stdlib additions are all in the same shape of risk. The fix needed to be "this type is not a dataclass at all," not "this type has been patched against the one tool that currently does this."

## Consequences

- Every place that previously relied on dataclass conveniences (equality, hashing, repr) now has a hand-written implementation in `nova/security/untrusted.py` — more code than the decorator would have produced, and that's the accepted cost.
- Verified by the lead directly (per `docs/TASKS.md`'s F12 entry) rather than by test alone — a `dataclasses.asdict()` call against a structure containing an `Untrusted` value, including nested inside another dataclass, was confirmed to raise/fail to unwrap rather than silently succeed.

## The lesson, corrected after F17 — this section originally undercounted the threat surface

**This ADR originally named two categories to check a security-boundary type against: object protocols (`str()`, f-strings, `+`) and introspection-based serialization (`asdict`, `vars()`, `pickle`, `copy.deepcopy`).** nova-accuracy found a third that neither covers (finding **F17**, `docs/TASKS.md`, HIGH): **plain attribute access.** `u._value` returns the raw wrapped content with no audit trail, and it is neither a protocol method nor a serialization function called on the object — it's just reading a name off it, the same as reading any other attribute on any other object. `Untrusted`'s hand-rolled `__slots__`/no-dataclass design (this ADR's own fix) closes the first two categories completely and says nothing at all about the third, because attribute access was never one of the protocols removed.

The danger this created was specific: **this document read as exhaustive while being incomplete.** Someone following its two-category checklist for a future security-boundary type would believe they'd covered the surface and would still ship the same gap `Untrusted` had — because the checklist itself was short one category, in the one document people trust precisely because it explains *why*, not just *what*.

**The corrected list is three categories, not two:**
1. Object protocols (`str()`, f-strings, `+`, `in`, iteration, truthiness).
2. Introspection-based serialization (`asdict`, `vars()`, `__dict__` access, `pickle`, `copy.deepcopy`).
3. **Plain attribute access on the wrapper itself** — the field holding the sensitive value has no privacy Python enforces; if its name is guessable or documented (`_value` is both), nothing stops `u._value` short of nobody trying it.

**What's actually fixed today, and what isn't:** the AST test (`tests/test_security_untrusted.py::_finds_untrusted_bypass`) now also flags a literal `.attr == "_value"` access anywhere under `nova/`, the same allowlist-of-one technique already used for `reveal`. This closes the *lazy* version of category 3 — someone reaching for the obvious attribute without particular effort — the same way this ADR's original fix closed the lazy version of category 2. It does **not** close the *deliberate* version: `getattr(u, "_value")`, or `gc.get_referents(u)` walking the object graph, both defeat the AST scan and are explicitly, deliberately not defended against (the lead's scoping call, recorded in the test file's own comment) — Python has no real private attributes, and pretending a reference-graph-walking check could enforce one would be theatre, not a control. That gap is closed the same way every deliberate bypass in this codebase is closed: by the injection test suite proving hostile intent doesn't reach a tool call, not by a type system that cannot stop a determined reader from all directions at once.
