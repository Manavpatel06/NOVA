"""nova-hub (docs/decisions/0019): the loopback egress door, the local model
wrapper, the hub service and its one listener, and the laptop's hub lane."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

import nova.app as app_mod
import nova.skills  # noqa: F401 -- registers the skills
from nova.brain.hub_tier import HubDecision, HubTier, catalogue, check, offered
from nova.config import Config, ConfigError, HubConfig, UiConfig, WakeConfig
from nova.hub.listener import OWNER_HEADER, make_server
from nova.hub.local_model import LocalModel, ROUTE_SCHEMA, catalogue_text, parse_decision
from nova.hub.service import HubError, HubService
from nova.security.audit import AuditLog
from nova.security.egress import Egress, EgressError
from nova.security.tiers import ArmedBy, Tier
from nova.skills.registry import all_skills


# --- a tiny local HTTP service standing in for Ollama -------------------------

class _Fake(BaseHTTPRequestHandler):
    replies: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self):
        body = None
        if self.command == "POST":
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).seen.append((self.command, self.path, body))
        status, payload = type(self).replies.get(self.path, (404, {}))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _reply


@pytest.fixture
def fake_service():
    _Fake.replies, _Fake.seen = {}, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    port = server.server_address[1]
    if port < 1024:  # pragma: no cover
        pytest.skip("ephemeral port below 1024")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port
    server.shutdown()
    server.server_close()


def _egress(tmp_path, ports=()):
    return Egress([], audit=AuditLog(path=tmp_path / "audit.log"), loopback_ports=ports)


# --- Egress.local_json --------------------------------------------------------

def test_local_json_refuses_ports_not_named_at_construction(tmp_path, fake_service):
    eg = _egress(tmp_path, ports=[fake_service + 1 if fake_service < 65535 else 1024])
    with pytest.raises(EgressError):
        eg.local_json(fake_service, "/api/tags")
    assert "egress.loopback" in (tmp_path / "audit.log").read_text()


@pytest.mark.parametrize("path", ["api/tags", "/../etc", "/a?b=1", "//evil.com/x", "/a b", "/a@b"])
def test_local_json_refuses_odd_paths(tmp_path, fake_service, path):
    with pytest.raises(EgressError):
        _egress(tmp_path, [fake_service]).local_json(fake_service, path)
    assert _Fake.seen == []


@pytest.mark.parametrize("bad", [80, 0, 70000, True, "11434"])
def test_loopback_ports_are_validated(tmp_path, bad):
    with pytest.raises(ConfigError):
        _egress(tmp_path, [bad])


def test_local_json_gets_and_posts_to_loopback_only(tmp_path, fake_service):
    _Fake.replies = {"/api/tags": (200, {"models": []}), "/api/chat": (200, {"ok": 1})}
    eg = _egress(tmp_path, [fake_service])
    assert eg.local_json(fake_service, "/api/tags") == {"models": []}
    assert eg.local_json(fake_service, "/api/chat", {"x": "secret words"}) == {"ok": 1}
    assert [s[0] for s in _Fake.seen] == ["GET", "POST"]
    log = (tmp_path / "audit.log").read_text()
    assert "secret words" not in log  # sizes, never bodies


def test_local_json_http_error_is_an_egress_error(tmp_path, fake_service):
    _Fake.replies = {"/api/chat": (500, {})}
    with pytest.raises(EgressError):
        _egress(tmp_path, [fake_service]).local_json(fake_service, "/api/chat", {})


def test_post_json_allows_put_but_nothing_else(tmp_path):
    eg = Egress(["nova-hub.example.ts.net"], audit=AuditLog(path=tmp_path / "a.log"))
    with pytest.raises(EgressError):
        eg.post_json("https://nova-hub.example.ts.net/v1/x", {}, headers={}, method="DELETE")


# --- local model ----------------------------------------------------------------

class _ModelEgress:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def local_json(self, port, path, body=None, *, timeout=30.0):
        self.calls.append((port, path, body))
        if self.exc:
            raise self.exc
        return self.reply


def test_route_asks_ollama_for_schema_json_with_the_tools_in_a_fixed_prefix():
    eg = _ModelEgress({"message": {"content": json.dumps({"route": "tool", "tool": "mute", "params": {}, "say": ""})}})
    m = LocalModel(eg, model="qwen3.5:2b", port=11434)
    tools = catalogue(all_skills())
    d = m.route("mute", tools)
    assert d["route"] == "tool" and d["tool"] == "mute" and "ms" in d
    port, path, body = eg.calls[0]
    assert (port, path) == (11434, "/api/chat")
    assert body["format"] == ROUTE_SCHEMA and body["think"] is False and body["keep_alive"] == -1
    assert body["options"]["temperature"] == 0
    system, user = body["messages"]
    assert "- mute(" in system["content"] and user == {"role": "user", "content": "mute"}
    # the same tool list gives a byte-identical prefix (Ollama's cache)
    m.route("unmute", tools)
    assert eg.calls[1][2]["messages"][0] == system


@pytest.mark.parametrize("content", ["not json", "[]", '{"route": "run_shell"}', "", None])
def test_anything_malformed_escalates(content):
    assert parse_decision(content)["route"] == "escalate"


def test_a_dead_model_escalates_instead_of_crashing():
    m = LocalModel(_ModelEgress(exc=EgressError("down")), model="m", port=11434)
    assert m.route("mute", [])["route"] == "escalate"


def test_catalogue_text_bounds_what_reaches_the_prompt():
    tools = [{"name": f"t{i}", "description": "d" * 500, "params": ["a"], "required": ["a"]} for i in range(400)]
    text = catalogue_text(tools)
    assert text.count("\n") == 149 and "d" * 201 not in text


# --- hub service ------------------------------------------------------------------

class _Model:
    model = "qwen3.5:2b"

    def __init__(self, decision=None):
        self.decision = decision or {"route": "escalate", "tool": "", "params": {}, "say": ""}
        self.seen = []

    def installed(self):
        return ["qwen3.5:2b"]

    def loaded(self):
        return ["qwen3.5:2b"]

    def route(self, text, tools):
        self.seen.append(text)
        return dict(self.decision, ms=5)


def _service(tmp_path, owner="manav@example.com", model=None):
    if owner:
        (tmp_path / "hub_owner").write_text(owner + "\n")
    return HubService(model=model or _Model(), data_dir=tmp_path)


def test_only_the_recorded_owner_counts_and_no_owner_means_nobody(tmp_path):
    svc = _service(tmp_path)
    assert svc.is_owner("Manav@Example.com") and not svc.is_owner("someone@else.com") and not svc.is_owner(None)
    assert not _service(tmp_path / "x", owner=None).is_owner("manav@example.com")


def test_health_reports_the_model(tmp_path):
    h = _service(tmp_path).health()
    assert h["ok"] and h["model_installed"] and h["model_loaded"] and h["owner_set"]


@pytest.mark.parametrize("body", [None, [], {"text": "", "tools": []}, {"text": "x" * 301, "tools": []},
                                  {"text": "mute", "tools": "all"}, {"text": "mute", "tools": [{}] * 151}])
def test_route_rejects_bad_requests(tmp_path, body):
    with pytest.raises(HubError):
        _service(tmp_path).route(body)


@pytest.mark.parametrize("rel", ["../x.md", "/etc/passwd.md", "a/b/c.md", "x.sh", "journal/../../x.md",
                                 ".hidden.md", "a\\b.md", "journal/.md"])
def test_memory_paths_that_could_leave_the_mirror_are_refused(tmp_path, rel):
    with pytest.raises(HubError):
        _service(tmp_path).put_memory({"files": {rel: "x"}})


def test_memory_mirror_writes_and_counts(tmp_path):
    svc = _service(tmp_path)
    out = svc.put_memory({"files": {"facts.json": "[]", "journal/2026-11-22.md": "- hi"}})
    assert out == {"ok": True, "written": 2, "removed": 0, "files": 2}
    assert (tmp_path / "memory-mirror" / "journal" / "2026-11-22.md").read_text() == "- hi"
    assert svc.health()["memory_files"] == 2


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin on Windows")
def test_memory_mirror_refuses_a_planted_symlink_out(tmp_path):
    svc = _service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "memory-mirror").mkdir()
    (tmp_path / "memory-mirror" / "journal").symlink_to(outside)
    with pytest.raises(HubError):
        svc.put_memory({"files": {"journal/x.md": "pwned"}})
    assert list(outside.iterdir()) == []


def test_memory_upload_size_is_capped(tmp_path):
    with pytest.raises(HubError):
        _service(tmp_path).put_memory({"files": {"a.md": "x" * 5_000_001}})


# --- the listener ----------------------------------------------------------------------

@pytest.fixture
def hub(tmp_path):
    model = _Model({"route": "tool", "tool": "mute", "params": {}, "say": ""})
    server = make_server(_service(tmp_path, model=model), 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server.server_address, model
    server.shutdown()
    server.server_close()


def _call(addr, method, path, body=None, login=None, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if login:
        req.add_header(OWNER_HEADER, login)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_listener_is_on_loopback(hub):
    assert hub[0][0] == "127.0.0.1"


def test_health_is_open_route_and_memory_are_owner_only(hub):
    addr, model = hub
    assert _call(addr, "GET", "/v1/health")[0] == 200
    assert _call(addr, "POST", "/v1/route", {"text": "mute", "tools": []})[0] == 403
    assert _call(addr, "POST", "/v1/route", {"text": "mute", "tools": []}, login="evil@x.com")[0] == 403
    assert _call(addr, "PUT", "/v1/memory", {"files": {"a.md": "x"}}, login="evil@x.com")[0] == 403
    assert model.seen == []  # refused requests never reach the model
    status, body = _call(addr, "POST", "/v1/route", {"text": "mute", "tools": []}, login="manav@example.com")
    assert status == 200 and body["tool"] == "mute"


def test_bad_requests_get_400_and_unknown_paths_404(hub):
    addr, _ = hub
    assert _call(addr, "POST", "/v1/route", b"{nope", login="manav@example.com")[0] == 400
    assert _call(addr, "POST", "/v1/route", {"text": ""}, login="manav@example.com")[0] == 400
    assert _call(addr, "GET", "/v1/route")[0] == 404
    assert _call(addr, "POST", "/v1/shell", {"x": 1}, login="manav@example.com")[0] == 404


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Content-Type": "text/plain"},
])
def test_requests_from_other_websites_are_refused_even_for_the_owner(hub, headers):
    # Tailscale names the DEVICE, so a web page open on Manav's phone would pass
    # the owner check; the browser's own markings are what give it away
    addr, model = hub
    status, _ = _call(addr, "POST", "/v1/route", {"text": "mute", "tools": []}, login="manav@example.com",
                      headers=headers)
    assert status in (400, 403)
    assert model.seen == []


def test_same_origin_and_non_browser_requests_still_work(hub, tmp_path):
    addr, _ = hub
    ok = {"text": "mute", "tools": []}
    assert _call(addr, "POST", "/v1/route", ok, login="manav@example.com")[0] == 200
    assert _call(addr, "POST", "/v1/route", ok, login="manav@example.com",
                 headers={"Sec-Fetch-Site": "same-origin"})[0] == 200


# --- laptop side: what the hub may propose ---------------------------------------------------

def _skills():
    return all_skills()


def test_only_read_and_act_skills_without_approvals_are_offered():
    names = {t["name"] for t in catalogue(_skills())}
    for spec in _skills().values():
        if spec.tier is Tier.CONFIRM or getattr(spec, "confirm_phrase", None) or getattr(spec, "untrusted_output", False):
            assert spec.name not in names
    assert {"mute", "volume_up", "time"} <= names


def _d(**kw):
    return dict({"route": "tool", "tool": "", "params": {}, "say": "", "ms": 3}, **kw)


def test_a_clean_proposal_passes():
    assert check(_d(tool="mute"), "mute please", _skills()) == HubDecision(kind="tool", tool="mute", params={}, ms=3)


def test_numbers_are_allowed_without_appearing_verbatim():
    d = check(_d(tool="set_volume", params={"level": "40"}), "volume to forty", _skills())
    assert d is not None and d.params == {"level": "40"}


def test_values_manav_never_said_are_refused():
    spec = next(s for s in _skills().values() if offered(s) and any(
        p != "ctx" for p in __import__("inspect").signature(s.handler).parameters))
    param = next(p for p in __import__("inspect").signature(spec.handler).parameters if p != "ctx")
    assert check(_d(tool=spec.name, params={param: "definitely not said"}), "do the thing", _skills()) is None


@pytest.mark.parametrize("decision", [
    _d(tool="no_such_tool"),
    _d(tool="mute", params={"extra": "x"}),
    _d(tool="set_volume", params={}),                       # required param missing
    _d(tool="set_volume", params={"level": ""}),
    _d(route="escalate"),
    _d(route="run"),
    "tool",
    None,
])
def test_anything_off_goes_to_claude(decision):
    assert check(decision, "set volume to 40", _skills()) is None


def test_confirm_tier_skills_are_never_taken_even_if_proposed():
    confirm = next(s.name for s in _skills().values() if s.tier is Tier.CONFIRM)
    assert check(_d(tool=confirm), "do it", _skills()) is None


def test_small_talk_only_for_short_sentences_and_short_replies():
    assert check(_d(route="chat", say="Hey Manav!"), "hey nova", _skills()).say == "Hey Manav!"
    assert check(_d(route="chat", say="Sure."), " ".join(["word"] * 13), _skills()) is None
    assert check(_d(route="chat", say="x" * 201), "hi", _skills()) is None
    assert check(_d(route="chat", say="  "), "hi", _skills()) is None


# --- laptop side: talking to the hub -------------------------------------------------------------

class _PostEgress:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def post_json(self, url, body, *, headers, timeout=20.0, method="POST"):
        self.calls.append((url, body, method))
        if self.exc:
            raise self.exc
        return self.reply


def _cfg(host="nova-hub.tail1.ts.net", **hub):
    return dataclasses.replace(Config(), hub=dataclasses.replace(HubConfig(), host=host, **hub))


async def test_no_host_means_no_hub_calls():
    eg = _PostEgress(_d(tool="mute"))
    tier = HubTier(_cfg(host=""), eg)
    assert not tier.enabled() and await tier.decide("mute") is None and eg.calls == []


async def test_local_tier_can_be_switched_off():
    assert not HubTier(_cfg(local_tier=False), _PostEgress()).enabled()


async def test_decide_sends_the_sentence_and_the_offered_tools():
    eg = _PostEgress(_d(tool="mute"))
    d = await HubTier(_cfg(), eg).decide("mute")
    url, body, method = eg.calls[0]
    assert url == "https://nova-hub.tail1.ts.net/v1/route" and method == "POST"
    assert body["text"] == "mute" and {t["name"] for t in body["tools"]} == {t["name"] for t in catalogue(_skills())}
    assert d.tool == "mute"


async def test_a_dead_hub_is_skipped_for_a_minute():
    now = [100.0]
    eg = _PostEgress(exc=EgressError("unreachable"))
    tier = HubTier(_cfg(), eg, clock=lambda: now[0])
    assert await tier.decide("mute") is None
    assert not tier.enabled() and await tier.decide("mute") is None and len(eg.calls) == 1
    now[0] += 61
    assert tier.enabled()


async def test_push_memory_puts_the_memory_folder(tmp_path):
    (tmp_path / "journal").mkdir()
    (tmp_path / "facts.json").write_text("[]")
    (tmp_path / "journal" / "2026-11-22.md").write_text("- hi")
    (tmp_path / "skip.bin").write_bytes(b"\0")
    eg = _PostEgress({"ok": True})
    assert await HubTier(_cfg(), eg).push_memory(tmp_path)
    url, body, method = eg.calls[0]
    assert url.endswith("/v1/memory") and method == "PUT"
    assert body["files"] == {"facts.json": "[]", "journal/2026-11-22.md": "- hi"}


# --- in the app ----------------------------------------------------------------------------------

class _Synth:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)
        return np.zeros(4, dtype=np.float32), 16000


class _Player:
    async def play(self, pcm, sr, *, on_first_frame=None):
        if on_first_frame:
            on_first_frame()


class _Hub:
    def __init__(self, decision):
        self.decision, self.asked = decision, []

    def enabled(self):
        return True

    async def decide(self, text):
        self.asked.append(text)
        return self.decision


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AuditLog", lambda **kw: AuditLog(path=tmp_path / "audit.log"))
    cfg = dataclasses.replace(_cfg(), wake=dataclasses.replace(WakeConfig(), mode="prefix"),
                              ui=dataclasses.replace(UiConfig(), window=False))
    a = app_mod.NovaApp(config=cfg)
    a.synth = _Synth()
    a.player = _Player()
    a.agent._cli_available = lambda: True
    yield a
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None


def test_the_hub_joins_the_allowlist_only_when_named(app):
    assert "nova-hub.tail1.ts.net" in app.egress.allowed_hosts and isinstance(app.hub, HubTier)


async def test_hub_lane_runs_a_skill_without_asking_claude_and_tells_claude_after(app, monkeypatch, tmp_path):
    import nova.system.winops as winops

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))
    asked_claude = []

    async def _respond(text, invoke_tool):
        asked_claude.append(text)

    monkeypatch.setattr(app.agent, "respond", _respond)
    app.hub = _Hub(HubDecision(kind="tool", tool="media_play_pause", params={}))
    assert app._instant_match("could you stop the tunes for a sec") is None
    reply = await app.run_text("could you stop the tunes for a sec", source="voice", armed_by=ArmedBy.VOICE)
    assert pressed and asked_claude == [] and reply is not None
    assert app.hub.asked == ["could you stop the tunes for a sec"]
    assert "could you stop the tunes" in (app.agent._carry or "")      # Claude hears about it next turn
    assert "media_play_pause" in (tmp_path / "audit.log").read_text()  # through the ladder, audited


async def test_instant_lane_still_comes_first(app):
    app.hub = _Hub(HubDecision(kind="chat", say="nope"))
    reply = await app.run_text("what time is it", source="voice", armed_by=ArmedBy.VOICE)
    assert app.hub.asked == [] and reply.text.startswith("It's")


async def test_small_talk_is_answered_by_the_hub(app):
    app.hub = _Hub(HubDecision(kind="chat", say="Doing great, Manav."))
    reply = await app.run_text("hey nova how are you", source="voice", armed_by=ArmedBy.VOICE)
    assert reply.text == "Doing great, Manav." and app.synth.said[-1] == "Doing great, Manav."


async def test_when_the_hub_isnt_sure_claude_gets_the_sentence(app, monkeypatch):
    from nova.brain.agent import AgentResult

    seen = []

    async def _respond(text, invoke_tool):
        seen.append(text)
        return AgentResult(text="Claude here.", actions=[], used_web=False)

    monkeypatch.setattr(app.agent, "respond", _respond)
    app.hub = _Hub(None)
    reply = await app.run_text("write me a poem about Tempe", source="voice", armed_by=ArmedBy.VOICE)
    assert app.hub.asked == ["write me a poem about Tempe"] and seen and reply.text == "Claude here."


# --- review fixes (2026-11-22) -------------------------------------------------------------------

def test_no_host_means_no_hub_in_the_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AuditLog", lambda **kw: AuditLog(path=tmp_path / "audit.log"))
    cfg = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), mode="prefix"),
                              ui=dataclasses.replace(UiConfig(), window=False))
    a = app_mod.NovaApp(config=cfg)
    try:
        assert a.hub is None and not any("ts.net" in h for h in a.egress.allowed_hosts)
    finally:
        import nova.security.untrusted as untrusted_mod

        untrusted_mod._AUDIT = None


async def test_decide_redacts_the_sentence_before_it_leaves():
    cfg = dataclasses.replace(_cfg(), security=dataclasses.replace(Config().security, redact_terms=["hunter2"]))
    eg = _PostEgress(_d(route="escalate"))
    await HubTier(cfg, eg).decide("mute hunter2")
    assert "hunter2" not in eg.calls[0][1]["text"]


def test_wifi_is_never_offered_to_the_hub():
    assert "set_radio" not in {t["name"] for t in catalogue(_skills())}
    assert check(_d(tool="set_radio", params={"radio": "wifi", "state": "off"}), "turn wifi off", _skills()) is None


@pytest.mark.parametrize("text", ["don't mute", "never mute", "mute if it gets loud", "mute when the call starts",
                                  "should I mute", "mute it later", "do not mute"])
def test_negations_and_conditions_go_to_claude(text):
    assert check(_d(tool="mute"), text, _skills()) is None


@pytest.mark.parametrize("text,level,ok", [
    ("set the volume to 40", "40", True),
    ("volume to forty", "40", True),
    ("set the volume to 40", "60", False),     # a different number than he said
    ("set the volume", "100", False),          # no number said at all
])
def test_numbers_must_come_from_what_he_said(text, level, ok):
    assert (check(_d(tool="set_volume", params={"level": level}), text, _skills()) is not None) is ok


@pytest.mark.parametrize("text,say,ok", [
    ("hey nova", "Hey Manav!", True),
    ("thanks nova", "Anytime.", True),
    ("how are you", "Doing well, thanks for asking.", True),
    ("when is my next meeting", "Your next meeting is at 3pm.", False),   # not small talk
    ("who won the game", "The Suns.", False),
    ("hey nova", "It's 3 o'clock.", False),                               # numbers = made-up facts
])
def test_small_talk_is_decided_on_the_laptop(text, say, ok):
    assert (check(_d(route="chat", say=say), text, _skills()) is not None) is ok


def test_a_complete_upload_forgets_what_the_laptop_forgot(tmp_path):
    svc = _service(tmp_path)
    svc.put_memory({"files": {"facts.json": "[1]", "journal/2026-11-21.md": "old", "from_chat.md": "x"}})
    out = svc.put_memory({"files": {"facts.json": "[]", "journal/2026-11-22.md": "new"}, "complete": True})
    mirror = tmp_path / "memory-mirror"
    assert out["removed"] == 2 and out["files"] == 2
    assert sorted(p.relative_to(mirror).as_posix() for p in mirror.rglob("*") if p.is_file()) == [
        "facts.json", "journal/2026-11-22.md"]


def _call_host(addr, method, path, body, host, login):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}", data=data, method=method)
    req.add_header("Host", host)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if login:
        req.add_header(OWNER_HEADER, login)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_requests_for_some_other_name_are_refused_even_with_the_owner_header(hub, tmp_path):
    """DNS rebinding: a page open on the hub reaches 127.0.0.1 as evil.com."""
    addr, model = hub
    status, _ = _call_host(addr, "POST", "/v1/route", {"text": "mute", "tools": []}, "evil.com:8765", "manav@example.com")
    assert status == 403 and model.seen == []
    (tmp_path / "hub_name").write_text("nova-hub.tail1.ts.net\n")
    status, body = _call_host(addr, "POST", "/v1/route", {"text": "mute", "tools": []}, "nova-hub.tail1.ts.net",
                              "manav@example.com")
    assert status == 200 and body["tool"] == "mute"


def test_the_hubs_own_pages_may_call_it(hub, tmp_path):
    addr, _ = hub
    (tmp_path / "hub_name").write_text("nova-hub.tail1.ts.net\n")
    req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}/v1/route",
                                 data=json.dumps({"text": "mute", "tools": []}).encode(), method="POST")
    for k, v in {"Host": "nova-hub.tail1.ts.net", "Content-Type": "application/json",
                 "Origin": "https://nova-hub.tail1.ts.net", "Sec-Fetch-Site": "same-origin",
                 OWNER_HEADER: "manav@example.com"}.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200


def test_other_tailnet_devices_get_only_ok_from_health(hub, tmp_path):
    addr, _ = hub
    (tmp_path / "hub_name").write_text("nova-hub.tail1.ts.net\n")
    _, other = _call_host(addr, "GET", "/v1/health", None, "nova-hub.tail1.ts.net", "friend@example.com")
    _, mine = _call_host(addr, "GET", "/v1/health", None, "nova-hub.tail1.ts.net", "manav@example.com")
    _, local = _call(addr, "GET", "/v1/health")
    assert other == {"ok": True} and "model_loaded" in mine and "model_loaded" in local
