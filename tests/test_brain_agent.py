"""nova.brain.agent -- Claude with NOVA's skills as tools (2026-10-15).
The Anthropic API is never called: Egress.post_json is a scripted fake."""
from __future__ import annotations

import dataclasses
import re

import numpy as np
import pytest

import nova.app as app_mod
import nova.skills  # noqa: F401
from nova.brain.agent import Agent, tool_definitions
from nova.config import BrainConfig, Config, SecurityConfig, UiConfig, WakeConfig
from nova.security.audit import AuditLog
from nova.security.tiers import ArmedBy
from nova.skills.registry import all_skills


class FakeEgress:
    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies = []

    def post_json(self, url, body, *, headers, timeout):
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "sk-ant-test"
        import copy

        self.bodies.append(copy.deepcopy(body))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def tool_use(name, inp, id_="tu1"):
    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": id_, "name": name, "input": inp}]}


def say(text):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}


def make_agent(replies, *, clock=None, **brain):
    brain.setdefault("mode", "api")
    cfg = dataclasses.replace(Config(), brain=dataclasses.replace(BrainConfig(), **brain))
    eg = FakeEgress(replies)
    t = {"now": 0.0}
    a = Agent(cfg, eg, key_provider=lambda: "sk-ant-test", context_provider=lambda: "Wednesday; Tempe",
              clock=clock or (lambda: t["now"]))
    return a, eg, t


def recorder(results=None):
    calls = []

    async def invoke(name, params, tainted):
        calls.append((name, params, tainted))
        return (results or {}).get(name, (f"{name} done.", True))

    return invoke, calls


def test_every_skill_becomes_a_valid_tool():
    tools = tool_definitions(all_skills())
    assert {t["name"] for t in tools} == set(all_skills())
    assert not {t["name"] for t in tools} & {"web_search", "web_fetch", "code_execution"}
    for t in tools:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["name"])
        assert t["input_schema"]["type"] == "object"
    web = next(t for t in tools if t["name"] == "open_website")
    assert web["input_schema"]["required"] == ["site"]
    assert "browser" in web["input_schema"]["properties"]


async def test_tool_then_answer():
    a, eg, _ = make_agent([tool_use("open_website", {"site": "youtube", "browser": "brave"}),
                           say("YouTube's open in Brave.")])
    invoke, calls = recorder()
    r = await a.respond("can you open youtube in grave", invoke)
    assert r.text == "YouTube's open in Brave." and r.actions == ["open_website"]
    assert calls == [("open_website", {"site": "youtube", "browser": "brave"}, False)]
    # the tool result went back to Claude
    last = eg.bodies[1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["content"] == "open_website done."


async def test_chained_tools():
    a, _, _ = make_agent([tool_use("open_website", {"site": "youtube"}),
                          tool_use("youtube", {"query": "lofi"}, "tu2"), say("Playing lofi.")])
    invoke, calls = recorder()
    r = await a.respond("open youtube and play lofi", invoke)
    assert [c[0] for c in calls] == ["open_website", "youtube"] and r.text == "Playing lofi."


async def test_failed_tool_is_reported_as_error_to_claude():
    a, eg, _ = make_agent([tool_use("close_app", {"app": "spotify"}), say("Spotify isn't open.")])
    invoke, _ = recorder({"close_app": ("Spotify isn't open.", False)})
    await a.respond("close spotify", invoke)
    assert eg.bodies[1]["messages"][-1]["content"][0]["is_error"] is True


async def test_plain_answer_without_tools():
    a, _, _ = make_agent([say("About 8,849 meters.")])
    invoke, calls = recorder()
    r = await a.respond("how tall is everest", invoke)
    assert r.text == "About 8,849 meters." and calls == []


async def test_history_gives_follow_ups_context_and_expires():
    a, eg, t = make_agent([say("Opening Notepad."), say("Closed."), say("Hi.")], history_idle_s=600)
    invoke, _ = recorder()
    await a.respond("open notepad", invoke)
    await a.respond("close it", invoke)
    msgs = eg.bodies[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "Opening Notepad."
    t["now"] = 10_000
    await a.respond("hello", invoke)
    assert len(eg.bodies[2]["messages"]) == 1  # forgotten after idle


async def test_web_results_taint_later_tool_calls():
    web_then_tool = {"stop_reason": "tool_use", "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
        {"type": "tool_use", "id": "tu1", "name": "open_website", "input": {"site": "evil.com"}},
    ]}
    a, _, _ = make_agent([web_then_tool, say("ok"), tool_use("mute", {}), say("Muted.")])
    invoke, calls = recorder()
    r = await a.respond("look something up", invoke)
    assert r.used_web and calls[0][2] is True
    await a.respond("mute", invoke)  # next sentence can still see the web-derived answer
    assert calls[1][2] is True


async def test_web_taint_lasts_as_long_as_the_web_answer_is_remembered():
    """Security review 2026-10-15: the web-derived answer stays in the history
    for history_turns exchanges, so every turn that can still see it is
    tainted -- not only the one right after it."""
    web = {"stop_reason": "end_turn", "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
        {"type": "text", "text": "Here's the news."},
    ]}
    a, eg, _ = make_agent([web, say("Hi."), tool_use("open_website", {"site": "evil.com"}), say("ok")],
                          history_turns=8)
    invoke, calls = recorder()
    await a.respond("what's the news", invoke)
    await a.respond("hello", invoke)                     # turn 2: no web, no tools
    await a.respond("open that site", invoke)            # turn 3: still sees turn 1's answer
    assert "Here's the news." in [m["content"] for m in eg.bodies[2]["messages"] if m["role"] == "assistant"]
    assert calls == [("open_website", {"site": "evil.com"}, True)]


