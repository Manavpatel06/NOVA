"""NOVA warns first (2026-11-25): the hub tells Manav's iPhone (Bark) before
something becomes a problem, instead of waiting to be asked.

  * The hub lost power: on battery for 2 minutes -> "is it unplugged?", and
    again at 20 % and 10 %; "plugged in again" when it's back.
  * Running hot (90 C or more, twice in a row) -> once an hour at most.
  * Disk nearly full (under 5 GB free) -> once a day at most.
  * Each morning (hub.heads_up_hour, local time; -1 = off), if Canvas is
    connected: how many assignments are due in the next two days. Only the
    counts go through Bark (and Apple): titles stay on the hub -- ask NOVA
    "what's due?" for them.

Reads /proc and /sys and asks the Canvas connector; runs nothing else.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("hub.watch")

UNPLUGGED_S = 120.0
HOT_C = 90
HOT_EVERY_S = 3600.0
DISK_GB = 5
DISK_EVERY_S = 86400.0
LOW_LEVELS = (20, 10)
HEADS_UP_DAYS = 2


def count_due(text: str) -> tuple[int, int]:
    """(due, already submitted) from canvas-mcp's upcoming-assignments text."""
    due = submitted = 0
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("•"):
            due += 1
        elif s.lower().startswith("status:"):
            v = s.lower().split(":", 1)[1].strip()
            if any(w in v for w in ("submitted", "graded")) and not v.startswith(("not", "un")) \
                    and "missing" not in v:
                submitted += 1
    return due, submitted


class Watch:
    def __init__(self, say: Callable[[str], bool], *, facts: Callable[[], dict[str, Any]] | None = None,
                 connectors: Any = None, state_path: Path | None = None, heads_up_hour: int = 8,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = datetime.now) -> None:
        from nova.hub.hub_skills import facts as hub_facts

        self._say = say
        self._facts = facts or hub_facts
        self.connectors = connectors
        self._state_path = state_path
        self._hour = heads_up_hour
        self._clock = clock
        self._now = now
        self._on_battery_since: float | None = None
        self._warned_unplugged = False
        self._warned_levels: set[int] = set()
        self._hot_count = 0
        self._last_hot = -HOT_EVERY_S
        self._last_disk = -DISK_EVERY_S

    # --- one check (every minute) ---------------------------------------------------

    def tick(self) -> list[str]:
        sent: list[str] = []
        try:
            f = self._facts()
        except Exception as exc:  # noqa: BLE001
            _log.warning("couldn't read the hub's state (%s)", type(exc).__name__)
            f = {}
        sent += self._power(f)
        sent += self._heat(f)
        sent += self._disk(f)
        sent += self._heads_up()
        for text in sent:
            self._say(text)
        return sent

    def _power(self, f: dict[str, Any]) -> list[str]:
        now = self._clock()
        status, pct = f.get("battery_status"), f.get("battery_pct")
        if status != "discharging":
            back = self._warned_unplugged and status in ("charging", "full", "not charging")
            self._on_battery_since, self._warned_unplugged = None, False
            self._warned_levels.clear()
            return ["The hub is plugged in again."] if back else []
        if self._on_battery_since is None:
            self._on_battery_since = now
        out = []
        if not self._warned_unplugged and now - self._on_battery_since >= UNPLUGGED_S:
            self._warned_unplugged = True
            out.append(f"The hub is running on its battery ({pct} percent). Is it unplugged?")
        for level in LOW_LEVELS:
            if isinstance(pct, int) and pct <= level and level not in self._warned_levels and self._warned_unplugged:
                self._warned_levels.update(lv for lv in LOW_LEVELS if lv >= level)
                out.append(f"The hub's battery is down to {pct} percent. Plug it in, or NOVA will go quiet.")
                break
        return out

    def _heat(self, f: dict[str, Any]) -> list[str]:
        t = f.get("temp_c")
        if not isinstance(t, (int, float)) or t < HOT_C:
            self._hot_count = 0
            return []
        self._hot_count += 1
        now = self._clock()
        if self._hot_count >= 2 and now - self._last_hot >= HOT_EVERY_S:
            self._last_hot = now
            return [f"The hub is running hot ({round(t)} degrees). Is its vent blocked?"]
        return []

    def _disk(self, f: dict[str, Any]) -> list[str]:
        gb = f.get("disk_free_gb")
        now = self._clock()
        if isinstance(gb, (int, float)) and gb < DISK_GB and now - self._last_disk >= DISK_EVERY_S:
            self._last_disk = now
            return [f"The hub has only {gb} gigabytes free."]
        return []

    # --- the morning heads-up -----------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        if self._state_path is None:
            return getattr(self, "_mem", {})
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        if self._state_path is None:
            self._mem = data
            return
        try:
            self._state_path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def _heads_up(self) -> list[str]:
        if self._hour < 0 or self.connectors is None:
            return []
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        state = self._state()
        if now.hour < self._hour or state.get("heads_up") == today:
            return []
        try:
            ready = {s["name"] for s in self.connectors.status() if s.get("state") == "ready"}
        except Exception:  # noqa: BLE001
            ready = set()
        if "canvas" not in ready:
            return []
        try:
            text, is_error = self.connectors.call("canvas", "get_my_upcoming_assignments", {"days": HEADS_UP_DAYS})
        except Exception as exc:  # noqa: BLE001 -- try again next minute
            _log.warning("Canvas heads-up failed (%s)", type(exc).__name__)
            return []
        state["heads_up"] = today
        self._save(state)
        if is_error:
            return []
        due, done = count_due(text)
        left = due - done
        if left <= 0:
            return []
        word = "thing" if left == 1 else "things"
        return [f"Canvas: {left} {word} due in the next {HEADS_UP_DAYS} days"
                + (f" ({done} already submitted)" if done else "") + ". Ask NOVA \"what's due?\" for details."]
