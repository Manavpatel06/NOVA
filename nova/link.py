"""NOVA Link: the laptop's small helper once NOVA lives on the hub (ADR 0024).

    pythonw -m nova.link        (scripts/install_link.ps1 starts it at sign-in)

What it does, and nothing else:
  * runs this laptop's skills (volume, apps, screenshots, power...) when NOVA
    on the hub asks. The hub has already approved each one through its
    ladder (tier, spoken "Nova, confirm" for risky ones); this side checks
    the skill exists and the parameters fit, runs it, and audits it here too.
  * (hub.listen) keeps the laptop mic open for "Nova, ..." -- sentences go
    to the hub, which acts only on ones addressed to NOVA -- and plays the
    replies on the laptop's speakers.

No AI models run here: speech-to-text, the voice and Claude are on the hub.
It connects OUT to the hub over Tailscale (long-polling), so the laptop
opens no port, and only the hub's own name is on its egress allowlist.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import sys
import threading
import time
from typing import Any

from nova.log import get_logger

_log = get_logger("link")

DEVICE = "laptop"
POLL_WAIT_S = 25.0
MAX_BACKOFF_S = 30.0


class Link:
    def __init__(self, config: Any, *, egress: Any, audit: Any, device: str = DEVICE) -> None:
        import nova.skills  # noqa: F401 -- registers the skills
        from nova.security.ladder import Ladder

        self.config = config
        self.egress = egress
        self.audit = audit
        self.device = device
        self.host = config.hub.host.strip().lower()
        # handlers never ask the ladder themselves; the hub already approved
        self._ladder = Ladder(audit)
        self._announced = False
        self.connected = False
        self.restart = False   # set after an upgrade is installed here

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        return self.egress.post_json(f"https://{self.host}{path}", body, headers={}, timeout=timeout)

    # --- jobs ---------------------------------------------------------------

    def skill_names(self) -> list[str]:
        from nova.skills.registry import all_skills

        return sorted(all_skills())

    async def serve(self, stop: asyncio.Event) -> None:
        backoff = 2.0
        running: set[asyncio.Task] = set()
        while not stop.is_set() and not self.restart:
            body: dict[str, Any] = {"device": self.device, "wait": POLL_WAIT_S}
            if not self._announced:
                body["skills"] = self.skill_names()
            try:
                out = await asyncio.to_thread(self._post, "/v1/link/poll", body, POLL_WAIT_S + 10)
            except Exception as exc:  # noqa: BLE001 -- hub restarting, Wi-Fi down...
                if self.connected:
                    _log.warning("Lost the hub (%s); retrying", type(exc).__name__)
                self.connected = False
                self._announced = False
                await _sleep(stop, backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            if not self.connected:
                _log.info("Connected to NOVA on the hub (%s)", self.host)
            self.connected = True
            self._announced = True
            backoff = 2.0
            for job in out.get("jobs", []) if isinstance(out, dict) else []:
                task = asyncio.create_task(self._job(job))
                running.add(task)
                task.add_done_callback(running.discard)
        if running:  # finish (and report) what's in flight before leaving
            await asyncio.wait(running, timeout=30)

    async def _job(self, job: Any) -> None:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            return
        result = await self.run_job(job)
        result.update(device=self.device, id=job["id"])
        try:
            await asyncio.to_thread(self._post, "/v1/link/result", result, 15.0)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Couldn't send a result back to the hub (%s)", type(exc).__name__)

    async def run_job(self, job: dict) -> dict:
        from nova.security.tiers import ArmedBy
        from nova.skills.registry import SkillCtx, get_skill

        name, kind, params = job.get("skill"), job.get("kind"), job.get("params")
        if kind == "update":
            return await self._update(params)
        spec = get_skill(name) if isinstance(name, str) else None
        if spec is None or kind not in ("run", "resolve") or not isinstance(params, dict):
            return {"text": "This laptop can't do that.", "success": False}
        ctx = SkillCtx(config=self.config, ladder=self._ladder, egress=self.egress, armed_by=ArmedBy.PTT)
        if kind == "resolve":
            if spec.resolve_for_confirm is None:
                return {"text": "", "success": True}
            try:
                resolved = await spec.resolve_for_confirm(ctx, params)
            except Exception as exc:  # noqa: BLE001 -- the question then shows the raw name
                _log.warning("resolve %s failed (%s)", spec.name, type(exc).__name__)
                resolved = None
            return {"text": "", "success": True, "resolved": resolved if isinstance(resolved, dict) else None}
        try:
            inspect.signature(spec.handler).bind(ctx, **params)
        except TypeError as exc:
            return {"text": f"bad parameters for {spec.name}: {exc}", "success": False}
        started = time.monotonic()
        try:
            reply = await spec.handler(ctx, **params)
            text, ok = reply.text, bool(reply.success)
        except Exception as exc:  # noqa: BLE001 -- report, never crash the helper
            _log.error("%s raised (%s)", spec.name, type(exc).__name__)
            text, ok = f"{spec.name.replace('_', ' ')} failed on the laptop", False
        self.audit.record({"kind": "link.run", "skill": spec.name, "params": params, "success": ok,
                           "ms": int((time.monotonic() - started) * 1000), "approved_on": "hub"})
        _log.info("Ran %s for NOVA on the hub -> %s", spec.name, "ok" if ok else "failed")
        return {"text": text, "success": ok}

    async def _update(self, params: Any) -> dict:
        """Install the upgrade the hub just applied (Manav said "Nova,
        confirm" there): exactly that commit, then restart this helper."""
        import re

        from nova import ROOT
        from nova.system import winops

        commit = params.get("commit") if isinstance(params, dict) else None
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            return {"text": "not a version id", "success": False}
        try:
            await asyncio.to_thread(winops.git_update, str(ROOT), commit)
        except Exception as exc:  # noqa: BLE001
            _log.error("upgrade install failed (%s)", exc)
            return {"text": "The laptop couldn't install it safely.", "success": False}
        self.audit.record({"kind": "link.update", "commit": commit})
        _log.info("Installed upgrade %s; restarting NOVA Link", commit[:8])
        self.restart = True
        return {"text": "Installed on the laptop.", "success": True}

    # --- ears ---------------------------------------------------------------

    def more(self, speech_id: str, part: int) -> dict:
        return self._post("/v1/talk/more", {"id": speech_id, "part": part}, 40.0)

    def hear(self, pcm16: bytes) -> dict:
        return self._post("/v1/link/hear", {"device": self.device,
                                            "audio": base64.b64encode(pcm16).decode("ascii")}, 200.0)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


def _single_instance():
    """Hold a lock file for the process lifetime; None if another Link runs."""
    from nova import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = open(DATA_DIR / "link.lock", "a+")  # noqa: SIM115 -- held open on purpose
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main(argv: list[str] | None = None) -> int:
    from nova import DATA_DIR
    from nova.config import load_config
    from nova.security.audit import AuditLog
    from nova.security.egress import Egress

    lock = None
    for _ in range(15):  # a restarting Link may still be exiting
        lock = _single_instance()
        if lock is not None:
            break
        time.sleep(1)
    if lock is None:
        print("NOVA Link is already running.")
        return 0
    config = load_config()
    host = config.hub.host.strip().lower()
    if not host:
        _log.error("NOVA Link needs hub.host in config/nova.yaml (the hub's Tailscale name)")
        return 1
    audit = AuditLog(DATA_DIR / "link-audit.log", redact_terms=config.security.redact_terms)
    egress = Egress(list(config.security.egress_allowlist) + [host], audit=audit,
                    redact_terms=config.security.redact_terms, keep_alive_hosts=[host])
    link = Link(config, egress=egress, audit=audit)
    stop_thread = threading.Event()
    if config.hub.listen:
        from nova.ears import Ears

        ears = Ears(link.hear, listening=lambda: link.connected, more=link.more,
                    silence_ms=config.wake.vad_silence_ms, max_ms=config.wake.max_utterance_ms,
                    min_speech_ms=config.wake.min_speech_ms)
        threading.Thread(target=ears.run, args=(stop_thread,), name="ears", daemon=True).start()
    stop = asyncio.Event()
    try:
        asyncio.run(link.serve(stop))
    except KeyboardInterrupt:
        pass
    finally:
        stop_thread.set()
    if link.restart:
        from nova.system import winops

        lock.close()
        try:
            winops.restart_link()
        except Exception as exc:  # noqa: BLE001
            _log.error("Couldn't restart after the upgrade (%s); start NOVA Link again", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