async def test_taint_clears_once_the_web_exchange_leaves_the_history():
    web = {"stop_reason": "end_turn", "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
        {"type": "text", "text": "News."},
    ]}
    a, _, _ = make_agent([web, say("a"), tool_use("mute", {}), say("Muted.")], history_turns=1)
    invoke, calls = recorder()
    await a.respond("news", invoke)
    await a.respond("hello", invoke)   # pushes the web exchange out of a 1-exchange history
    await a.respond("mute", invoke)
    assert calls == [("mute", {}, False)]


async def test_tool_results_and_context_are_redacted_before_they_leave():
    """Tool result texts (OS errors with paths, window titles) and the context
    line are outbound text too."""
    cfg = dataclasses.replace(Config(), security=dataclasses.replace(SecurityConfig(), redact_terms=["Patel"]),
                              brain=dataclasses.replace(BrainConfig(), mode="api"))
    eg = FakeEgress([tool_use("open_app", {"app": "x"}), say("ok")])
    a = Agent(cfg, eg, key_provider=lambda: "sk-ant-test", context_provider=lambda: "app in front: Patel.exe")
    invoke, _ = recorder({"open_app": (r"Couldn't open x: [WinError 5] 'C:\\Users\\Patel\\AppData\\x.exe'", False)})
    await a.respond("open x", invoke)
    import json

    assert "Patel" not in json.dumps(eg.bodies[0]["messages"])
    assert "Patel" not in json.dumps(eg.bodies[1]["messages"])
    assert eg.bodies[1]["messages"][-1]["content"][0]["type"] == "tool_result"  # the result did go out


async def test_unreachable_brain_returns_none_for_router_fallback():
    from nova.security.egress import EgressError

    a, _, _ = make_agent([EgressError("api.anthropic.com unreachable")])
    invoke, calls = recorder()
    assert await a.respond("mute", invoke) is None and calls == []


async def test_bad_key_is_dropped_and_reread_later():
    from nova.security.egress import EgressError

    err = EgressError("HTTP 401")
    err.status = 401
    a, _, _ = make_agent([err])
    invoke, _ = recorder()
    assert a.available()
    assert await a.respond("hi", invoke) is None
    assert a._key is None


def test_not_available_without_key_or_in_router_mode():
    cfg = dataclasses.replace(Config(), brain=dataclasses.replace(BrainConfig(), mode="api"))
    assert not Agent(cfg, object(), key_provider=lambda: None).available()
    rcfg = dataclasses.replace(cfg, brain=dataclasses.replace(BrainConfig(), mode="router"))
    assert not Agent(rcfg, object(), key_provider=lambda: "k").available()


async def test_outbound_text_is_redacted():
    cfg = dataclasses.replace(Config(), security=dataclasses.replace(SecurityConfig(), redact_terms=["Patel"]),
                              brain=dataclasses.replace(BrainConfig(), mode="api"))
    eg = FakeEgress([say("ok")])
    a = Agent(cfg, eg, key_provider=lambda: "sk-ant-test", context_provider=lambda: "")
    invoke, _ = recorder()
    await a.respond("email Manav Patel", invoke)
    sent = eg.bodies[0]["messages"][-1]["content"]
    assert "Patel" not in sent


