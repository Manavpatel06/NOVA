"""nova.brain.max_brain + claude_arm.ClaudeSession -- the brain on Manav's
Max plan through the signed-in `claude` program (2026-10-15). No real
`claude` process is ever started (tests/conftest.py forbids it)."""
from __future__ import annotations

import asyncio
import dataclasses
import json

import numpy as np
import pytest

import nova.app as app_mod
import nova.skills  # noqa: F401
from nova.brain.agent import AgentResult
from nova.brain.claude_arm import ClaudeSession, ClaudeSessionError, SessionReply
from nova.brain.max_brain import MaxBrain, parse_reply, tool_catalogue
from nova.config import BrainConfig, Config, SecurityConfig, UiConfig, WakeConfig
from nova.security.audit import AuditLog
from nova.security.tiers import ArmedBy
from nova.skills.registry import all_skills


def j(**kw):
    return json.dumps(kw)


class FakeSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.turns = 0
        self.closed = False

    async def start(self):
        pass

    async def send(self, text, *, timeout_s):
        self.sent.append(text)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        self.turns += 1
        return r if isinstance(r, SessionReply) else SessionReply(text=r, used_tools=[])

    async def close(self):
        self.closed = True


def brain(replies, **kw):
    sessions = []

    def factory(prompt):
        s = FakeSession(replies if not sessions else kw.pop("next_replies", []))
        s.prompt = prompt
        sessions.append(s)
        return s

    t = {"now": 0.0}
    cfg = dataclasses.replace(Config(), brain=dataclasses.replace(BrainConfig(), mode="max"),
                              **({"security": kw.pop("security")} if "security" in kw else {}))
    b = MaxBrain(cfg, session_factory=factory, context_provider=lambda: "Wednesday; Tempe",
                 clock=lambda: t["now"])
    return b, sessions, t


def recorder(results=None):
    calls = []

    async def invoke(name, params, tainted):
        calls.append((name, params, tainted))
        return (results or {}).get(name, (f"{name} done.", True))

    return invoke, calls


# -- parsing ------------------------------------------------------------------

@pytest.mark.parametrize("text,say", [
    ('{"say": "Hi.", "actions": [], "final": true}', "Hi."),
    ('```json\n{"say": "Hi."}\n```', "Hi."),
    ('Sure! {"say": "a {brace} \\"q\\"", "actions": []} trailing', 'a {brace} "q"'),
])
def test_parse_reply(text, say):
    assert parse_reply(text)["say"] == say


@pytest.mark.parametrize("text", ["", "just words", "{not json", "[1,2]"])
def test_parse_reply_rejects(text):
    assert parse_reply(text) is None


def test_catalogue_lists_every_skill_with_params():
    cat = tool_catalogue(all_skills())
    for name in all_skills():
        assert f"- {name}(" in cat
    assert "open_website(site, browser?)" in cat


# -- respond ----------------------------------------------------------------------

async def test_one_round_trip_when_final_and_ok():
    b, sessions, _ = brain([j(say="Opening YouTube in Brave.", final=True,
                              actions=[{"tool": "open_website", "params": {"site": "youtube", "browser": "brave"}}])])
    invoke, calls = recorder()
    r = await b.respond("can you open youtube in grave", invoke)
    assert r.text == "Opening YouTube in Brave." and r.actions == ["open_website"]
    assert calls == [("open_website", {"site": "youtube", "browser": "brave"}, False)]
    assert len(sessions[0].sent) == 1
    assert "Manav: can you open youtube in grave" in sessions[0].sent[0]
    assert "[Wednesday; Tempe]" in sessions[0].sent[0]


async def test_results_ride_along_with_the_next_message():
    b, sessions, _ = brain([
        j(say="Opening Notepad.", final=True, actions=[{"tool": "open_app", "params": {"app": "notepad"}}]),
        j(say="Closing it.", final=True, actions=[{"tool": "close_app", "params": {"app": "notepad"}}]),
    ])
    invoke, _ = recorder()
    await b.respond("open notepad", invoke)
    await b.respond("close it", invoke)
    assert sessions[0].sent[1].startswith("(Results of your last actions: open_app: ok")


async def test_second_round_when_not_final_or_failed():
    b, sessions, _ = brain([
        j(say="", final=False, actions=[{"tool": "battery", "params": {}}]),
        j(say="You're at 63 percent.", actions=[]),
    ])
    invoke, _ = recorder({"battery": ("Battery's at 63 percent.", True)})
    r = await b.respond("how's my battery", invoke)
    assert r.text == "You're at 63 percent."
    assert sessions[0].sent[1].startswith("RESULTS:\n- battery: ok -- Battery's at 63 percent.")

    b, sessions, _ = brain([
        j(say="Closing Spotify.", final=True, actions=[{"tool": "close_app", "params": {"app": "spotify"}}]),
        j(say="Spotify isn't open.", actions=[]),
    ])
    invoke, _ = recorder({"close_app": ("Spotify isn't open.", False)})
    r = await b.respond("close spotify", invoke)
    assert r.text == "Spotify isn't open." and "FAILED" in sessions[0].sent[1]


