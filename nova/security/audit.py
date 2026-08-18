"""Append-only, hash-chained audit trail (docs/CONTRACTS.md T0.5, amended
after docs/THREAT_MODEL.md).

Two properties the threat model made non-negotiable:

1. **Fail closed.** `record()` raises on any failure to durably append an
   entry (write error, or a chain state it cannot safely extend) rather than
   swallowing it and returning normally. The rule this exists to enforce
   lives one layer up, in the pending `Ladder`: "we could not log that this
   left" must mean "it did not leave" — an ACT/CONFIRM action must not
   proceed on an audit write it cannot prove happened. This module's job is
   only to make that failure loud; `Ladder.authorize()` is what turns it into
   a deny.
2. **Tamper-evident.** Each entry carries the hash of the previous entry, so
   editing or deleting a past entry breaks the chain from that point forward.
   `verify()` walks the whole chain and reports the first break — an audit
   log nobody can check is decoration.

Payload sanitization caps string length and drops keys that look like
secrets/tokens/raw audio so the audit file itself cannot become a second,
unredacted transcript store.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nova import AUDIT_LOG_PATH
from nova.log import get_logger
from nova.security.redact import redact

logger = get_logger("security.audit")

GENESIS_HASH = "0" * 64

# --- payload sanitization ---------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(password|secret|token|api[_-]?key|credential|authoriz|cookie|"
    r"pcm|audio_path|wav_path|audio_data|raw_audio)",
    re.IGNORECASE,
)
_MAX_STRING_LEN = 500
_MAX_LIST_ITEMS = 50
_MAX_DEPTH = 6
_REDACTED = "<redacted>"


def _sanitize(value: Any, depth: int = 0, *, redact_terms: Sequence[str] = ()) -> Any:
    """Recursively cap string length, cap list length, redact secret-shaped
    dict keys, and run every string value through the one shared content
    redactor (F14: composing nova.security.redact.redact() here, not just
    matching on key names, closes the gap the moment an ACT/CONFIRM skill
    takes free text — a raw path or email address in a `params` value used
    to sail through the key-based check untouched). Applied to every event
    before it is hashed or written — the audit log is a permanent record,
    not a scratch log."""
    if depth > _MAX_DEPTH:
        return "<max-depth-exceeded>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = _REDACTED
                continue
            out[k] = _sanitize(v, depth + 1, redact_terms=redact_terms)
        return out
    if isinstance(value, list):
        return [_sanitize(v, depth + 1, redact_terms=redact_terms) for v in value[:_MAX_LIST_ITEMS]]
    if isinstance(value, str):
        value = redact(value, redact_terms=redact_terms)
        if len(value) > _MAX_STRING_LEN:
            omitted = len(value) - _MAX_STRING_LEN
            return f"{value[:_MAX_STRING_LEN]}...<truncated {omitted} chars>"
        return value
    return value


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _entry_hash(seq: int, ts: str, event: dict[str, Any], prev_hash: str) -> str:
    payload = _canonical({"seq": seq, "ts": ts, "event": event, "prev_hash": prev_hash})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditWriteError(RuntimeError):
    """Raised when AuditLog.record() cannot durably append an entry — a
    write error, or a chain state it cannot safely extend. Callers (the
    pending Ladder) must treat this as a deny for ACT/CONFIRM tiers, per
    docs/THREAT_MODEL.md §1.3/§4."""


@dataclass(frozen=True)
class ChainStatus:
    """Result of walking the chain. `ok=False` means tampering or deletion
    was detected; `broken_at` is the seq number of the first bad entry."""

    ok: bool
    entries: int
    broken_at: int | None = None
    reason: str | None = None


class AuditLog:
    """The durable record of every ACT/CONFIRM decision NOVA makes. Not a
    general-purpose logger — see nova.log for that."""

    def __init__(self, path: Path | None = None, *, redact_terms: Sequence[str] = ()) -> None:
        self._path = Path(path) if path is not None else AUDIT_LOG_PATH
        self._lock = threading.Lock()
        self._redact_terms = tuple(redact_terms)

    def record(self, event: dict[str, Any]) -> None:
        """Append `event` to the chain. Raises AuditWriteError if the entry
        cannot be durably appended — never fails silently."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                last = self._last_entry()
                seq = 0 if last is None else last["seq"] + 1
                prev_hash = GENESIS_HASH if last is None else last["hash"]
                ts = datetime.now(timezone.utc).isoformat()
                sanitized_event = _sanitize(event, redact_terms=self._redact_terms)
                entry_hash = _entry_hash(seq, ts, sanitized_event, prev_hash)
                entry = {
                    "seq": seq,
                    "ts": ts,
                    "event": sanitized_event,
                    "prev_hash": prev_hash,
                    "hash": entry_hash,
                }
                line = _canonical(entry) + "\n"
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
            except AuditWriteError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.error("audit write failed: %s", exc)
                raise AuditWriteError(f"could not append audit entry: {exc}") from exc

    def _last_entry(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        last: dict[str, Any] | None = None
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line)
        return last

    def verify(self) -> ChainStatus:
        """Walk the chain from the genesis entry and confirm every entry's
        hash matches its content and every prev_hash matches the previous
        entry's hash. Returns the seq of the first broken entry, if any."""
        return verify_chain(self._path)


def verify_chain(path: Path) -> ChainStatus:
    """Free-function form of AuditLog.verify(), usable on any audit.log path
    (e.g. by an offline integrity checker)."""
    path = Path(path)
    if not path.exists():
        return ChainStatus(ok=True, entries=0)

    expected_prev = GENESIS_HASH
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                return ChainStatus(
                    ok=False, entries=count, broken_at=count,
                    reason=f"line {lineno} is not valid JSON: {exc}",
                )

            seq = entry.get("seq")
            ts = entry.get("ts")
            event = entry.get("event")
            prev_hash = entry.get("prev_hash")
            claimed_hash = entry.get("hash")

            if seq != count:
                return ChainStatus(
                    ok=False, entries=count, broken_at=count,
                    reason=f"expected seq {count}, found {seq!r}",
                )
            if prev_hash != expected_prev:
                return ChainStatus(
                    ok=False, entries=count, broken_at=count,
                    reason=(
                        f"entry {count}: prev_hash does not match the previous "
                        f"entry's hash (chain broken - tampering or deletion)"
                    ),
                )
            recomputed = _entry_hash(seq, ts, event, prev_hash)
            if recomputed != claimed_hash:
                return ChainStatus(
                    ok=False, entries=count, broken_at=count,
                    reason=f"entry {count}: stored hash does not match recomputed "
                           f"hash (content was modified after being written)",
                )
            expected_prev = claimed_hash
            count += 1

    return ChainStatus(ok=True, entries=count)