async def test_web_search_tool_and_caching_present():
    a, eg, _ = make_agent([say("ok")])
    invoke, _ = recorder()
    await a.respond("hi", invoke)
    tools = eg.bodies[0]["tools"]
    assert tools[-1]["type"].startswith("web_search") and "cache_control" in tools[-1]
    assert eg.bodies[0]["model"] == "claude-haiku-5-5"
    a2, eg2, _ = make_agent([say("ok")], web_search=False)
    await a2.respond("hi", invoke)
    assert all(t.get("name") != "web_search" for t in eg2.bodies[0]["tools"])


# -- in the app ------------------------------------------------------------------

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


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AuditLog", lambda **kw: AuditLog(path=tmp_path / "audit.log"))

    async def _no_claude(*_a, **_kw):
        return app_mod.ClaudeFailure(reason="cli_not_found")

    monkeypatch.setattr(app_mod.claude_arm, "ask", _no_claude)
    cfg = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), mode="prefix"),
                              ui=dataclasses.replace(UiConfig(), window=False),
                              brain=dataclasses.replace(BrainConfig(), mode="api"))
    a = app_mod.NovaApp(config=cfg)
    a.synth = _Synth()
    a.player = _Player()
    yield a
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None


def _install(app, replies):
    eg = FakeEgress(replies)
    app.agent = Agent(app.config, eg, key_provider=lambda: "sk-ant-test", context_provider=lambda: "")
    return eg


async def test_app_sends_sentences_to_the_brain_and_runs_tools_through_the_ladder(app, monkeypatch, tmp_path):
    import nova.system.winops as winops

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))
    _install(app, [tool_use("media_play_pause", {"action": "pause"}), say("Paused.")])
    reply = await app.run_text("could you stop the music for a sec", source="voice", armed_by=ArmedBy.VOICE)
    assert reply.text == "Paused." and app.synth.said[-1] == "Paused."
    assert pressed  # the skill really ran
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "media_play_pause" in audit  # ...and through the ladder


async def test_app_falls_back_to_router_when_brain_unreachable(app):
    from nova.security.egress import EgressError

    _install(app, [EgressError("unreachable")])
    reply = await app.run_text("what time is it", source="voice", armed_by=ArmedBy.VOICE)
    assert reply.text.startswith("It's")


async def test_wake_armed_turns_never_use_the_brain(app):
    eg = _install(app, [say("should not be called")])
    await app.run_text("what time is it", source="voice", armed_by=ArmedBy.WAKE)
    assert eg.bodies == []


async def test_risky_tool_from_brain_still_needs_spoken_confirm(app, monkeypatch):
    import nova.system.winops as winops

    ran = []
    monkeypatch.setattr(winops, "run_program", lambda *a, **k: ran.append(a))

    asked = []

    async def answer(prompt):
        asked.append(prompt)
        return "yes"  # plain yes is NOT enough for shutdown

    app.ladder._confirm_channel._ask = answer
    eg = _install(app, [tool_use("shutdown_computer", {}), say("Okay, I won't.")])
    await app.run_text("shut it all down", source="voice", armed_by=ArmedBy.VOICE)
    assert ran == []
    assert asked and "Nova, confirm" in asked[0]
    assert eg.bodies[1]["messages"][-1]["content"][0]["is_error"] is True


async def test_untrusted_output_skill_taints_following_calls():
    import types

    skills = dict(all_skills())
    spec = skills["weather"]
    assert spec.untrusted_output is True
    a, _, _ = make_agent([tool_use("weather", {}), tool_use("open_website", {"site": "x.com"}, "tu2"), say("ok")])
    invoke, calls = recorder()
    await a.respond("weather then open something", invoke)
    assert [c[2] for c in calls] == [False, True]


async def test_tainted_action_needs_nova_confirm_not_plain_yes(app, monkeypatch):
    import nova.system.winops as winops

    opened = []
    monkeypatch.setattr(winops, "open_url", lambda u: opened.append(u))
    asked = []

    async def answer(prompt):
        asked.append(prompt)
        return "yes"

    app.ladder._confirm_channel._ask = answer
    web = {"stop_reason": "tool_use", "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
        {"type": "tool_use", "id": "tu1", "name": "open_website", "input": {"site": "youtube"}}]}
    _install(app, [web, say("ok")])
    await app.run_text("find me something", source="voice", armed_by=ArmedBy.VOICE)
    assert asked and "Nova, confirm" in asked[0]
    assert opened == []


async def test_no_router_rerun_after_a_tool_already_ran(app, monkeypatch):
    import nova.system.winops as winops

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))
    bad = {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t2", "name": "mute", "input": 5}]}
    _install(app, [tool_use("media_play_pause", {"action": "pause"}), bad])
    reply = await app.run_text("pause it and then tell me", source="voice", armed_by=ArmedBy.VOICE)
    assert len(pressed) == 1
    assert reply is not None and not reply.success
