"""Manav's devices, as seen from the hub's NOVA (ADR 0024).

A device (the laptop today) runs the small NOVA Link helper (nova/link.py).
The helper connects OUT to the hub and long-polls for jobs, so the laptop
opens no port. Every request is owner-only, like every hub route.

A job is one approved skill call. The hub's ladder has already authorized it
(tier, spoken approval) by the time it is queued here. Before that, a
`resolve` job can ask the device what a free-text name means (open_app's
"spotify" -> the real path) so the approval question shows the resolved
thing, exactly as on the laptop.

Jobs are queued in memory only: a hub restart drops them. Nothing queued
outlives JOB_TTL_S, so a laptop that wakes up an hour later never runs a
stale "shut down".
"""
from __future__ import annotations

import concurrent.futures as cf
import re
import secrets
import threading
import time
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("hub.devices")

DEVICE_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")
SKILL_RE = re.compile(r"[a-z][a-z0-9_]{1,47}")
POLL_MAX_S = 25.0
ONLINE_FOR_S = POLL_MAX_S + 15.0   # seen this recently = online
JOB_TTL_S = 30.0                    # a job not picked up by then is dropped
MAX_SKILLS = 200
MAX_RESULT = 4000


class DeviceError(ValueError):
    pass


class _Device:
    def __init__(self) -> None:
        self.skills: frozenset[str] = frozenset()
        self.seen = 0.0
        self.queue: list[dict[str, Any]] = []
        self.wake = threading.Condition()


class Devices:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._devices: dict[str, _Device] = {}
        self._waiting: dict[str, tuple[str, cf.Future, float]] = {}  # job id -> (device, future, queued at)
        self._guard = threading.Lock()

    # --- the device side (HTTP threads) --------------------------------------

    def _device(self, name: Any) -> tuple[str, _Device]:
        if not isinstance(name, str) or not DEVICE_RE.fullmatch(name):
            raise DeviceError("device must be a short lowercase name like 'laptop'")
        with self._guard:
            dev = self._devices.get(name)
            if dev is None:
                if len(self._devices) >= 8:
                    raise DeviceError("too many devices")
                dev = self._devices[name] = _Device()
            return name, dev

    def poll(self, body: Any) -> dict[str, Any]:
        """A device asks for work: {"device": "laptop", "skills": [...]?, "wait": s}."""
        if not isinstance(body, dict):
            raise DeviceError("body must be a JSON object")
        name, dev = self._device(body.get("device"))
        skills = body.get("skills")
        if skills is not None:
            if (not isinstance(skills, list) or len(skills) > MAX_SKILLS
                    or not all(isinstance(s, str) and SKILL_RE.fullmatch(s) for s in skills)):
                raise DeviceError("skills must be a list of skill names")
            dev.skills = frozenset(skills)
        wait = body.get("wait", POLL_MAX_S)
        wait = min(max(float(wait), 0.0), POLL_MAX_S) if isinstance(wait, (int, float)) else POLL_MAX_S
        deadline = self._clock() + wait
        with dev.wake:
            dev.seen = self._clock()
            while True:
                self._expire(name, dev)
                if dev.queue:
                    jobs, dev.queue = dev.queue, []
                    return {"jobs": [public(j) for j in jobs]}
                left = deadline - self._clock()
                if left <= 0:
                    return {"jobs": []}
                dev.wake.wait(min(left, 1.0))
                dev.seen = self._clock()

    def result(self, body: Any) -> dict[str, Any]:
        """A device reports a finished job: {"device", "id", "text", "success", "resolved"?}."""
        if not isinstance(body, dict):
            raise DeviceError("body must be a JSON object")
        name, _dev = self._device(body.get("device"))
        jid = body.get("id")
        with self._guard:
            entry = self._waiting.get(jid) if isinstance(jid, str) else None
            if entry is None or entry[0] != name:
                raise DeviceError("no such job for this device")
            del self._waiting[jid]
        _, fut, _ = entry
        text = body.get("text")
        out = {"text": str(text)[:MAX_RESULT] if text is not None else "",
               "success": body.get("success") is True}
        if isinstance(body.get("resolved"), dict):
            out["resolved"] = {str(k)[:40]: str(v)[:300] for k, v in list(body["resolved"].items())[:10]}
        if not fut.done():
            fut.set_result(out)
        return {"ok": True}

    def _expire(self, name: str, dev: _Device) -> None:
        now = self._clock()
        keep = []
        for job in dev.queue:
            if now - job["_at"] > JOB_TTL_S:
                self._fail(job["id"], "the laptop didn't pick that up in time")
            else:
                keep.append(job)
        dev.queue = keep

    def _fail(self, jid: str, why: str) -> None:
        with self._guard:
            entry = self._waiting.pop(jid, None)
        if entry is not None and not entry[1].done():
            entry[1].set_result({"text": why, "success": False, "failed": True})

    # --- the hub side (NOVA's turns) ------------------------------------------

    def online(self, name: str) -> bool:
        with self._guard:
            dev = self._devices.get(name)
        return dev is not None and self._clock() - dev.seen < ONLINE_FOR_S

    def skills(self, name: str) -> frozenset[str]:
        with self._guard:
            dev = self._devices.get(name)
        return dev.skills if dev is not None and self.online(name) else frozenset()

    def submit(self, name: str, kind: str, skill: str, params: dict[str, Any]) -> cf.Future:
        """Queue one job; the future gets {"text", "success", ...}."""
        if kind not in ("run", "resolve", "update"):
            raise DeviceError("kind must be run, resolve or update")
        _, dev = self._device(name)
        jid = secrets.token_hex(8)
        fut: cf.Future = cf.Future()
        job = {"id": jid, "kind": kind, "skill": skill, "params": params, "_at": self._clock()}
        with self._guard:
            self._waiting[jid] = (name, fut, job["_at"])
        with dev.wake:
            dev.queue.append(job)
            dev.wake.notify_all()
        return fut

    def give_up(self, jid_future: cf.Future) -> None:
        with self._guard:
            for jid, (_, fut, _) in list(self._waiting.items()):
                if fut is jid_future:
                    del self._waiting[jid]
        if not jid_future.done():
            jid_future.cancel()

    def status(self) -> dict[str, Any]:
        with self._guard:
            names = list(self._devices)
        return {n: {"online": self.online(n), "skills": len(self.skills(n))} for n in names}


def public(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if not k.startswith("_")}
