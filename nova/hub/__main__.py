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
    # the hub reaches out to Bark (alerts) and, now that NOVA itself runs
    # here (ADR 0023), Open-Meteo for the weather skill -- nothing else
    # (the `claude` and `git` programs it runs make their own connections)
    from nova.hub.phone import API_HOST as TWILIO_HOST
    from nova.hub.phone import PLIVO_HOST
    from nova.skills.weather import FORECAST_URL, GEOCODE_URL

    # + Twilio's API, used only when phone calls are set up (ADR 0025)
    from nova.brain.agent import API_URL

    # + Anthropic's API when NOVA thinks with Manav's API credit (ADR 0026)
    hosts = [BARK_HOST, FORECAST_URL.split("/")[2], GEOCODE_URL.split("/")[2], TWILIO_HOST, PLIVO_HOST,
             API_URL.split("/")[2]]
    # Claude's API is called every turn: keep that connection open (ADR 0033)
    return Egress(hosts, audit=audit or _audit(config), redact_terms=config.security.redact_terms,
                  loopback_ports=[config.hub.ollama_port], keep_alive_hosts=[API_URL.split("/")[2]])


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


def _restart_soon(delay_s: float = 15.0) -> None:
    """Exit after the reply has been spoken; systemd (Restart=always) starts
    the hub again with the new code."""
    import os

    def go() -> None:
        _log.info("Restarting to load an installed upgrade")
        os._exit(0)

    threading.Timer(delay_s, go).start()


def _stt_model(config) -> str:
    """The hub's speech-to-text model: config, or `nova-hub hearing fast|accurate`
    (a file in data/, so the tracked config file stays untouched on the hub)."""
    import re

    try:
        chosen = (DATA_DIR / "stt_model").read_text(encoding="utf-8").strip()
    except OSError:
        chosen = ""
    return chosen if re.fullmatch(r"(?:tiny|base|small|medium)\.en", chosen) else config.hub.stt_model


def _connectors():
    """Connected apps (ADR 0030): the MCP servers `nova-hub connect` set up."""
    from nova.hub.connectors import Connectors

    def builtin() -> set:
        import nova.skills  # noqa: F401 -- registers the skills
        from nova.skills.registry import all_skills

        return set(all_skills())

    c = Connectors(DATA_DIR / "connectors.json", DATA_DIR / "connectors", builtin=builtin)
    c.start()   # in the background: NOVA answers while they start
    return c


def _talk(config, egress: Egress, audit: AuditLog, devices=None, face=None, connectors=None):
    """NOVA itself, on the hub (ADR 0023/0024): the assistant behind the talk
    page, the laptop link and the hub's own mic."""
    if not config.hub.assistant:
        return None
    from nova.hub.assistant import HubAssistant, hub_apply_upgrade
    from nova.hub.talk import Talk
    from nova.hub.voice import HubVoice

    _adopt_memory()
    from nova.hub.vocabulary import Vocabulary

    vocab = Vocabulary(DATA_DIR / "vocabulary.txt", names=config.wake.names,
                       apps=list(config.security.allowed_apps), connectors=connectors)
    voice = HubVoice(stt_model=_stt_model(config), voice=config.tts.voice, prompt=vocab.prompt)
    missing = voice.missing()
    if missing:
        _log.warning("Talk page is text-only until `nova-hub voice` installs: %s", ", ".join(missing))
    from nova.hub import hub_skills

    local = {"apply_upgrade": hub_apply_upgrade(devices, _restart_soon)}
    holder: dict = {}
    local.update(hub_skills.make(connectors=connectors, devices=devices, restart=_restart_soon,
                                 spend=lambda: getattr(holder.get("a"), "spend", None), vocabulary=vocab))
    assistant = HubAssistant(config, audit=audit, egress=egress, devices=devices, connectors=connectors,
                             local_skills=local)
    holder["a"] = assistant
    judge = None
    if config.hub.conversation_s > 0:
        from nova.hub.conversation import Judge

        judge = Judge(config, egress, spend=lambda: getattr(assistant, "spend", None))
    talk = Talk(assistant, voice, face, judge)
    talk.warm_up()
    return talk


def _hub_ears(config, talk) -> None:
    """Phase 3: the hub's own mic and speaker ("Nova, ..." in the room)."""
    import base64

    # on in config, or switched on with `nova-hub ears on` (a flag file, so the
    # tracked config file stays untouched on the hub)
    if not (config.hub.hub_ears or (DATA_DIR / "hub_ears").exists()) or talk is None:
        return
    if not talk.voice_ready():
        _log.warning("The hub's own mic is on in config, but voice isn't set up (nova-hub voice)")
        return
    from nova.ears import Ears

    def send(pcm16: bytes) -> dict:
        return talk.hear({"device": "hub", "audio": base64.b64encode(pcm16).decode("ascii")})

    ears = Ears(send, silence_ms=config.wake.vad_silence_ms, max_ms=config.wake.max_utterance_ms,
                min_speech_ms=config.wake.min_speech_ms,
                more=lambda sid, part: talk.more({"id": sid, "part": part}))
    threading.Thread(target=ears.run, args=(threading.Event(),), name="hub-ears", daemon=True).start()


