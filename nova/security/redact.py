"""The ONE shared redaction implementation (docs/SECURITY_SPEC.md §2.3,
CLAUDE.md's egress and output-sanitizing rules).

Nothing else implements its own copy — grep for `def redact` finding more
than one hit anywhere under `nova/` is a bug. Both `Egress.release_for_process`
and (once it grows an outbound-body parameter) `Egress.fetch` call `redact()`
from here, and so does whatever eventually renders replies containing quoted
untrusted content.

`redact()` is pure: no I/O, no network, no clock, deterministic given
`redact_terms`. Safe to unit test exhaustively without any of the security
kernel's other pieces constructed.

Every pattern is bounded (`{0,N}`, never a bare `*`/`+` next to an
alternation) — this runs over text that may originate outside the machine
(once M5 lands), and an unbounded quantifier next to an alternation is a
ReDoS surface, not just a style preference.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# --- 1. user-home path shape, not a hardcoded username ---------------------
#
# Nothing but Config may read the environment, so this can't look up the
# actual username — it matches the *shape* of a home directory instead and
# redacts only the captured segment, keeping the rest of the path. Both
# slash styles, case-insensitive. This also catches `D:\NOVA` if a future
# config entry adds it explicitly (redact_terms) — the home-path pattern
# alone doesn't need to know NOVA's own root.
_WIN_HOME_RE = re.compile(r"(?i)([A-Za-z]:[\\/]Users[\\/])([^\\/]{1,255})")
_POSIX_HOME_RE = re.compile(r"(?i)(/(?:home|Users)/)([^/]{1,255})")

# --- 3. secrets/tokens, pattern-matched -------------------------------------
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9-]{20,120}"),
    re.compile(r"sk-[A-Za-z0-9]{20,120}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,120}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{1,500}\.[A-Za-z0-9_-]{1,500}\.[A-Za-z0-9_-]{1,500}"),  # JWT-shaped
)
_LABELLED_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password)(\s*[:=]\s*)(\S{1,256})"
)

# --- 4. email addresses ------------------------------------------------------
_EMAIL_RE = re.compile(r"[\w.+-]{1,128}@[\w-]{1,128}\.[\w.-]{1,128}")


def redact(text: str, *, redact_terms: Sequence[str] = ()) -> str:
    """Redact `text` in place, applying passes in order:

    1. User-home path shape (both slash styles, case-insensitive) -> [USER]
    2. `redact_terms` — Manav's own private terms (SecurityConfig.redact_terms,
       empty by default), literal, case-insensitive -> [REDACTED]
    3. Secrets/tokens, pattern-matched -> [SECRET]
    4. Email addresses -> [EMAIL]

    Pure function: no I/O, no network, no clock. Deterministic given
    `redact_terms`.
    """
    result = _WIN_HOME_RE.sub(r"\1[USER]", text)
    result = _POSIX_HOME_RE.sub(r"\1[USER]", result)

    for term in redact_terms:
        if not term:
            continue
        result = re.sub(re.escape(term), "[REDACTED]", result, flags=re.IGNORECASE)

    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[SECRET]", result)
    result = _LABELLED_SECRET_RE.sub(r"\1\2[SECRET]", result)

    result = _EMAIL_RE.sub("[EMAIL]", result)

    return result
