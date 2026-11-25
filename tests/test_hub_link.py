"""NOVA Link (ADR 0024): the hub's NOVA running laptop skills through the
helper, device mics, hub-side upgrades."""
from __future__ import annotations

import asyncio
import base64
import threading
import time
from dataclasses import dataclass

import pytest

import nova.skills  # noqa: F401 -- registers the skills
from nova.brain import hub_tier
from nova.config import Config
from nova.ears import Ears, from_wav, to_pcm16
from nova.hub.assistant import NOT_HERE, HubAssistant, hub_apply_upgrade
from nova.hub.devices import JOB_TTL_S, DeviceError, Devices
from nova.hub.talk import Talk
from nova.hub.voice import to_wav
from nova.link import Link
from nova.security.audit import AuditLog
from nova.skills.registry import all_skills


# --- the job queue -------------------------------------------------------------

def test_a_queued_job_reaches_the_device_and_its_result_comes_back():
    d = Devices()
    d.poll({"device": "laptop", "skills": ["volume_up"], "wait": 0})
    assert d.online("laptop") and d.skills("laptop") == {"volume_up"}
    fut = d.submit("laptop", "run", "volume_up", {})
    jobs = d.poll({"device": "laptop", "wait": 0})["jobs"]
    assert jobs == [{"id": jobs[0]["id"], "kind": "run", "skill": "volume_up", "params": {}}]  # nothing internal leaks
    d.result({"device": "laptop", "id": jobs[0]["id"], "text": "Volume up.", "success": True})
    assert fut.result(1) == {"text": "Volume up.", "success": True}


def test_a_waiting_poll_wakes_up_for_a_new_job():
    d = Devices()
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("r", d.poll({"device": "laptop", "wait": 5})))
    t.start()
    time.sleep(0.1)
    d.submit("laptop", "run", "mute", {})
    t.join(3)
    assert got["r"]["jobs"][0]["skill"] == "mute"


def test_stale_jobs_are_dropped_not_run_later():
    now = [1000.0]
    d = Devices(clock=lambda: now[0])
    fut = d.submit("laptop", "run", "shutdown", {})
    now[0] += JOB_TTL_S + 1
    assert d.poll({"device": "laptop", "wait": 0})["jobs"] == []
    assert fut.result(1)["success"] is False


def test_results_only_from_the_device_the_job_went_to():
    d = Devices()
    d.submit("laptop", "run", "mute", {})
    jid = d.poll({"device": "laptop", "wait": 0})["jobs"][0]["id"]
    with pytest.raises(DeviceError):
        d.result({"device": "desktop", "id": jid, "text": "x", "success": True})
    with pytest.raises(DeviceError):
        d.result({"device": "laptop", "id": "nope", "text": "x", "success": True})


@pytest.mark.parametrize("body", [
    None, {}, {"device": "Laptop!"}, {"device": "x"}, {"device": "laptop", "skills": "volume_up"},
    {"device": "laptop", "skills": ["rm -rf"]}, {"device": "laptop", "skills": ["a"] * 500},
])
def test_bad_polls_are_refused(body):
    with pytest.raises(DeviceError):
        Devices().poll(body)


def test_offline_after_silence():
    now = [0.0]
    d = Devices(clock=lambda: now[0])
    d.poll({"device": "laptop", "skills": ["mute"], "wait": 0})
    now[0] += 120
    assert not d.online("laptop") and d.skills("laptop") == frozenset()


# --- the hub's NOVA running laptop skills ----------------------------------------------

class _FakeLaptop:
    """Polls the hub's queue like nova.link does, and records what it ran."""

    def __init__(self, devices, answer="done on the laptop", resolved=None):
        self.devices = devices
        self.ran: list[tuple[str, str, dict]] = []
        self.answer = answer
        self.resolved = resolved
        self._stop = threading.Event()
        devices.poll({"device": "laptop", "skills": sorted(all_skills()), "wait": 0})
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            for job in self.devices.poll({"device": "laptop", "wait": 0.2})["jobs"]:
                self.ran.append((job["kind"], job["skill"], job["params"]))
                extra = {"resolved": self.resolved} if job["kind"] == "resolve" else {}
                self.devices.result({"device": "laptop", "id": job["id"], "text": self.answer,
                                     "success": True, **extra})

    def stop(self):
        self._stop.set()
        self._t.join(2)


@dataclass
class _Result:
    text: str


