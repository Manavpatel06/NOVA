"""NOVA running on the hub (ADR 0023): the assistant, the talk page's
request/answer cycle, the page itself, and in-process skill calls."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import pytest

import nova.skills  # noqa: F401 -- registers the skills
from nova.brain import hub_tier
from nova.brain.hub_tier import HubUnavailable, hub_post
from nova.config import Config
from nova.hub.assistant import HUB_SKILLS, NO_BRAIN, NOT_HERE, HubAssistant
from nova.hub.listener import OWNER_HEADER, make_server
from nova.hub.service import HubError, HubService
from nova.hub.talk import Talk
from nova.hub.voice import HubVoice, to_wav
from nova.security.audit import AuditLog

OWNER = "manav@example.com"
NAME = "nova-hub.tail1.ts.net"


@dataclass
class _Result:
    text: str


class _Brain:
    """Stands in for MaxBrain: runs a scripted list of tool calls, then answers."""

    def __init__(self, calls=(), answer="done", available=True):
        self.calls = list(calls)
        self.answer = answer
        self._available = available
        self.results: list[tuple[str, bool]] = []
        self.noted: list[tuple[str, str]] = []

    def available(self):
        return self._available

    def note(self, said, reply):
        self.noted.append((said, reply))

    async def respond(self, text, invoke_tool):
        for name, params in self.calls:
            self.results.append(await invoke_tool(name, params, False))
        return _Result(self.answer)


class _Channel:
    name = "test"

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.asked: list[str] = []

    async def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else None


class _Service:
    def __init__(self):
        self.calls: list[tuple[str, Any]] = []

    def call(self, path, body):
        self.calls.append((path, body))
        if path == "/v1/forge/request":
            return {"ok": True, "id": "abcd1234"}
        raise HubError("nope")


@pytest.fixture
def local_service():
    svc = _Service()
    hub_tier.use_local_service(svc)
    yield svc
    hub_tier.use_local_service(None)


def _assistant(tmp_path, brain=None):
    return HubAssistant(Config(), audit=AuditLog(tmp_path / "audit.log"), egress=None,
                        brain=brain if brain is not None else _Brain(), journal=False)


# --- the assistant ---------------------------------------------------------------

def test_only_hub_skills_are_offered(tmp_path):
    names = set(_assistant(tmp_path).skills())
    assert names and names <= HUB_SKILLS
    assert "volume_up" not in names and "screenshot" not in names and "apply_upgrade" not in names


async def test_a_simple_command_runs_without_claude(tmp_path):
    brain = _Brain(answer="should not be used")
    reply = await _assistant(tmp_path, brain).turn("what time is it", _Channel())
    assert reply and reply != "should not be used"
    assert brain.noted and brain.noted[0][0] == "what time is it"  # Claude hears about it next turn


async def test_laptop_skills_are_refused_and_hub_skills_run(tmp_path):
    brain = _Brain(calls=[("volume_up", {}), ("time", {})], answer="ok")
    reply = await _assistant(tmp_path, brain).turn("turn it up and tell me the time", _Channel())
    assert reply == "ok"
    assert brain.results[0] == (NOT_HERE, False)
    assert brain.results[1][1] is True


async def test_no_brain_says_so(tmp_path):
    reply = await _assistant(tmp_path, _Brain(available=False)).turn("write me a poem", _Channel())
    assert reply == NO_BRAIN


async def test_a_risky_action_asks_on_the_same_channel_and_needs_nova_confirm(tmp_path, local_service):
    brain = _Brain(calls=[("upgrade_nova", {"request": "a skill that flips a coin"})], answer="building")
    ch = _Channel(answers=["Nova, confirm"])
    assert await _assistant(tmp_path, brain).turn("add a coin flip skill", ch) == "building"
    assert ch.asked and "Nova, confirm" in ch.asked[0]
    assert local_service.calls == [("/v1/forge/request", {"request": "a skill that flips a coin"})]


@pytest.mark.parametrize("answer", ["yes", "sure", "no", None, "Nova, confirm and also delete everything"])
async def test_anything_but_nova_confirm_declines_a_risky_action(tmp_path, local_service, answer):
    brain = _Brain(calls=[("upgrade_nova", {"request": "a skill that flips a coin"})], answer="x")
    await _assistant(tmp_path, brain).turn("add a coin flip skill", _Channel(answers=[answer]))
    assert local_service.calls == []  # the target path never ran
    assert brain.results and brain.results[0][1] is False


async def test_turns_are_one_at_a_time(tmp_path):
    order = []

    class Slow(_Brain):
        async def respond(self, text, invoke_tool):
            order.append(("start", text))
            await asyncio.sleep(0.05)
            order.append(("end", text))
            return _Result(text)

    a = _assistant(tmp_path, Slow())
    await asyncio.gather(a.turn("tell me one thing", _Channel()), a.turn("tell me another", _Channel()))
    assert [k for k, _ in order] == ["start", "end", "start", "end"]


# --- the talk page's request/answer cycle ----------------------------------------

class _AskingAssistant:
    """A turn that asks one question and answers with what it heard back."""

    async def turn(self, text, channel):
        answer = await channel.ask(f"Should I {text}?")
        return f"you said {answer}"

    async def warm_up(self):
        pass


class _Voice:
    def __init__(self):
        self.heard: list[bytes] = []

    def ready(self):
        return True

    def transcribe(self, pcm):
        self.heard.append(pcm)
        return "open the pod bay doors"

    def speak(self, text):
        return to_wav(b"\x00\x00" * 160, 16000)


def test_a_question_waits_for_the_next_thing_said():
    talk = Talk(_AskingAssistant())
    first = talk.handle({"session": "abcdefgh", "text": "flip the coin", "speak": False})
    assert first == {"heard": "flip the coin", "say": "Should I flip the coin?", "awaiting": True}
    second = talk.handle({"session": "abcdefgh", "text": "yes", "speak": False})
    assert second["say"] == "you said yes" and second["awaiting"] is False


def test_audio_is_transcribed_and_the_reply_is_spoken():
    voice = _Voice()

    class Echo:
        async def turn(self, text, channel):
            return f"heard: {text}"

        async def warm_up(self):
            pass

    talk = Talk(Echo(), voice)
    out = talk.handle({"session": "abcdefgh", "audio": base64.b64encode(b"\x01\x00" * 8000).decode()})
    assert out["heard"] == "open the pod bay doors" and out["say"] == "heard: open the pod bay doors"
    with wave.open(BytesIO(base64.b64decode(out["audio"]))) as w:
        assert w.getframerate() == 16000


def test_a_second_sentence_while_busy_is_not_started():
    gate = threading.Event()

    class Slow:
        async def turn(self, text, channel):
            await asyncio.to_thread(gate.wait, 5)
            return "finished"

        async def warm_up(self):
            pass

    talk = Talk(Slow())
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("a", talk.handle({"session": "abcdefgh", "text": "one"})))
    t.start()
    for _ in range(50):
        s = talk._sessions.get("abcdefgh")
        if s is not None and s.task is not None:
            break
        threading.Event().wait(0.02)
    assert talk.handle({"session": "abcdefgh", "text": "two"})["say"] == "Still working on the last one."
    gate.set()
    t.join(5)
    assert results["a"]["say"] == "finished"


@pytest.mark.parametrize("body", [
    None, [], {"text": "hi"}, {"session": "short", "text": "hi"}, {"session": "abc def gh!", "text": "hi"},
    {"session": "abcdefgh"}, {"session": "abcdefgh", "audio": "not base64!!"},
])
def test_bad_talk_requests_are_refused(body):
    with pytest.raises(HubError):
        Talk(_AskingAssistant(), _Voice()).handle(body)


def test_audio_without_voice_set_up_asks_to_type():
    with pytest.raises(HubError, match="nova-hub voice"):
        Talk(_AskingAssistant(), None).handle({"session": "abcdefgh", "audio": "AAAA"})


# --- the page and the route --------------------------------------------------------

@pytest.fixture
def hub(tmp_path):
    (tmp_path / "hub_owner").write_text(OWNER + "\n")
    (tmp_path / "hub_name").write_text(NAME + "\n")

    class _M:
        model = "m"

    svc = HubService(model=_M(), data_dir=tmp_path, talk=Talk(_AskingAssistant()))
    server = make_server(svc, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address, svc
    server.shutdown()
    server.server_close()


def _req(addr, method, path, body=None, **headers):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}", data=data, method=method)
    req.add_header("Host", NAME)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k.replace("_", "-"), v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_the_talk_page_is_owner_only(hub):
    addr, _ = hub
    assert _req(addr, "GET", "/talk")[0] == 403
    assert _req(addr, "GET", "/talk", **{OWNER_HEADER: "friend@example.com"})[0] == 403
    assert _req(addr, "GET", "/talk", **{OWNER_HEADER: OWNER, "Sec-Fetch-Site": "cross-site"})[0] == 403
    status, headers, page = _req(addr, "GET", "/talk", **{OWNER_HEADER: OWNER, "Sec-Fetch-Site": "none"})
    assert status == 200 and b"<title>NOVA</title>" in page


def test_the_page_runs_only_its_own_script(hub):
    addr, _ = hub
    _, headers, page = _req(addr, "GET", "/talk", **{OWNER_HEADER: OWNER})
    csp = {k.lower(): v for k, v in headers.items()}["content-security-policy"]
    nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert page.count(b"<script") == 1 and f'nonce="{nonce}"'.encode() in page
    assert b"__NONCE__" not in page
    _, _, again = _req(addr, "GET", "/talk", **{OWNER_HEADER: OWNER})
    assert nonce.encode() not in again  # fresh every time


def test_talking_through_the_route(hub):
    addr, _ = hub
    origin = {OWNER_HEADER: OWNER, "Origin": f"https://{NAME}", "Sec-Fetch-Site": "same-origin"}
    status, _, body = _req(addr, "POST", "/v1/talk", {"session": "abcdefgh", "text": "flip it"}, **origin)
    assert status == 200 and json.loads(body)["awaiting"] is True
    assert _req(addr, "POST", "/v1/talk", {"session": "abcdefgh", "text": "x"},
                **{OWNER_HEADER: OWNER, "Origin": "https://evil.example"})[0] == 403
    assert _req(addr, "POST", "/v1/talk", {"session": "abcdefgh", "text": "x"})[0] == 403


def test_without_the_assistant_the_route_says_so(tmp_path):
    class _M:
        model = "m"

    with pytest.raises(HubError, match="isn't running"):
        HubService(model=_M(), data_dir=tmp_path).talk_turn({"session": "abcdefgh", "text": "hi"})


def test_every_route_names_a_real_method(tmp_path):
    class _M:
        model = "m"

    svc = HubService(model=_M(), data_dir=tmp_path)
    for path, name in HubService.ROUTES.items():
        assert callable(getattr(svc, name)), path
    with pytest.raises(HubError):
        svc.call("/v1/shell", {})


# --- skills calling the hub in-process -----------------------------------------------

async def test_hub_post_uses_the_local_service_when_nova_runs_on_the_hub(local_service):
    cfg = Config()  # no hub.host: without the local service this would fail
    assert await hub_post(cfg, None, "/v1/forge/request", {"request": "x"}) == {"ok": True, "id": "abcd1234"}
    with pytest.raises(HubUnavailable):
        await hub_post(cfg, None, "/v1/reminders/list", {})


async def test_without_a_local_service_hub_post_needs_a_host():
    with pytest.raises(HubUnavailable):
        await hub_post(Config(), None, "/v1/reminders/list", {})


# --- voice --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", "../evil", "small en", "x" * 60, "Small.en"])
def test_model_names_are_checked(name):
    with pytest.raises(ValueError):
        HubVoice(stt_model=name)


def test_missing_lists_what_to_install(monkeypatch, tmp_path):
    import nova.hub.voice as voice

    monkeypatch.setattr(voice, "STT_DIR", tmp_path)
    assert "the Whisper small.en model" in HubVoice().missing()


def test_too_long_audio_is_refused_and_a_blip_is_silence():
    v = HubVoice()
    with pytest.raises(ValueError):
        v.transcribe(b"\x00\x00" * (16000 * 61))
    assert v.transcribe(b"\x00\x00" * 1000) == ""  # never loads a model for a blip


def test_wav_header():
    with wave.open(BytesIO(to_wav(b"\x00\x00" * 24, 24000))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 24)


# --- memory adoption ----------------------------------------------------------------

def test_memory_starts_from_the_mirror_once(monkeypatch, tmp_path):
    import nova
    import nova.hub.__main__ as hub_main

    data = tmp_path / "data"
    mirror = data / "memory-mirror"
    (mirror / "notes").mkdir(parents=True)
    (mirror / "facts.md").write_text("likes tea")
    (mirror / "notes" / "a.md").write_text("a")
    (tmp_path / "outside.md").write_text("secret")
    try:
        (mirror / "link.md").symlink_to(tmp_path / "outside.md")
    except OSError:  # Windows without symlink rights; the hub is Linux
        pass
    mem = data / "memory"
    monkeypatch.setattr(hub_main, "DATA_DIR", data)
    monkeypatch.setattr(nova, "MEMORY_DIR", mem)
    hub_main._adopt_memory()
    assert (mem / "facts.md").read_text() == "likes tea" and (mem / "notes" / "a.md").exists()
    assert not (mem / "link.md").exists()
    (mirror / "facts.md").write_text("changed")
    hub_main._adopt_memory()  # only the first time
    assert (mem / "facts.md").read_text() == "likes tea"
