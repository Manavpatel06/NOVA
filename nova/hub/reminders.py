"""Reminders and timers, kept and fired by the hub (2026-11-24).

The hub is always on, so it is the one that remembers "remind me at 5" and
makes Manav's iPhone ring (nova.hub.notify, Bark). One JSON file,
data/reminders.json, written atomically.

Firing rules (HubService.tick, every few seconds):
- normal: one Time Sensitive notification when due
- urgent: a Critical alert that rings through silent mode, repeated every
  RESEND_S until he taps "Got it" (the notification opens the hub's ack
  page) or MAX_SENDS is reached
- if the hub was off and a reminder is more than MISSED_AFTER_S late, it is
  sent once, marked as late, rather than not at all
- delivered ones are kept KEEP_DONE_S so "what did you remind me of" works,
  then dropped
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

MAX_TEXT = 200
MAX_PENDING = 500
MAX_AHEAD_S = 366 * 86400
RESEND_S = 60
MAX_SENDS = 10
MISSED_AFTER_S = 12 * 3600
KEEP_DONE_S = 7 * 86400


class ReminderError(ValueError):
    pass


class ReminderStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    # --- file ---------------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)

    # --- api ----------------------------------------------------------------

    def add(self, text: Any, due: Any, *, urgent: bool = False, call: bool = False) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ReminderError("text is required")
        if isinstance(due, bool) or not isinstance(due, (int, float)):
            raise ReminderError("due must be a unix time")
        now = self._clock()
        if not now - 60 <= due <= now + MAX_AHEAD_S:
            raise ReminderError("due must be in the next year")
        item = {
            "id": secrets.token_hex(4),
            "token": secrets.token_urlsafe(12),
            "text": " ".join(text.split())[:MAX_TEXT],
            "due": float(due),
            "urgent": bool(urgent),
            "call": bool(call),   # also phone Manav (ADR 0025)
            "created": now,
            "sent": 0,
            "last_sent": 0.0,
            "done": False,
            "acked": False,
        }
        with self._lock:
            items = self._load()
            if sum(1 for r in items if not r.get("done")) >= MAX_PENDING:
                raise ReminderError("too many reminders")
            items.append(item)
            self._save(items)
        return item

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((r for r in self._load() if not r.get("done")), key=lambda r: r["due"])

    def get(self, rid: str) -> dict[str, Any] | None:
        with self._lock:
            return next((r for r in self._load() if r.get("id") == rid), None)

    def cancel(self, which: str) -> int:
        """Cancel one reminder by id, or "all" pending ones. Returns how many."""
        with self._lock:
            items = self._load()
            n = 0
            for r in items:
                if not r.get("done") and (which == "all" or r.get("id") == which):
                    r["done"], r["cancelled"] = True, True
                    n += 1
            self._save(items)
            return n

    def ack(self, rid: str, token: str) -> dict[str, Any] | None:
        with self._lock:
            items = self._load()
            for r in items:
                if r.get("id") == rid and secrets.compare_digest(str(r.get("token", "")), str(token or "")):
                    r["acked"], r["done"] = True, True
                    self._save(items)
                    return r
            return None

    def tick(self, send: Callable[[dict[str, Any], bool], bool]) -> int:
        """Send what's due. `send(reminder, late)` -> delivered? Returns how
        many notifications went out."""
        now = self._clock()
        sent = 0
        with self._lock:
            items = self._load()
            changed = False
            for r in items:
                if r.get("done") or r["due"] > now:
                    continue
                first = r["sent"] == 0
                again = r["urgent"] and not first and now - r["last_sent"] >= RESEND_S
                if not (first or again):
                    continue
                if send(r, first and now - r["due"] > MISSED_AFTER_S):
                    r["sent"] += 1
                    r["last_sent"] = now
                    sent += 1
                    if not r["urgent"] or r["sent"] >= MAX_SENDS:
                        r["done"] = True
                    changed = True
            kept = [r for r in items if not (r.get("done") and now - max(r["due"], r["last_sent"]) > KEEP_DONE_S)]
            if changed or len(kept) != len(items):
                self._save(kept)
        return sent
