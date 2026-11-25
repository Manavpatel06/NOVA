"""End-to-end check of the laptop -> hub path, using NOVA's own code and config.

Run from D:\\NOVA:  .venv\\Scripts\\python.exe scripts\\probe_hub.py

1. health over HTTPS (Tailscale), through the one Egress module
2. the Claude conversation on the hub: start, two turns (the second needs the first), close
3. reminders: add one far in the future, see it listed, cancel it
Nothing is spoken, no tool runs, and no memory is sent.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nova.brain.hub_tier import HubUnavailable, hub_post  # noqa: E402
from nova.brain.remote_session import RemoteSession  # noqa: E402
from nova.config import load_config  # noqa: E402
from nova.security.egress import Egress  # noqa: E402


class _NoAudit:
    """The probe's calls aren't part of NOVA's audit trail."""
    def record(self, entry: dict) -> None:
        pass


def line(ok: bool, what: str, extra: str = "") -> None:
    print(f"  {'ok ' if ok else 'BAD'}  {what}{('  ' + extra) if extra else ''}", flush=True)


async def main() -> int:
    config = load_config()
    host = config.hub.host.strip().lower()
    if not host:
        print("hub.host is empty in config/nova.yaml")
        return 1
    egress = Egress(list(config.security.egress_allowlist) + [host], audit=_NoAudit(),
                    redact_terms=config.security.redact_terms)
    print(f"Hub: {host}")
    bad = 0

    # 1. health
    t = time.monotonic()
    try:
        import json
        h = json.loads(await asyncio.to_thread(egress.fetch, f"https://{host}/v1/health", 15.0))
        line(bool(h.get("ok")), "health", f"{(time.monotonic() - t) * 1000:.0f} ms, brain={h.get('brain')} "
             f"forge={h.get('forge')} alerts_ready={h.get('alerts_ready')}")
    except Exception as exc:  # noqa: BLE001
        line(False, "health", type(exc).__name__ + f": {exc}"); return 1

    # 2. brain on the hub
    s = RemoteSession(config, egress, web_search=False, model=config.brain.model,
                      system_prompt="You are a test. Answer in five words or fewer.")
    try:
        t = time.monotonic(); await s.start()
        line(True, "brain start", f"{(time.monotonic() - t):.1f} s")
        t = time.monotonic(); r1 = await s.send("Remember the word: pineapple. Reply 'got it'.", timeout_s=90)
        line(not r1.is_error, "brain turn 1", f"{(time.monotonic() - t):.1f} s -> {r1.text.strip()[:60]!r}")
        t = time.monotonic(); r2 = await s.send("What word did I ask you to remember?", timeout_s=90)
        good = "pineapple" in r2.text.lower()
        line(good, "brain turn 2 (remembers turn 1)", f"{(time.monotonic() - t):.1f} s -> {r2.text.strip()[:60]!r}")
        bad += not good
    except Exception as exc:  # noqa: BLE001
        line(False, "brain", type(exc).__name__ + f": {exc}"); bad += 1
    finally:
        await s.close()

    # 3. reminders round trip (due in 30 days, cancelled straight away)
    try:
        out = await hub_post(config, egress, "/v1/reminders",
                             {"text": "NOVA probe (safe to ignore)", "due": time.time() + 30 * 86400, "urgent": False})
        rid = out.get("id")
        listed = await hub_post(config, egress, "/v1/reminders/list", {})
        seen = any(r.get("id") == rid for r in listed.get("reminders", []))
        gone = await hub_post(config, egress, "/v1/reminders/cancel", {"id": rid})
        line(bool(rid) and seen and gone.get("cancelled", 0) >= 1, "reminder add / list / cancel")
    except HubUnavailable as exc:
        line(False, "reminders", str(exc)); bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