class _Brain:
    def __init__(self, calls):
        self.calls = calls
        self.results = []

    def available(self):
        return True

    def note(self, *_):
        pass

    async def respond(self, text, invoke_tool):
        for name, params in self.calls:
            self.results.append(await invoke_tool(name, params, False))
        return _Result("ok")


class _Channel:
    name = "test"

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.asked = []

    async def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else None


def _assistant(tmp_path, devices, brain=None):
    return HubAssistant(Config(), audit=AuditLog(tmp_path / "a.log"), egress=None, devices=devices,
                        brain=brain or _Brain([]), journal=False)


def test_every_nova_skill_is_offered_once_a_device_link_exists(tmp_path):
    names = set(_assistant(tmp_path, Devices()).skills())
    assert {"volume_up", "screenshot", "open_app", "time", "remind_me"} <= names
    assert names == set(all_skills()) - {"apply_upgrade"}  # apply_upgrade is the hub's own (local_skills)


async def test_a_laptop_command_runs_on_the_laptop(tmp_path):
    devices = Devices()
    laptop = _FakeLaptop(devices, answer="Volume up to 60.")
    try:
        reply = await _assistant(tmp_path, devices).turn("turn the volume up", _Channel())
    finally:
        laptop.stop()
    assert reply == "Volume up to 60."
    assert laptop.ran and laptop.ran[0][:2] == ("run", "volume_up")


async def test_with_the_laptop_offline_nova_says_so_without_asking_or_queueing(tmp_path):
    devices = Devices()
    brain = _Brain([("shutdown_computer", {})])
    ch = _Channel(answers=["Nova, confirm"])
    reply = await _assistant(tmp_path, devices, brain).turn("shut it down", ch)
    assert reply == "ok" and brain.results[0] == (NOT_HERE, False)
    assert ch.asked == []  # never asked to approve something that can't happen
    assert devices.poll({"device": "laptop", "wait": 0})["jobs"] == []


async def test_a_risky_laptop_action_needs_nova_confirm_on_the_hub_first(tmp_path):
    risky = sorted(n for n, s in all_skills().items() if s.tier.name == "CONFIRM" and n != "apply_upgrade"
                   and n not in ("upgrade_nova",))
    assert risky, "expected at least one CONFIRM-tier laptop skill"
    name = risky[0]
    params = {p: "x" for p in list(__import__("inspect").signature(all_skills()[name].handler).parameters)[1:]
              if __import__("inspect").signature(all_skills()[name].handler).parameters[p].default
              is __import__("inspect").Parameter.empty}
    for answer, should_run in (("yes", False), ("Nova, confirm", True)):
        devices = Devices()
        laptop = _FakeLaptop(devices)
        brain = _Brain([(name, params)])
        try:
            await _assistant(tmp_path, devices, brain).turn("do the risky thing", _Channel(answers=[answer]))
        finally:
            laptop.stop()
        ran = [r for r in laptop.ran if r[0] == "run"]
        assert bool(ran) is should_run, (answer, laptop.ran)


async def test_the_approval_question_shows_what_the_laptop_resolved(tmp_path):
    spec = all_skills().get("open_app")
    if spec is None or spec.resolve_for_confirm is None:
        pytest.skip("open_app has no resolve step")
    devices = Devices()
    laptop = _FakeLaptop(devices, resolved={"app": "C:\\Program Files\\Spotify\\Spotify.exe"})
    ch = _Channel(answers=["no"])
    brain = _Brain([("open_app", {"app": "spotify"})])

    class Strict(HubAssistant):
        pass

    a = _assistant(tmp_path, devices, brain)
    a._strict = False
    try:
        await a.turn("open spotify", ch)
    finally:
        laptop.stop()
    kinds = [k for k, _, _ in laptop.ran]
    assert "resolve" in kinds
    if ch.asked:  # open_app asked for approval: it shows the resolved path, and "no" ran nothing
        assert "Spotify.exe" in ch.asked[0] and "run" not in kinds


# --- device mics -----------------------------------------------------------------------

class _Echo:
    config = Config()

    async def turn(self, text, channel):
        if text == "ask me":
            return f"answer was {await channel.ask('Sure?')}"
        return f"did: {text} ({channel.armed_by.name})"

    async def warm_up(self):
        pass


