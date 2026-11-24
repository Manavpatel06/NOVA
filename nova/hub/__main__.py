"""`python -m nova.hub serve | selftest` -- see nova/hub/__init__.py."""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from nova import DATA_DIR
from nova.config import load_config
from nova.hub.local_model import LocalModel
from nova.hub.notify import HOST as BARK_HOST, Bark
from nova.log import get_logger
from nova.security.audit import AuditLog
from nova.security.egress import Egress

_log = get_logger("hub")
TICK_S = 10

#: sentences for `selftest`: what the fast lane should take, and what it
#: should hand to Claude.
SELFTEST = [
    ("turn the volume up", "tool"),
    ("mute", "tool"),
    ("what time is it", "tool"),
    ("take a screenshot", "tool"),
    ("thanks nova", "chat"),
    ("hey, how are you", "chat"),
    ("write me a poem about Tempe", "escalate"),
    ("what's the capital of Australia and why", "escalate"),
    ("open youtube and then play lofi", "escalate"),
    ("turn it up a bit more", "escalate"),
]


def _audit(config) -> AuditLog:
    return AuditLog(DATA_DIR / "hub-audit.log", redact_terms=config.security.redact_terms)


def _egress(config, audit: AuditLog | None = None) -> Egress:
    # the hub reaches out to exactly one internet host: Bark, for alerts
    # (the `claude` and `git` programs it runs make their own connections)
    return Egress([BARK_HOST], audit=audit or _audit(config), redact_terms=config.security.redact_terms,
                  loopback_ports=[config.hub.ollama_port])


def _brain(config, egress: Egress, audit: AuditLog):
    """NOVA's Claude conversation, on the hub (ADR 0021)."""
    from nova.brain.claude_arm import ClaudeSession
    from nova.hub.brain_host import BrainHost

    brain_dir = DATA_DIR / "brain"  # empty working dir: nothing for the CLI to read
    brain_dir.mkdir(parents=True, exist_ok=True)

    def factory(system_prompt: str, web_search: bool, model: str) -> ClaudeSession:
        return ClaudeSession(egress=egress, audit=audit, model=model, system_prompt=system_prompt,
                             web_search=web_search, cwd=str(brain_dir))

    return BrainHost(factory)


def _forge(egress: Egress, notify):
    """NOVA upgrading itself (ADR 0021); None until the workshop is set up."""
    from nova import ROOT
    from nova.hub.forge import Forge

    home = Path.home() / "NOVA-forge"
    python = home / ".venv" / "bin" / "python"
    if not python.exists():
        _log.warning("Upgrades are off: the workshop isn't set up (run setup_hub.sh again)")
        return None
    forge = Forge(repo=ROOT, work_root=home / "work", state_dir=DATA_DIR / "forge", test_python=python,
                  notify=notify, egress=egress)
    forge.start_worker()
    return forge


def _model(config, egress: Egress | None = None) -> LocalModel:
    return LocalModel(egress or _egress(config), model=config.hub.model, port=config.hub.ollama_port)


def _bark(config, egress: Egress) -> Bark:
    def hub_name() -> str:
        try:
            return (DATA_DIR / "hub_name").read_text(encoding="utf-8").strip().rstrip(".")
        except OSError:
            return ""

    return Bark(egress, key_path=DATA_DIR / "bark_key", hub_name=hub_name, show_text=config.hub.alert_text)


def _tools() -> list[dict]:
    import nova.skills  # noqa: F401 -- registers the skills
    from nova.brain.hub_tier import catalogue
    from nova.skills.registry import all_skills

    return catalogue(all_skills())


def serve(config) -> int:
    from nova.hub.listener import make_server
    from nova.hub.service import HubService

    audit = _audit(config)
    egress = _egress(config, audit)
    model = _model(config, egress)
    for attempt in range(30):  # Ollama may still be starting after a reboot
        try:
            model.warm_up()
            _log.info("Local model %s loaded", model.model)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 29:
                _log.error("Local model not ready (%s); serving anyway, health will say so", exc)
            time.sleep(2)
    bark = _bark(config, egress)
    service = HubService(model=model, data_dir=DATA_DIR, notifier=bark, brain=_brain(config, egress, audit),
                         forge=_forge(egress, bark.say))

    def scheduler() -> None:
        while True:
            try:
                service.tick()
            except Exception as exc:  # noqa: BLE001 -- the next tick tries again
                _log.error("reminder tick failed (%s)", type(exc).__name__)
            time.sleep(TICK_S)

    threading.Thread(target=scheduler, name="reminders", daemon=True).start()
    server = make_server(service, config.hub.port)
    _log.info("nova-hub listening on 127.0.0.1:%d", config.hub.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def selftest(config) -> int:
    model = _model(config)
    tools = _tools()
    print(f"Model {model.model}, {len(tools)} tools. First answer is slower (model + prompt load).")
    good = 0
    for text, want in SELFTEST:
        d = model.route(text, tools)
        got = d["route"]
        # a fast-lane miss that escalates is safe; acting when it shouldn't isn't
        ok = got == want or (got == "escalate")
        good += got == want
        mark = "ok " if got == want else ("~  " if ok else "BAD")
        extra = f" {d['tool']}{d['params']}" if got == "tool" else (f" {d['say']!r}" if got == "chat" else "")
        print(f"  {mark} {d.get('ms', 0):>6} ms  {text!r} -> {got}{extra}")
    print(f"{good}/{len(SELFTEST)} as expected (~ = handed to Claude, safe but slower; BAD = should not happen)")
    return 0


def ping(config) -> int:
    bark = _bark(config, _egress(config))
    if not bark.configured():
        print("No Bark key yet: install Bark on your iPhone, copy your key, then run  nova-hub bark <key>")
        return 1
    ok = bark.test()
    print("Sent -- check your iPhone." if ok else "Couldn't reach Bark; see nova-hub logs.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nova.hub")
    parser.add_argument("command", choices=["serve", "selftest", "ping"])
    args = parser.parse_args(argv)
    config = load_config()
    return {"serve": serve, "selftest": selftest, "ping": ping}[args.command](config)


if __name__ == "__main__":
    sys.exit(main())
