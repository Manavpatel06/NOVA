"""open_app beyond the curated allowlist: installed Start-menu apps, and
the catch-all "open X" pattern that yields to more specific skills."""
from __future__ import annotations

import json

import pytest

import nova.skills  # noqa: F401 -- registers every skill
import nova.skills.open_app as open_app_mod
from nova.brain.router import Router
from nova.system import winops

APPS = [
    ("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"),
    ("Spotify Uninstaller", "x!uninstall"),
    ("Visual Studio Code", "Microsoft.VisualStudioCode"),
    ("Discord", "com.squirrel.Discord.Discord"),
    ("Google Chrome", "Chrome"),
    ("Microsoft Word", "Microsoft.Office.WINWORD.EXE.15"),
    ("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
]


@pytest.mark.parametrize("spoken,expected", [
    ("spotify", "Spotify"), ("Spotify", "Spotify"), ("chrome", "Google Chrome"),
    ("visual studio code", "Visual Studio Code"), ("vs code", "Visual Studio Code"), ("word", "Microsoft Word"), ("discord", "Discord"),
    ("disc", "Discord"), ("dis", None), ("photoshop", None), ("", None),
])
def test_best_start_app(spoken, expected):
    hit = open_app_mod.best_start_app(spoken, APPS)
    assert (hit[0] if hit else None) == expected


@pytest.fixture
def ctx_factory(tmp_path):
    from nova.config import Config
    from nova.security.audit import AuditLog
    from nova.security.ladder import Ladder
    from nova.skills.registry import SkillCtx

    return lambda: SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.fixture
def router():
    return Router()


@pytest.mark.parametrize("text,app", [
    ("open spotify", "spotify"), ("Open Spotify.", "spotify"), ("launch discord", "discord"),
    ("start notepad", "notepad"), ("can you open visual studio code please", "visual studio code"),
    ("open the calculator app", "calculator"), ("fire up chrome", "chrome"),
])
def test_open_x_routes_to_open_app(router, text, app):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "open_app", (text, m)
    assert m.params == {"app": app}


@pytest.mark.parametrize("text,skill", [
    ("open youtube", "open_website"), ("open github.com", "open_website"), ("open gmail", "open_website"),
    ("open wifi settings", "open_settings"), ("open youtube and search for lofi", "youtube"),
])
def test_specific_skills_beat_the_open_catch_all(router, text, skill):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == skill, (text, m)


async def test_unknown_app_launches_from_start_menu(monkeypatch, ctx_factory):
    launched = []
    open_app_mod._start_apps_cache = None
    monkeypatch.setattr(winops, "powershell", lambda script, **kw: json.dumps([{"Name": n, "AppID": i} for n, i in APPS]))
    monkeypatch.setattr(winops, "launch_start_app", lambda app_id: launched.append(app_id))
    spec = nova.skills.registry.get_skill("open_app")
    reply = await spec.invoke(ctx_factory(), app="spotify")
    assert reply.success and reply.text == "Opening Spotify."
    assert launched == ["SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"]


async def test_unfound_app_is_an_honest_no(monkeypatch, ctx_factory):
    open_app_mod._start_apps_cache = None
    monkeypatch.setattr(winops, "powershell", lambda script, **kw: json.dumps([]))
    spec = nova.skills.registry.get_skill("open_app")
    reply = await spec.invoke(ctx_factory(), app="photoshop")
    assert not reply.success and "couldn't find" in reply.text


def test_launch_start_app_refuses_odd_ids(monkeypatch):
    monkeypatch.setattr(winops.sys, "platform", "win32")
    for bad in ("..\\..\\evil", "https://x.com", "\\\\server\\share", "a\nb", "x" * 400):
        with pytest.raises(ValueError):
            winops.launch_start_app(bad)


def test_ps_command_carries_args_as_data_only():
    cmd = winops.build_ps_command("Write-Output $NovaArgs.level", json.dumps({"level": "40'; rm -r C:\\"}))
    assert "$NovaArgs = @'\n" in cmd and "\n'@ | ConvertFrom-Json\n" in cmd
    with pytest.raises(ValueError):
        winops.build_ps_command("x", "{\n}")


def test_open_catch_all_has_no_catastrophic_backtracking():
    """Security review 2026-10-15: "start up" matched both as one verb and as
    "start" + " up", so a repeated phrase backtracked 2^n ways on the event
    loop (22 repeats took ~50 s). 18 repeats took seconds before the fix."""
    import time

    spec = nova.skills.registry.get_skill("open_app")
    text = "start up " * 18 + "z" * 60
    for rx in spec.catch_all_patterns:
        t0 = time.perf_counter()
        rx.fullmatch(text)
        assert time.perf_counter() - t0 < 0.5


@pytest.mark.parametrize("text,app", [
    ("start up spotify", "spotify"), ("boot up discord", "discord"), ("open up chrome", "chrome"),
    ("run notepad", "notepad"), ("load up word", "word"),
])
def test_open_verbs_still_route_after_the_backtracking_fix(router, text, app):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "open_app", (text, m)
    assert m.params == {"app": app}


async def test_confirm_prompt_names_the_start_menu_app_that_will_launch(monkeypatch, tmp_path):
    """Security review 2026-10-15: a Claude-proposed open_app (CONFIRM floor)
    for an app off the allowlist asked "open 'spotify' (would be refused...)"
    and then, on "yes", launched it from the Start menu anyway. Consent must
    name the entry that will run, and a different entry after the answer
    must be refused."""
    from nova.config import Config
    from nova.security.audit import AuditLog
    from nova.security.ladder import Ladder
    from nova.security.tiers import Origin
    from nova.skills.registry import SkillCtx

    shown, launched = [], []

    class _Channel:
        async def request(self, skill, params):
            shown.append(dict(params))
            return True

    open_app_mod._start_apps_cache = None
    monkeypatch.setattr(winops, "powershell", lambda script, **kw: json.dumps([{"Name": n, "AppID": i} for n, i in APPS]))
    monkeypatch.setattr(winops, "launch_start_app", lambda app_id: launched.append(app_id))
    ctx = SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "a.log"), confirm_channel=_Channel()),
                   origin=Origin.CLAUDE)
    reply = await nova.skills.registry.get_skill("open_app").invoke(ctx, app="spotify")
    assert shown and shown[0]["app"] == "Spotify" and "refused" not in shown[0]["app"]
    assert shown[0]["start_menu_app_id"] == "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
    assert reply.success and launched == ["SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"]
    open_app_mod._start_apps_cache = None


async def test_start_menu_entry_changing_during_confirm_is_refused(monkeypatch, tmp_path):
    from nova.config import Config
    from nova.security.audit import AuditLog
    from nova.security.ladder import Ladder
    from nova.security.tiers import Origin
    from nova.skills.registry import SkillCtx

    launched = []
    apps = {"list": [("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify")]}

    class _Channel:
        async def request(self, skill, params):
            # while the human is answering, a different "Spotify" appears
            apps["list"] = [("Spotify", "Evil.Spotify!App")]
            open_app_mod._start_apps_cache = None
            return True

    open_app_mod._start_apps_cache = None
    monkeypatch.setattr(winops, "powershell", lambda script, **kw: json.dumps([{"Name": n, "AppID": i} for n, i in apps["list"]]))
    monkeypatch.setattr(winops, "launch_start_app", lambda app_id: launched.append(app_id))
    ctx = SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "a.log"), confirm_channel=_Channel()),
                   origin=Origin.CLAUDE)
    reply = await nova.skills.registry.get_skill("open_app").invoke(ctx, app="spotify")
    assert not reply.success and launched == []
    open_app_mod._start_apps_cache = None