def test_speech_not_addressed_to_nova_is_dropped_without_its_words():
    out = Talk(_Echo()).hear_text("laptop", "so anyway I told him the password", want_audio=False)
    assert out == {"ignored": True}


def test_addressed_speech_runs_as_voice_not_as_a_button_press():
    out = Talk(_Echo()).hear_text("laptop", "Nova, what time is it", want_audio=False)
    assert out["say"].startswith("did: what time is it") and "VOICE" in out["say"]


class _EchoNoConversation(_Echo):
    import dataclasses as _dc
    config = _dc.replace(Config(), hub=_dc.replace(Config().hub, conversation_s=0.0))


def test_a_bare_name_opens_a_short_follow_up():
    talk = Talk(_EchoNoConversation())   # conversation mode off: exactly one sentence (ADR 0029 covers "on")
    assert talk.hear_text("laptop", "Nova.", want_audio=False)["say"] == "Yes?"
    assert talk.hear_text("laptop", "open spotify", want_audio=False)["say"].startswith("did: open spotify")
    assert talk.hear_text("laptop", "and close it", want_audio=False) == {"ignored": True}  # only one


def test_an_answer_to_nova_needs_no_name():
    talk = Talk(_Echo())
    first = talk.hear_text("laptop", "Nova, ask me", want_audio=False)
    assert first["say"] == "Sure?" and first["awaiting"] is True
    assert talk.hear_text("laptop", "yes", want_audio=False)["say"] == "answer was yes"


def test_bad_device_bodies_are_refused():
    from nova.hub.service import HubError

    for body in (None, {"device": "Laptop", "audio": "AA"}, {"device": "laptop"}):
        with pytest.raises(HubError):
            Talk(_Echo()).hear(body)


# --- the ears loop's send/play half ---------------------------------------------------------

def test_ears_play_replies_and_ignore_the_rest():
    played = []
    wav = to_wav(b"\x00\x10" * 800, 16000)
    replies = iter([{"ignored": True}, {"say": "hi", "audio": base64.b64encode(wav).decode()}])
    ears = Ears(lambda pcm: next(replies), play=played.append)
    ears.handle(b"\x00\x00" * 100)
    assert played == []
    ears.handle(b"\x00\x00" * 100)
    assert played == [wav]


def test_ears_survive_a_hub_that_is_down():
    def down(_):
        raise ConnectionError("hub restarting")

    assert Ears(down, play=lambda _: None).handle(b"\x00\x00") == {}


def test_pcm_round_trip():
    import numpy as np

    frames, rate = from_wav(to_wav(to_pcm16(np.array([0.0, 0.5, -0.5, 1.5], dtype=np.float32)), 16000))
    assert rate == 16000 and np.allclose(frames, [0.0, 0.5, -0.5, 1.0], atol=1e-3)


# --- the laptop helper's side -------------------------------------------------------------------

class _NoPost:
    def post_json(self, *a, **k):
        raise AssertionError("no network in this test")


def _link(tmp_path):
    import dataclasses

    from nova.config import HubConfig

    cfg = dataclasses.replace(Config(), hub=dataclasses.replace(HubConfig(), host="nova-hub.tail1.ts.net"))
    return Link(cfg, egress=_NoPost(), audit=AuditLog(tmp_path / "link.log"))


async def test_the_helper_runs_a_real_skill_and_audits_it(tmp_path):
    link = _link(tmp_path)
    out = await link.run_job({"id": "1", "kind": "run", "skill": "time", "params": {}})
    assert out["success"] is True and out["text"]
    assert "link.run" in (tmp_path / "link.log").read_text()


@pytest.mark.parametrize("job", [
    {"id": "1", "kind": "run", "skill": "no_such_skill", "params": {}},
    {"id": "1", "kind": "explode", "skill": "time", "params": {}},
    {"id": "1", "kind": "run", "skill": "time", "params": "x"},
    {"id": "1", "kind": "run", "skill": "time", "params": {"surprise": 1}},
    {"id": "1", "kind": "update", "params": {"commit": "main; rm -rf /"}},
])
async def test_the_helper_refuses_what_doesnt_fit(tmp_path, job):
    out = await _link(tmp_path).run_job(job)
    assert out["success"] is False


# --- upgrades applied by the hub ---------------------------------------------------------------

