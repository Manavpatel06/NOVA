"""NOVA's brain on the hub (ADR 0021): the hub's BrainHost, the laptop's
RemoteSession/AutoSession, and the app choosing them."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import urllib.error
import urllib.request

import pytest

import nova.app as app_mod
from nova.brain.claude_arm import ClaudeSessionError, SessionReply
from nova.brain.hub_tier import HubUnavailable
from nova.brain.remote_session import AutoSession, RemoteSession
from nova.config import BrainConfig, Config, HubConfig, UiConfig, WakeConfig
from nova.hub.brain_host import BrainHost
from nova.hub.listener import OWNER_HEADER, make_server
from nova.hub.service import HubError, HubService
from nova.security.audit import AuditLog
from nova.security.egress import EgressError


class FakeClaude:
    made: list["FakeClaude"] = []

    def __init__(self, prompt, web, model, fail=False):
        self.prompt, self.web, self.model, self.fail = prompt, web, model, fail
        self.sent, self.closed, self.started = [], False, False
        FakeClaude.made.append(self)

    async def start(self):
        self.started = True

    async def send(self, text, *, timeout_s):
        if self.fail:
            raise ClaudeSessionError("broke")
        self.sent.append(text)
        return SessionReply(text=json.dumps({"say": f"echo {text}", "actions": [], "final": True}),
                            used_tools=["WebSearch"] if "search" in text else [])

    async def close(self):
        self.closed = True


@pytest.fixture
def host():
    FakeClaude.made = []
    return BrainHost(lambda p, w, m: FakeClaude(p, w, m))


START = {"system_prompt": "You are NOVA.", "web_search": True, "model": "claude-haiku-5-5"}


def test_host_runs_one_conversation(host):
    sid = host.start(START)["session"]
    out = host.send({"session": sid, "message": "hi", "timeout_s": 30})
    assert json.loads(out["text"])["say"] == "echo hi" and out["used_tools"] == []
    assert host.send({"session": sid, "message": "search x", "timeout_s": 30})["used_tools"] == ["WebSearch"]
    assert FakeClaude.made[0].prompt == "You are NOVA." and FakeClaude.made[0].web is True


def test_a_new_start_closes_the_old_session(host):
    old = host.start(START)["session"]
    host.start(START)
    assert FakeClaude.made[0].closed
    with pytest.raises(Exception):
        host.send({"session": old, "message": "hi", "timeout_s": 30})


@pytest.mark.parametrize("body", [
    {}, {"system_prompt": "", "web_search": True, "model": "haiku"},
    {"system_prompt": "x", "web_search": "yes", "model": "haiku"},
    {"system_prompt": "x", "web_search": True, "model": "haiku; rm -rf /"},
    {"system_prompt": "x" * 60_001, "web_search": True, "model": "haiku"},
])
def test_bad_starts_are_refused(host, body):
    with pytest.raises(Exception):
        host.start(body)


def test_a_broken_session_is_reported_and_dropped(host):
    host._factory = lambda p, w, m: FakeClaude(p, w, m, fail=True)
    sid = host.start(START)["session"]
    assert "error" in host.send({"session": sid, "message": "hi", "timeout_s": 30})
    assert not host.active()


def test_service_maps_brain_errors_to_400(tmp_path, host):
    svc = HubService(model=None, data_dir=tmp_path, brain=host)
    with pytest.raises(HubError):
        svc.brain_send({"session": "nope", "message": "hi", "timeout_s": 30})
    with pytest.raises(HubError):
        HubService(model=None, data_dir=tmp_path).brain_start(START)


def test_brain_routes_are_owner_only_over_http(tmp_path, host):
    (tmp_path / "hub_owner").write_text("manav@example.com")
    server = make_server(HubService(model=None, data_dir=tmp_path, brain=host), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    addr = server.server_address
    try:
        def call(path, body, login=None):
            req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}", data=json.dumps(body).encode(),
                                         method="POST")
            if login:
                req.add_header(OWNER_HEADER, login)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None

        assert call("/v1/brain/start", START)[0] == 403
        assert call("/v1/brain/start", START, login="evil@x.com")[0] == 403
        assert FakeClaude.made == []
        status, out = call("/v1/brain/start", START, login="manav@example.com")
        assert status == 200
        status, out = call("/v1/brain/send", {"session": out["session"], "message": "hi", "timeout_s": 30},
                           login="manav@example.com")
        assert status == 200 and "echo hi" in out["text"]
    finally:
        server.shutdown()
        server.server_close()


# --- laptop side ---------------------------------------------------------------------

class HubEgress:
    """Stands in for egress.post_json to the hub, backed by a real BrainHost."""

    def __init__(self, host, down=False):
        self.host, self.down, self.calls = host, down, []

    def post_json(self, url, body, *, headers, timeout=20.0, method="POST"):
        self.calls.append(url.rsplit("/", 1)[-1])
        if self.down:
            raise EgressError("unreachable")
        return getattr(self.host, url.rsplit("/", 1)[-1])(body)


def _cfg(host="nova-hub.tail1.ts.net", where="auto"):
    return dataclasses.replace(Config(), hub=dataclasses.replace(HubConfig(), host=host),
                               brain=dataclasses.replace(BrainConfig(), where=where))


async def test_remote_session_talks_to_the_hub(host):
    eg = HubEgress(host)
    rs = RemoteSession(_cfg(), eg, system_prompt="You are NOVA.", web_search=False, model="haiku")
    reply = await rs.send("hello", timeout_s=30)
    assert "echo hello" in reply.text and rs.turns == 1 and eg.calls == ["start", "send"]
    await rs.close()
    assert FakeClaude.made[0].closed


async def test_auto_falls_back_to_the_laptop_before_the_conversation_starts(host):
    local = FakeClaude("p", False, "haiku")
    auto = AutoSession(RemoteSession(_cfg(), HubEgress(host, down=True), system_prompt="p", web_search=False,
                                     model="haiku"), lambda: local)
    reply = await auto.send("hi", timeout_s=30)
    assert "echo hi" in reply.text and auto.where == "laptop" and local.sent == ["hi"]


async def test_auto_never_switches_mid_conversation(host):
    eg = HubEgress(host)
    auto = AutoSession(RemoteSession(_cfg(), eg, system_prompt="p", web_search=False, model="haiku"),
                       lambda: FakeClaude("p", False, "haiku"))
    await auto.send("one", timeout_s=30)
    eg.down = True
    with pytest.raises(ClaudeSessionError):
        await auto.send("two", timeout_s=30)
    assert auto.where == "hub"


async def test_hub_only_mode_has_no_fallback(host):
    auto = AutoSession(RemoteSession(_cfg(), HubEgress(host, down=True), system_prompt="p", web_search=False,
                                     model="haiku"), None)
    with pytest.raises((ClaudeSessionError, HubUnavailable)):
        await auto.send("hi", timeout_s=30)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AuditLog", lambda **kw: AuditLog(path=tmp_path / "audit.log"))
    made = []

    def build(cfg):
        cfg = dataclasses.replace(cfg, wake=dataclasses.replace(WakeConfig(), mode="prefix"),
                                  ui=dataclasses.replace(UiConfig(), window=False))
        a = app_mod.NovaApp(config=cfg)
        made.append(a)
        return a

    yield build
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None


def test_app_uses_the_hub_brain_when_there_is_a_hub(make_app):
    app = make_app(_cfg())
    assert isinstance(app.agent._session_factory("prompt"), AutoSession) and app.agent._cli_available()


@pytest.mark.parametrize("cfg", [_cfg(host=""), _cfg(where="laptop")])
def test_app_keeps_the_laptop_brain_otherwise(make_app, cfg):
    from nova.brain.claude_arm import ClaudeSession

    assert isinstance(make_app(cfg).agent._session_factory("prompt"), ClaudeSession)


async def test_the_whole_turn_runs_its_tools_on_the_laptop(make_app, host, monkeypatch, tmp_path):
    """Brain on the hub, hands on the laptop: the hub's Claude proposes, the
    laptop's ladder runs it and audits it."""
    import nova.system.winops as winops
    from nova.security.tiers import ArmedBy

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))

    class ActingClaude(FakeClaude):
        async def send(self, text, *, timeout_s):
            return SessionReply(text=json.dumps({"say": "Paused.", "final": True, "actions": [
                {"tool": "media_play_pause", "params": {"action": "pause"}}]}), used_tools=[])

    host._factory = lambda p, w, m: ActingClaude(p, w, m)
    app = make_app(_cfg(where="hub"))
    app.egress = HubEgress(host)
    app.hub = None  # no local-model lane in this test
    reply = await app.run_text("could you stop the tunes for a sec", source="voice", armed_by=ArmedBy.VOICE)
    assert reply.text == "Paused." and pressed
    assert "media_play_pause" in (tmp_path / "audit.log").read_text()
