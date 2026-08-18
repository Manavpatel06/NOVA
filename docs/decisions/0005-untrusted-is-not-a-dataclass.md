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
- This is the second-order lesson worth generalizing beyond this one class: **any security-boundary type should be checked against introspection-based serialization (`asdict`, `vars()`, `__dict__` access, `pickle`, `copy.deepcopy`), not just against the object protocols (`str()`, f-strings, `+`) the design started from.** The first review covered the protocols; it took a second pass to find the introspection path. Worth a deliberate check the next time a similar wrapper type is designed, rather than assuming protocol coverage is complete coverage.
- Verified by the lead directly (per `docs/TASKS.md`'s F12 entry) rather than by test alone — a `dataclasses.asdict()` call against a structure containing an `Untrusted` value, including nested inside another dataclass, was confirmed to raise/fail to unwrap rather than silently succeed.
