# ADR 0009: CONFIRM denials get three distinct, non-interchangeable audit reasons

**Status:** decided, implemented in `nova/security/ladder.py` (`Ladder.authorize()`'s CONFIRM branch) and `nova/security/confirm.py` (`ConfirmTimeout`, `ScriptedConfirmChannel`, `ConsoleConfirmChannel`). Lead's ruling, 2026-08-18, amending `docs/THREAT_MODEL.md` §1.4.

## Context

`Ladder.authorize()` for `Tier.CONFIRM` can fail to produce an approval for at least three genuinely different reasons: **a human was asked and said no** (`request()` returns `False`); **a human was asked but nothing came back in time** (the channel's own time budget elapsed with no clear answer); and **the channel itself is broken** (an exception during `request()` — a crashed dialog process, a dropped console connection, a programming error in a future channel implementation). Before this decision, all three collapsed into a single generic denial reason, indistinguishable from each other in the audit log.

That collapse is a specific kind of lie, not just an imprecision: an audit entry that reads "declined" is a factual claim that a human was consulted and said no. If the true cause was a channel exception that never reached a human at all, the log now asserts something that didn't happen — and unlike a merely incomplete record, this is a *false* one that will be believed by whoever reads it later trying to reconstruct what actually occurred (an operator investigating an incident, a security review, a curious Manav).

## Decision

Three distinct, audited reasons, one per cause:

- **`confirm_declined`** — `request()` returned `False`. A human (or the scripted test double standing in for one) was actually consulted and said no.
- **`confirm_timed_out`** — the channel raised `ConfirmTimeout`, a dedicated exception type introduced specifically so a timeout can't be confused with a `return False`. `ConfirmChannel.request()`'s own contract (documented on the `Protocol` in `nova/security/ladder.py`) requires implementations to raise this, not silently return `False`, when no clear answer arrived within their own time budget — "never let an unclear outcome read as approval" is extended here to also mean "never let an unclear outcome read as the *wrong kind* of denial."
- **`confirm_channel_error`** — any other exception during `request()`. Caught broadly (`except Exception`), logged with the exception, and denied — a broken channel must never crash the ladder or hang, and it must never be recorded as though a human weighed in.

`no_confirm_channel` (no channel registered at all — `docs/decisions` predates this one, see `docs/THREAT_MODEL.md` §1.4's original fail-closed requirement) remains a fourth, separate reason, for the case where the question was never even posed to anything.

## Why

The principle, stated precisely because it generalizes beyond CONFIRM: **an audit log that records the wrong specific reason is worse than one that records no reason at all, because the wrong reason will be trusted.** A missing or generic record invites a follow-up question ("why did this deny?"); a specific but false one answers that question incorrectly and the inquiry stops there. This is a sharper version of the audit-log discipline the constitution already requires ("we could not log that this left" must mean "it did not leave") — here the risk isn't a missing record, it's a *misleading present* one, which is arguably the more dangerous failure mode because nothing about it looks broken.

## Consequences

- Any future `ConfirmChannel` implementation (M1's tray/dialog channel is the next one in line) inherits this obligation: it must raise `ConfirmTimeout` distinctly from returning `False`, or its timeouts will silently misreport as declines. This is a real constraint on new channel authors, not just a `ladder.py` implementation detail — documented on the `Protocol` itself so it travels with the interface, not just this ADR.
- `ScriptedConfirmChannel` (the deterministic test double in `nova/security/confirm.py`) can simulate all three outcomes explicitly — approve, decline, `TIMEOUT` sentinel, or a live exception instance — so nova-tester can assert the ladder produces the *correct distinct reason* for each, not just that it denies.
