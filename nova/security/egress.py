"""THE ONLY MODULE THAT MAY REACH THE NETWORK (docs/CONTRACTS.md T0.5,
docs/SECURITY_SPEC.md §2). Everything else under `nova/` is banned from
importing a transport (`socket`, `ssl`, `urllib`, `http`, `requests`,
`httpx`, ...) by the AST test named in docs/SECURITY_SPEC.md §3.1 — this file
is the single, named exemption.

Two boundaries, not one:
  - `fetch(url)` — inbound. Allowlisted hosts only, deny-by-default.
  - `release_for_process(purpose, payload)` — the text-crossing-to-a-spawned
    -process boundary (docs/THREAT_MODEL.md §2.2.3). Redacts, audits length
    and boolean facts only (never the text), and returns what may be piped
    to a child's stdin. It does not know or care which file calls it — the
    guarantee that only a sanctioned process-spawn call site can reach the
    point of piping anything to a child comes from the AST test in
    docs/SECURITY_SPEC.md §3.2, not from an identity check here.

Redaction (nova.security.redact) is the one shared implementation — this
module does not reimplement any of it.
"""

from __future__ import annotations

import re
import urllib.request
from collections.abc import Sequence
from urllib.parse import urlsplit

from nova.config import ConfigError
from nova.security.audit import AuditLog
from nova.security.redact import redact

_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,63}$")

#: Bare hostname only: no scheme, port, path, wildcard, IP literal, or
#: trailing dot. Matches docs/SECURITY_SPEC.md §2.1 exactly.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


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
    if _IPV4_RE.match(candidate):  # a literal IP has no hostname to allowlist against — refuse, don't guess
        return None
    if not _HOST_RE.match(candidate):
        return None
    return candidate


class EgressError(RuntimeError):
    """Raised when `fetch()` is asked for a URL whose host is not on the
    allowlist, or the URL itself doesn't parse into a usable host."""


class EgressRedirectBlocked(EgressError):
    """Raised when a `fetch()` response tries to redirect anywhere.
    Redirects are never followed, for M0: `fetch()` validates only the URL
    it was given, and `urllib.request.urlopen()` follows HTTP redirects by
    default with nothing re-validating where the redirect actually lands —
    an allowlisted host that has (or is tricked into serving) an open
    redirect defeats a host-based allowlist otherwise, and this needs no bug
    in `Egress`'s own logic, only an OAuth callback, a URL shortener, or a
    misconfigured server on an otherwise-legitimate allowlisted host.
    nova-security demonstrated this with two local servers (a 302 from an
    allowlisted-standin to a non-allowlisted one, followed transparently),
    not just asserted it. A future caller may opt into a specific,
    re-validated hop if one is ever genuinely needed; nothing does yet.
    """

    def __init__(self, *, original_url: str, redirect_url: str, status: int) -> None:
        self.original_url = original_url
        self.redirect_url = redirect_url
        self.status = status
        super().__init__(
            f"fetch({original_url!r}) received a {status} redirect to {redirect_url!r} — "
            f"redirects are not followed; the target host is never re-validated against the allowlist"
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Installed process-wide (via `install_opener`) the moment an `Egress`
    is constructed — `Egress` is THE ONLY MODULE THAT MAY REACH THE
    NETWORK, so there is exactly one legitimate installer of this policy.
    `redirect_request` is the single choke point every 301/302/303/307/308
    handler in the base `HTTPRedirectHandler` funnels through, so overriding
    it once blocks every redirect status code, not just 302.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 — urllib's method name
        raise EgressRedirectBlocked(original_url=req.full_url, redirect_url=newurl, status=code)


class Egress:
    """The one sanctioned network/process-egress boundary. No runtime
    allowlist mutator exists on this class, anywhere — no `add_allowed_host`,
    `allow`, `permit`, or similarly-shaped method. The allowlist is fixed at
    process start; changing it means editing nova.yaml and restarting.
    """

    def __init__(
        self,
        allowlist: Sequence[str],
        *,
        audit: AuditLog,
        redact_terms: Sequence[str] = (),
    ) -> None:
        normalized: set[str] = set()
        for raw in allowlist:
            host = normalize_host(raw)
            if host is None:
                # A malformed entry is a hard failure at boot, never a
                # silently-dropped entry — see docs/SECURITY_SPEC.md §2.1.
                raise ConfigError(
                    f"security.egress_allowlist: entry {raw!r} is not a bare "
                    f"hostname (no scheme/port/path/wildcard/IP literal/trailing dot allowed)"
                )
            normalized.add(host)
        self._allowed_hosts: frozenset[str] = frozenset(normalized)
        self._audit = audit
        self._redact_terms = tuple(redact_terms)

        # Refuse every redirect at the urllib layer, process-wide — see
        # _NoRedirectHandler and EgressRedirectBlocked above. install_opener
        # replaces urllib.request's default HTTPRedirectHandler (our class
        # is a subclass of it) for every urlopen() call in the process;
        # since this module is the only sanctioned urlopen() call site
        # (enforced by the AST test), that scope is exactly right.
        urllib.request.install_opener(urllib.request.build_opener(_NoRedirectHandler()))

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return self._allowed_hosts

    def fetch(self, url: str) -> bytes:
        """Fetch `url` if, and only if, its host is on the allowlist.

        Exact set membership against the fully-normalized host — never a
        substring/endswith check (`"evil-notion.so".endswith("notion.so")`
        is exactly how a suffix check gets bypassed).
        """
        parts = urlsplit(url)
        host = normalize_host(parts.hostname or "")
        allowed = host is not None and host in self._allowed_hosts

        self._audit.record({
            "kind": "egress.fetch",
            "host": host,
            "allowed": allowed,
        })  # raises on write failure — propagates, same discipline as release_for_process

        if not allowed:
            raise EgressError(f"host {host!r} (from {url!r}) is not on the egress allowlist")

        try:
            with urllib.request.urlopen(url) as response:  # noqa: S310 — the one sanctioned transport call
                return response.read()
        except EgressRedirectBlocked as exc:
            # A blocked redirect is exactly the event someone will want in
            # the log later — audited distinctly from an ordinary denial,
            # never silently swallowed.
            self._audit.record({
                "kind": "egress.fetch_redirect_blocked",
                "host": host,
                "redirect_target_host": normalize_host(urlsplit(exc.redirect_url).hostname or ""),
                "status": exc.status,
            })  # raises on write failure — propagates, same discipline as everywhere else in this module
            raise

    def release_for_process(self, purpose: str, payload: str) -> str:
        """Redact `payload`, audit lengths and a boolean (never the text),
        and return the text that may be piped to a spawned child's stdin.
        """
        if not _PURPOSE_RE.match(purpose):
            raise ValueError(f"release_for_process(purpose=...) must be a short label, got {purpose!r}")
        if not isinstance(payload, str):
            raise TypeError(f"release_for_process(payload=...) must be str, got {type(payload).__name__}")

        redacted = redact(payload, redact_terms=self._redact_terms)

        self._audit.record({
            "kind": "egress.release_for_process",
            "purpose": purpose,
            "chars_in": len(payload),
            "chars_out": len(redacted),
            "redacted": redacted != payload,
        })  # raises on write failure — propagates uncaught; the caller must not spawn the child on this exception

        return redacted
