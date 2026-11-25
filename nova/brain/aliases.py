"""Email addresses Manav says, without Claude seeing them (2026-11-25).

Everything sent to Claude has email addresses redacted, so "draft an email to
john@example.com" used to reach the brain as "[EMAIL]" and nothing could be
addressed. Now an address in Manav's OWN words becomes a numbered stand-in --
[EMAIL-1] -- that Claude can pass back in a tool's parameters; NOVA swaps the
real address back in on this machine, just before the skill runs. Claude
never sees the address itself.

Only Manav's own sentences make stand-ins. An address inside outside content
(an email body, a web page) stays plain [EMAIL] and can't be used as a
parameter: outside content never parameterizes an action (CLAUDE.md rule 2).
The stand-ins are forgotten with the conversation.
"""
from __future__ import annotations

import re
from typing import Any

from nova.security.redact import _EMAIL_RE

TOKEN_RE = re.compile(r"\[EMAIL-(\d{1,3})\]")
MAX = 50


class EmailAliases:
    def __init__(self) -> None:
        self._by_addr: dict[str, str] = {}
        self._by_token: dict[str, str] = {}

    def alias(self, text: str) -> str:
        """Manav's sentence, with each address replaced by its stand-in."""
        def swap(m: re.Match) -> str:
            addr = m.group(0).rstrip(".,;:")
            tail = m.group(0)[len(addr):]
            key = addr.lower()
            token = self._by_addr.get(key)
            if token is None:
                if len(self._by_token) >= MAX:
                    return m.group(0)   # redaction then hides it as usual
                token = f"[EMAIL-{len(self._by_token) + 1}]"
                self._by_addr[key] = token
                self._by_token[token] = addr
            return token + tail

        return _EMAIL_RE.sub(swap, text or "")

    def known(self, text: str) -> str:
        """Outside text (a tool's reply): addresses Manav already said show as
        their stand-ins; any other address is left for redaction to hide."""
        if not self._by_addr:
            return text
        return _EMAIL_RE.sub(lambda m: self._by_addr.get(m.group(0).lower(), m.group(0)), text or "")

    def restore(self, value: Any) -> Any:
        """Tool parameters with stand-ins turned back into the addresses."""
        if isinstance(value, str):
            return TOKEN_RE.sub(lambda m: self._by_token.get(m.group(0), m.group(0)), value)
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        return value

    def forget(self) -> None:
        self._by_addr.clear()
        self._by_token.clear()