async def test_plain_text_is_spoken_never_acted_on():
    b, _, _ = brain(["Sure, opening it now. open_app notepad"])
    invoke, calls = recorder()
    r = await b.respond("open notepad", invoke)
    assert calls == [] and r.text.startswith("Sure")


async def test_unreachable_before_any_action_falls_back_to_router():
    b, _, t = brain([ClaudeSessionError("no reply")])
    invoke, calls = recorder()
    assert await b.respond("mute", invoke) is None
    assert not b.available()  # router for a short while...
    t["now"] = 100
    assert b.available()      # ...then try Claude again


async def test_failure_after_an_action_does_not_fall_back():
    b, _, _ = brain([j(say="", final=False, actions=[{"tool": "mute", "params": {}}]), ClaudeSessionError("x")])
    invoke, calls = recorder()
    r = await b.respond("mute and tell me", invoke)
    assert r is not None and len(calls) == 1


async def test_web_search_taints_actions_until_the_session_is_replaced():
    b, sessions, t = brain([
        SessionReply(text=j(say="Here's what I found."), used_tools=["WebSearch"]),
        j(say="Opening.", final=True, actions=[{"tool": "open_website", "params": {"site": "evil.com"}}]),
    ], next_replies=[j(say="Opening.", final=True, actions=[{"tool": "open_website", "params": {"site": "x.com"}}])])
    invoke, calls = recorder()
    r = await b.respond("look it up", invoke)
    assert r.used_web
    await b.respond("open that site", invoke)
    assert calls[0][2] is True
    t["now"] = 10_000  # idle -> new session, clean
    await b.respond("open x", invoke)
    assert calls[1][2] is False and sessions[0].closed and len(sessions) == 2


async def test_untrusted_output_skill_taints_later_actions():
    b, _, _ = brain([
        j(say="", final=False, actions=[{"tool": "weather", "params": {}}]),
        j(say="Opening.", final=True, actions=[{"tool": "open_website", "params": {"site": "youtube"}}]),
    ])
    invoke, calls = recorder()
    await b.respond("weather then youtube", invoke)
    assert [c[2] for c in calls] == [False, True]


async def test_context_is_redacted():
    b, sessions, _ = brain([j(say="ok")], security=dataclasses.replace(SecurityConfig(), redact_terms=["Tempe"]))
    invoke, _ = recorder()
    await b.respond("hi", invoke)
    assert "Tempe" not in sessions[0].sent[0]


def test_unavailable_in_other_modes_or_without_cli():
    for mode in ("api", "router"):
        cfg = dataclasses.replace(Config(), brain=dataclasses.replace(BrainConfig(), mode=mode))
        assert not MaxBrain(cfg, session_factory=lambda p: None).available()
    cfg = dataclasses.replace(Config(), brain=dataclasses.replace(BrainConfig(), mode="max"))
    assert not MaxBrain(cfg, session_factory=lambda p: None, cli_available=lambda: False).available()


def test_system_prompt_has_persona_protocol_and_tools():
    b, _, _ = brain([])
    p = b.system_prompt()
    assert "You are NOVA" in p and '"final"' in p and "- open_app(" in p


# -- ClaudeSession over a fake process ----------------------------------------------

class _Stdin:
    def __init__(self):
        self.lines = []

    def write(self, b):
        self.lines.append(b)

    async def drain(self):
        pass

    def close(self):
        pass


class _Proc:
    def __init__(self, events):
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        for e in events:
            self.stdout.feed_data((json.dumps(e) + "\n").encode())
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = None
        self.pid = 4242
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _Egress:
    def __init__(self):
        self.released = []

    def release_for_process(self, purpose, payload):
        self.released.append((purpose, payload))
        return payload.replace("SECRET", "[redacted]")


def _session(tmp_path, proc):
    s = ClaudeSession(egress=_Egress(), audit=AuditLog(path=tmp_path / "a.log"), model="haiku",
                      system_prompt="x", web_search=True)
    s._proc = proc
    return s


async def test_session_sends_one_json_line_and_reads_the_result(tmp_path):
    proc = _Proc([
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "server_tool_use", "name": "WebSearch"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": '{"say": "hi"}'},
    ])
    s = _session(tmp_path, proc)
    r = await s.send("hello SECRET", timeout_s=5)
    assert r.text == '{"say": "hi"}' and r.used_tools == ["WebSearch"] and not r.is_error
    sent = json.loads(proc.stdin.lines[0].decode())
    assert sent == {"type": "user", "message": {"role": "user", "content": "hello [redacted]"}}
    assert s._egress.released[0][0] == "claude_session"