def _adopt_memory() -> None:
    """The first time NOVA runs here, its memory starts as the copy the
    laptop mirrored (regular files only, one folder deep at most)."""
    import shutil

    from nova import MEMORY_DIR

    mirror = DATA_DIR / "memory-mirror"
    if MEMORY_DIR.exists() and any(MEMORY_DIR.iterdir()):
        return
    if not mirror.is_dir():
        return
    root = mirror.resolve()
    copied = 0
    for src in sorted(mirror.rglob("*")):
        rel = src.relative_to(mirror)
        if src.is_symlink() or not src.is_file() or len(rel.parts) > 2:
            continue
        if not src.resolve().is_relative_to(root):
            continue
        dst = MEMORY_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.parent.resolve().is_relative_to(MEMORY_DIR.resolve()) or dst.is_symlink():
            continue  # realpath on both sides (CLAUDE.md rule 3)
        shutil.copyfile(src, dst)
        copied += 1
    if copied:
        _log.info("NOVA's memory on the hub starts from the laptop's copy (%d files)", copied)


def _model(config, egress: Egress | None = None) -> LocalModel:
    return LocalModel(egress or _egress(config), model=config.hub.model, port=config.hub.ollama_port)


def _hub_name() -> str:
    try:
        return (DATA_DIR / "hub_name").read_text(encoding="utf-8").strip().rstrip(".")
    except OSError:
        return ""


def _bark(config, egress: Egress) -> Bark:
    hub_name = _hub_name
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
    if config.hub.local_tier:
        for attempt in range(30):  # Ollama may still be starting after a reboot
            try:
                model.warm_up()
                _log.info("Local model %s loaded", model.model)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 29:
                    _log.error("Local model not ready (%s); serving anyway, health will say so", exc)
                time.sleep(2)
    else:
        try:  # the local lane is off: give its memory to NOVA's voice instead
            model.unload()
        except Exception:  # noqa: BLE001 -- Ollama not running is fine too
            pass
    bark = _bark(config, egress)
    from nova.hub.devices import Devices

    from nova.hub.face import Face

    devices = Devices() if config.hub.assistant else None
    face = Face()
    face.devices = devices
    connectors = _connectors() if config.hub.assistant else None
    talk = _talk(config, egress, audit, devices, face, connectors)
    phone = None
    if talk is not None:
        from nova.hub.phone import Phone

        phone = Phone(talk, data_dir=DATA_DIR, hub_name=_hub_name, egress=egress)
    from nova.hub.builder import Builder

    # projects land in the shared drive (\\nova-hub\nova\Projects = H:\Projects)
    builder = Builder(projects=Path.home() / "Shared" / "Projects", state=DATA_DIR / "builds.json",
                      notify=bark.say, egress=egress)
    builder.recover()
    service = HubService(model=model, data_dir=DATA_DIR, notifier=bark, brain=_brain(config, egress, audit),
                         forge=_forge(egress, bark.say), talk=talk, devices=devices, phone=phone, builder=builder, face=face)
    service.connectors = connectors
    _hub_ears(config, talk)
    if service.talk is not None:
        from nova.brain.hub_tier import use_local_service

        use_local_service(service)  # NOVA's own skills call this service directly

    def scheduler() -> None:
        while True:
            try:
                service.tick()
            except Exception as exc:  # noqa: BLE001 -- the next tick tries again
                _log.error("reminder tick failed (%s)", type(exc).__name__)
            time.sleep(TICK_S)

    threading.Thread(target=scheduler, name="reminders", daemon=True).start()

    from nova.hub.watch import Watch

    watch = Watch(bark.say, connectors=connectors, state_path=DATA_DIR / "watch.json",
                  heads_up_hour=config.hub.heads_up_hour)

    def watcher() -> None:   # NOVA warns first: power, heat, disk, the morning heads-up
        while True:
            time.sleep(60)
            try:
                watch.tick()
            except Exception as exc:  # noqa: BLE001
                _log.error("watch tick failed (%s)", type(exc).__name__)

    threading.Thread(target=watcher, name="watch", daemon=True).start()
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


def phone_setup(config) -> int:
    """`nova-hub phone`: settings arrive as JSON on stdin (never argv)."""
    import json

    from nova.hub.phone import PhoneError, write_settings

    try:
        values = json.loads(sys.stdin.read())
        write_settings(DATA_DIR / "phone.json", **{k: str(values.get(k, "")).strip() for k in (
            "account_sid", "auth_token", "nova_number", "owner_number", "pin", "provider")})
    except (PhoneError, ValueError, TypeError) as exc:
        print(f"Not saved: {exc}")
        return 1
    print("Saved.")
    return 0


def apikey_setup(config) -> int:
    """`nova-hub apikey`: the key arrives on stdin (never argv), is saved 0600,
    then proven with one tiny request."""
    from nova.brain.agent import check_key
    from nova.brain.keys import set_file_key

    try:
        set_file_key(sys.stdin.read())
    except ValueError as exc:
        print(f"Not saved: {exc}")
        return 1
    ok, message = check_key()
    print(message)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nova.hub")
    parser.add_argument("command", choices=["serve", "selftest", "ping", "phone-setup", "apikey-setup"])
    args = parser.parse_args(argv)
    config = load_config()
    return {"serve": serve, "selftest": selftest, "ping": ping, "phone-setup": phone_setup,
            "apikey-setup": apikey_setup}[args.command](config)


if __name__ == "__main__":
    sys.exit(main())