class _ForgeService:
    def __init__(self):
        self.calls = []

    def call(self, path, body):
        self.calls.append(path)
        if path == "/v1/forge/ready":
            return {"ready": {"id": "j1", "commit": "a" * 40, "request": "a coin flip", "files": ["x.py"]}}
        if path == "/v1/forge/apply":
            return {"ok": True, "pushed": True}
        return {}


async def test_applying_an_upgrade_installs_it_here_tells_the_laptop_and_restarts(tmp_path):
    svc = _ForgeService()
    hub_tier.use_local_service(svc)
    devices = Devices()
    laptop = _FakeLaptop(devices, answer="Installed on the laptop.")
    restarted = []
    try:
        a = HubAssistant(Config(), audit=AuditLog(tmp_path / "a.log"), egress=None, devices=devices,
                         brain=_Brain([("apply_upgrade", {})]), journal=False,
                         local_skills={"apply_upgrade": hub_apply_upgrade(devices, lambda: restarted.append(1))})
        ch = _Channel(answers=["Nova, confirm"])
        await a.turn("install the upgrade", ch)
    finally:
        laptop.stop()
        hub_tier.use_local_service(None)
    assert "a coin flip" in ch.asked[0]                      # read back before approval
    assert "/v1/forge/apply" in svc.calls and restarted == [1]
    assert ("update", "apply_upgrade", {"commit": "a" * 40}) in laptop.ran


async def test_an_unconfirmed_upgrade_installs_nothing(tmp_path):
    svc = _ForgeService()
    hub_tier.use_local_service(svc)
    restarted = []
    try:
        a = HubAssistant(Config(), audit=AuditLog(tmp_path / "a.log"), egress=None, devices=Devices(),
                         brain=_Brain([("apply_upgrade", {})]), journal=False,
                         local_skills={"apply_upgrade": hub_apply_upgrade(None, lambda: restarted.append(1))})
        await a.turn("install the upgrade", _Channel(answers=["yes"]))
    finally:
        hub_tier.use_local_service(None)
    assert "/v1/forge/apply" not in svc.calls and restarted == []


# --- the real wire: Link <-> hub listener over HTTP ------------------------------------------

async def test_link_and_hub_talk_over_http_end_to_end(tmp_path, monkeypatch):
    """A real Link.serve() long-polls a real hub listener; the hub's NOVA asks
    for a laptop skill; the result comes back over the same HTTP routes."""
    import dataclasses
    import json
    import urllib.request

    from nova.config import HubConfig
    from nova.hub.listener import OWNER_HEADER, make_server
    from nova.hub.service import HubService

    (tmp_path / "hub_owner").write_text("manav@example.com\n")
    (tmp_path / "hub_name").write_text("nova-hub.tail1.ts.net\n")

    class _M:
        model = "m"

    devices = Devices()
    svc = HubService(model=_M(), data_dir=tmp_path, devices=devices)
    server = make_server(svc, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    addr = server.server_address

    class _Egress:  # what Tailscale + nova.security.egress do, minus TLS
        def post_json(self, url, body, *, headers, timeout=20.0, method="POST"):
            path = url.split("nova-hub.tail1.ts.net", 1)[1]
            req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}", data=json.dumps(body).encode(),
                                         method=method)
            for k, v in {"Host": "nova-hub.tail1.ts.net", "Content-Type": "application/json",
                         OWNER_HEADER: "manav@example.com"}.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())

    import nova.link

    monkeypatch.setattr(nova.link, "POLL_WAIT_S", 1.0)
    cfg = dataclasses.replace(Config(), hub=dataclasses.replace(HubConfig(), host="nova-hub.tail1.ts.net"))
    link = Link(cfg, egress=_Egress(), audit=AuditLog(tmp_path / "link.log"))
    stop = asyncio.Event()
    serving = asyncio.create_task(link.serve(stop))
    try:
        for _ in range(50):
            if devices.online("laptop"):
                break
            await asyncio.sleep(0.05)
        assert devices.online("laptop") and "time" in devices.skills("laptop")
        brain = _Brain([("date", {}), ("get_volume", {})])
        a = _assistant(tmp_path, devices, brain)
        # "get_volume" is a laptop skill: it must travel to the Link and back
        await a.turn("tell me the date and then the volume", _Channel())  # not instant: goes to the brain
        text, _ok = brain.results[1]
        assert text and text != NOT_HERE
        assert "link.run" in (tmp_path / "link.log").read_text()
    finally:
        stop.set()
        serving.cancel()
        server.shutdown()
        server.server_close()