async def test_session_timeout_kills_the_process(tmp_path):
    proc = _Proc([{"type": "system", "subtype": "init"}])  # never answers
    s = _session(tmp_path, proc)
    with pytest.raises(ClaudeSessionError):
        await s.send("hi", timeout_s=0.2)
    assert proc.killed and s._proc is None


async def test_session_exit_is_an_error(tmp_path):
    proc = _Proc([])
    proc.stdout.feed_eof()
    s = _session(tmp_path, proc)
    with pytest.raises(ClaudeSessionError):
        await s.send("hi", timeout_s=2)


def test_session_argv_keeps_local_tools_off(tmp_path):
    s = ClaudeSession(egress=_Egress(), audit=AuditLog(path=tmp_path / "a.log"), model="haiku",
                      system_prompt="SYS", web_search=True)
    argv = s._argv("claude")
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert "--safe-mode" in argv and "--no-session-persistence" in argv
    assert not any("skip-permissions" in a or a == "--bare" for a in argv)
    off = ClaudeSession(egress=_Egress(), audit=AuditLog(path=tmp_path / "b.log"), model="haiku",
                        system_prompt="SYS", web_search=False)._argv("claude")
    assert off[off.index("--tools") + 1] == "" and "--allowedTools" not in off


# -- in the app --------------------------------------------------------------------

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
                              ui=dataclasses.replace(UiConfig(), window=False))
    a = app_mod.NovaApp(config=cfg)
    a.synth = _Synth()
    a.player = _Player()
    yield a
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None


def test_max_is_the_default_brain(app):
    assert app.config.brain.mode == "max" and isinstance(app.agent, MaxBrain)


async def test_app_runs_the_max_brain_through_the_ladder(app, monkeypatch, tmp_path):
    import nova.system.winops as winops

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))
    sess = FakeSession([j(say="Paused.", final=True, actions=[{"tool": "media_play_pause", "params": {"action": "pause"}}])])
    app.agent._session_factory = lambda p: sess
    app.agent._cli_available = lambda: True
    reply = await app.run_text("could you stop the music for a sec", source="voice", armed_by=ArmedBy.VOICE)
    assert reply.text == "Paused." and pressed
    assert "media_play_pause" in (tmp_path / "audit.log").read_text(encoding="utf-8")


async def test_app_falls_back_to_router_when_claude_is_missing(app):
    app.agent._cli_available = lambda: False
    reply = await app.run_text("what time is it", source="voice", armed_by=ArmedBy.VOICE)
    assert reply.text.startswith("It's")


# -- two-speed brain (2026-10-16) ----------------------------------------------------

def _wire(app, replies):
    sess = FakeSession(replies)
    app.agent._session_factory = lambda p: sess
    app.agent._cli_available = lambda: True
    return sess


@pytest.mark.parametrize("text,skill", [
    ("mute", "mute"), ("Nova volume 40", None), ("set volume to 40", "set_volume"), ("louder", "volume_up"),
    ("pause", "media_play_pause"), ("next song", "media_next"), ("what time is it?", "time"),
    ("lock my pc", "lock_computer"), ("turn off wifi", "set_radio"), ("how much battery do I have", "battery"),
])
def test_instant_lane_takes_simple_exact_commands(app, text, skill):
    m = app._instant_match(text)
    assert (m.spec.name if m else None) == skill


@pytest.mark.parametrize("text", [
    "can you search in grave", "open youtube in brave", "open spotify", "close notepad",
    "mute and pause the music", "pause it then lock the screen", "what's the weather",
    "set the volume to whatever feels right for a movie tonight please", "shut down",
    "who won the game last night",
])
def test_everything_else_goes_to_claude(app, text):
    assert app._instant_match(text) is None


async def test_instant_command_skips_claude_and_is_remembered(app, monkeypatch):
    import nova.system.winops as winops

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))
    sess = _wire(app, [j(say="Unpaused.", final=True,
                         actions=[{"tool": "media_play_pause", "params": {"action": "play"}}])])
    reply = await app.run_text("pause", source="voice", armed_by=ArmedBy.VOICE)
    assert pressed and sess.sent == []          # no round trip to Claude
    assert reply.success
    await app.run_text("ok put it back on, I'm ready now", source="voice", armed_by=ArmedBy.VOICE)
    assert "Handled instantly" in sess.sent[0] and "'pause'" in sess.sent[0]


async def test_instant_lane_is_off_when_the_list_is_empty(app):
    app.config = dataclasses.replace(app.config, brain=dataclasses.replace(app.config.brain, instant_skills=[]))
    assert app._instant_match("mute") is None


async def test_instant_lane_never_used_without_a_brain(app):
    """No brain available -> the ordinary router path (same result, no note)."""
    app.agent._cli_available = lambda: False
    reply = await app.run_text("what time is it", source="voice", armed_by=ArmedBy.VOICE)
    assert reply.text.startswith("It's")
