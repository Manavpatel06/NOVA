# ADR 0010: `Egress.fetch()` blocks every HTTP redirect, rather than re-validating each hop

**Status:** decided, implemented in `nova/security/egress.py` (`_NoRedirectHandler`, `EgressRedirectBlocked`). Tracked as finding F13 in `docs/TASKS.md`.

## Context

`Egress.fetch(url)` validates `url`'s host against the allowlist before doing anything else — exact, normalized set membership, the discipline `docs/SECURITY_SPEC.md` §2.1 specifies. But that check happens once, against the URL the caller passed in, at one point in time. `urllib.request.urlopen()` — the actual transport call, immediately afterward — follows HTTP redirects (301/302/303/307/308) by default, and **nothing re-validated where the redirect actually pointed.** The allowlist check and the eventual network connection were, in the redirect case, checking two different hosts.

This is not a hypothetical. nova-security demonstrated it concretely with two local servers: an allowlisted stand-in host serving a 302 to a non-allowlisted one, followed by `urlopen()` transparently, with the fetched bytes coming back from the second, unvalidated host. No bug in `Egress`'s own logic was needed to produce this — an OAuth callback, a URL shortener, or a misconfigured (or compromised) server on an otherwise-legitimate allowlisted host is a sufficient and realistic trigger.

## Decision

Block every redirect, unconditionally, for every `fetch()` call. A custom `urllib.request.HTTPRedirectHandler` subclass (`_NoRedirectHandler`) overrides `redirect_request` — the single choke point every redirect status code in the base handler funnels through, so overriding it once covers all of 301/302/303/307/308, not just 302 — to always raise `EgressRedirectBlocked` instead of following. It's installed process-wide via `urllib.request.install_opener()` the moment an `Egress` is constructed; since `Egress` is the one module permitted to call `urlopen()` at all (enforced by the AST test), a process-wide install is exactly the right scope, not an overreach.

A blocked redirect is itself audited distinctly (`egress.fetch_redirect_blocked`, recording both the original and the attempted redirect-target host) — visible to whoever reviews the log later, not a silently swallowed event.

**Explicitly not chosen:** re-validating each redirect hop against the allowlist and following it only if it also passes. That's available as a future extension — the exception type and the audit record already carry the redirect target host, so a caller that genuinely needs redirect-following has the pieces to build it — but it wasn't built now, because building it before any real caller exists means designing against an imagined requirement instead of a measured one. No skill calls `fetch()` yet in M0. The safer default (block everything) costs nothing today and can be relaxed deliberately, per-caller, later; the reverse — shipping a permissive redirect-revalidation scheme now and discovering it has a gap once a caller actually needs it — is the more expensive mistake to make first.

## Why

The generalizable lesson, worth stating precisely because it's not the first time this shape of gap has shown up in this codebase: **validating an input to a library call says nothing about what that library call does after it runs.** The allowlist check was true about the URL as given; it said nothing about the sequence of connections `urlopen()` might actually make on the caller's behalf, because redirect-following is behavior the library adds *after* the input you validated. This is the same category of blind spot as two other decisions already on record, in different shapes:

- `docs/decisions/0003-ast-test-covers-process-spawn-not-just-sockets.md` — the AST test was true about which files import a transport, and said nothing about what a spawned process does once it's running.
- `docs/decisions/0007-wake-word-vendored-not-openwakeword-dependency.md` — the AST test was true about `nova/`'s own imports, and said nothing about what a third-party dependency does at its own import time.

All three are instances of the same failure shape: a check that is accurate about the surface it inspects, paired with a delegate (a spawned process, an imported package, a followed redirect) that does more than the surface implied. The fix is never "check harder at the same point" — it's either extend the check to cover the delegate's actual behavior, or, as here, remove the behavior the check can't see.

## Consequences

- `fetch()` cannot follow any redirect today, including a fully benign one (e.g. an allowlisted host's own `http://` → `https://` upgrade on the same hostname). No caller has hit this yet; when one does, it's a real, known limitation to design around deliberately, not a bug to patch reactively.
- The pattern here — block first, add narrow re-validated exceptions only against a real, measured need — is the same posture as `docs/decisions/0002-int8-refused-on-blackwell.md`'s refusal despite measured absence of a crash: refuse the wider, harder-to-verify behavior even when nothing currently exploits the gap, and revisit only when a concrete caller creates a concrete need.
