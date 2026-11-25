"""NOVA's live face on the hub's screen (ADR 0028): what the page at
http://127.0.0.1:8765/face draws, kept here as a small state machine.

Talk tells it when someone is talking to NOVA (from the phone, the laptop's
mic, the hub's own mic), when NOVA is thinking, and what it says back; the
page polls /v1/face/state a few times a second and animates the star.

Only sentences addressed to NOVA ever reach it: speech that wasn't for NOVA
is dropped before this (talk.hear_text), so the screen never shows the room.
The state is served to the hub itself (loopback) and to Manav's own devices.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

MAX_TEXT = 600
SAY_RATE = 14.0      # characters a second, when the reply wasn't spoken here


class Face:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._mode = "idle"
        self._until = 0.0
        self._heard = ""
        self._said = ""
        self._source = ""
        self._seq = 0
        self.devices: Any = None

    def _set(self, mode: str, **fields: Any) -> None:
        with self._lock:
            self._mode = mode
            self._seq += 1
            for k, v in fields.items():
                setattr(self, f"_{k}", v)

    def listening(self, source: str) -> None:
        self._set("listening", source=source, until=self._clock() + 30)

    def thinking(self, source: str, heard: str) -> None:
        self._set("thinking", source=source, heard=_cap(heard), until=self._clock() + 180)

    def speaking(self, said: str, seconds: float | None = None) -> None:
        said = _cap(said)
        secs = seconds if seconds is not None else max(1.5, len(said) / SAY_RATE)
        self._set("speaking", said=said, until=self._clock() + min(secs, 120))

    def idle(self) -> None:
        self._set("idle", until=0.0)

    def state(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            mode = self._mode if (self._mode == "idle" or now < self._until) else "idle"
            out = {"mode": mode, "heard": self._heard, "said": self._said, "source": self._source,
                   "seq": self._seq}
        devices = self.devices
        out["laptop"] = bool(devices is not None and devices.online("laptop"))
        return out


def _cap(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rsplit(" ", 1)[0] + "..."
