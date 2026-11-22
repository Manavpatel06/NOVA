"""`python -m nova.hub serve | selftest` -- see nova/hub/__init__.py."""
from __future__ import annotations

import argparse
import sys
import time

from nova import DATA_DIR
from nova.config import load_config
from nova.hub.local_model import LocalModel
from nova.log import get_logger
from nova.security.audit import AuditLog
from nova.security.egress import Egress

_log = get_logger("hub")

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


def _model(config) -> LocalModel:
    audit = AuditLog(DATA_DIR / "hub-audit.log", redact_terms=config.security.redact_terms)
    egress = Egress([], audit=audit, redact_terms=config.security.redact_terms,
                    loopback_ports=[config.hub.ollama_port])
    return LocalModel(egress, model=config.hub.model, port=config.hub.ollama_port)


def _tools() -> list[dict]:
    import nova.skills  # noqa: F401 -- registers the skills
    from nova.brain.hub_tier import catalogue
    from nova.skills.registry import all_skills

    return catalogue(all_skills())


def serve(config) -> int:
    from nova.hub.listener import make_server
    from nova.hub.service import HubService

    model = _model(config)
    for attempt in range(30):  # Ollama may still be starting after a reboot
        try:
            model.warm_up()
            _log.info("Local model %s loaded", model.model)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 29:
                _log.error("Local model not ready (%s); serving anyway, health will say so", exc)
            time.sleep(2)
    service = HubService(model=model, data_dir=DATA_DIR)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nova.hub")
    parser.add_argument("command", choices=["serve", "selftest"])
    args = parser.parse_args(argv)
    config = load_config()
    return serve(config) if args.command == "serve" else selftest(config)


if __name__ == "__main__":
    sys.exit(main())
