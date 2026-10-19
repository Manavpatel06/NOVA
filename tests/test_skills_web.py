"""web_search / youtube / open_website (nova/skills/web.py) -- ACT tier.

Never touches a real browser: winops.open_url is monkeypatched. The real
winops.safe_https_url still runs, so every URL asserted here is one the
real doorway would accept.
"""
from __future__ import annotations

import pytest

import nova.skills  # noqa: F401  (populates the live registry)
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills.registry import SkillCtx, all_skills, get_skill
from nova.skills.web import SITES, percent_encode, resolve_site
from nova.system import winops


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.fixture
def opened(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(winops, "open_url", lambda url: urls.append(url))
    return urls


@pytest.fixture(scope="module")
def router():
    return Router()


# -- percent encoding ---------------------------------------------------------


def test_percent_encode_spaces_unreserved_and_specials():
    assert percent_encode("how tall is everest") == "how+tall+is+everest"
    assert percent_encode("a-b_c.d~e") == "a-b_c.d~e"
    assert percent_encode("a&b=c#d/e?f%") == "a%26b%3Dc%23d%2Fe%3Ff%25"
    assert percent_encode("café") == "caf%C3%A9"
    assert percent_encode("a b", plus_for_space=False) == "a%20b"


def test_percent_encoded_query_can_never_add_a_parameter_or_change_host():
    enc = percent_encode("x&q=evil#frag @evil.com/path")
    for ch in "&=#@/ ":
        assert ch not in enc


# -- routing ------------------------------------------------------------------


@pytest.mark.parametrize("text, query", [
    ("google how tall is mount everest", "how tall is mount everest"),
    ("search for cheap flights to delhi", "cheap flights to delhi"),
    ("search python list comprehension", "python list comprehension"),
    ("look up python decorators", "python decorators"),
    ("search google for best pizza in tempe", "best pizza in tempe"),
    ("can you look up the weather in paris on google please", None),
    ("search the web for asu parking", "asu parking"),
    ("google search best laptops", "best laptops"),
])
def test_web_search_routes(router, text, query):
    m = router.route(text)
    if query is None:  # just must not be stolen by another skill
        assert m.spec is not None and m.spec.name == "google_search"
        return
    assert m.spec is not None and m.spec.name == "google_search", m.clarification
    assert m.params == {"query": query}


@pytest.mark.parametrize("text, query", [
    ("search youtube for lofi beats", "lofi beats"),
    ("play lofi beats on youtube", "lofi beats"),
    ("youtube interstellar trailer", "interstellar trailer"),
    ("look up cat videos on youtube", "cat videos"),
    ("search for minecraft speedruns on youtube", "minecraft speedruns"),
    ("open youtube and search for lofi", "lofi"),
])
def test_youtube_routes_and_is_not_stolen_by_web_search(router, text, query):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "youtube", m.clarification
    assert m.params == {"query": query}


@pytest.mark.parametrize("text, site", [
    ("open github.com", "github com"),
    ("open github dot com", "github"),
    ("go to youtube", "youtube"),
    ("open reddit", "reddit"),
    ("open gmail", "gmail"),
    ("open google calendar", "google calendar"),
    ("open the reddit website", "reddit"),
    ("open youtube in chrome", "youtube"),
    ("take me to linkedin", "linkedin"),
    ("open canvas", "canvas"),
    ("open my asu", "my asu"),
    ("open example.com", "example com"),
    ("visit docs.python.org", "docs python org"),
    ("go to perplexity dot ai", "perplexity dot ai"),
])
def test_open_website_routes(router, text, site):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "open_website", m.clarification
    assert m.params.get("site") == site


@pytest.mark.parametrize("text", ["open notepad", "open file explorer", "launch calculator",
                                  "open task manager", "open control panel"])
def test_desktop_apps_still_route_to_open_app(router, text):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "open_app", m.clarification


@pytest.mark.parametrize("text", ["open spotify", "open notepad", "open file explorer", "open calculator",
                                  "open notepad app", "open spotify app", "open discord", "open samsung tv",
                                  "open file in", "open steam"])
def test_open_website_never_claims_a_bare_unknown_word(router, text):
    """open_website's patterns only fire on known sites / domain shapes, and
    routing of these is identical with or without the web skills present."""
    spec = get_skill("open_website")
    norm = Router.normalize(text)
    assert not any(rx.fullmatch(norm) for rx in spec.patterns), text
    m = router.route(text)
    assert m.spec is None or m.spec.name not in ("open_website", "google_search", "youtube")
    without = Router(skills={k: v for k, v in all_skills().items()
                             if k not in ("open_website", "google_search", "youtube")})
    m2 = without.route(text)
    assert (m.spec.name if m.spec else None) == (m2.spec.name if m2.spec else None)


def test_open_spotify_routes_exactly_as_before_the_web_skills():
    """'open spotify' must never become a website. (It reaches add_app/open_app
    by fuzzy match -- whatever it did before, it still does.)"""
    with_web = Router().route("open spotify")
    assert with_web.spec is None or with_web.spec.name in ("open_app", "add_app", "switch_to_app")


def test_all_three_are_act_tier():
    for name in ("google_search", "youtube", "open_website"):
        assert get_skill(name).tier is Tier.ACT


# -- resolve_site ---------------------------------------------------------------


def test_every_built_in_site_is_a_safe_https_url():
    for name, (url, spoken) in SITES.items():
        assert winops.safe_https_url(url) == url, name
        assert spoken


@pytest.mark.parametrize("spoken, url", [
    ("github", "https://github.com"),
    ("github com", "https://github.com"),
    ("example com", "https://example.com"),
    ("www python org", "https://python.org"),
    ("docs python org", "https://docs.python.org"),
    ("perplexity dot ai", "https://perplexity.ai"),
    ("canvas", "https://canvas.asu.edu"),
    ("my asu", "https://my.asu.edu"),
    ("the reddit website", "https://www.reddit.com"),
])
def test_resolve_site(spoken, url):
    assert resolve_site(spoken)[0] == url


@pytest.mark.parametrize("bad", ["notepad", "", "192 168 0 1", "localhost", "evil local",
                                 "a@b com", "printer lan", "x" * 300 + " com"])
def test_resolve_site_refuses_non_sites(bad):
    assert resolve_site(bad) is None


# -- handlers through the ladder -------------------------------------------------


async def test_web_search_opens_google_with_encoded_query(ctx, opened):
    reply = await get_skill("google_search").invoke(ctx, query="for cheap flights to delhi")
    assert reply.success
    assert opened == ["https://www.google.com/search?q=cheap+flights+to+delhi"]
    assert "google" in reply.text.lower()


async def test_web_search_query_cannot_inject_url_parts(ctx, opened):
    await get_skill("google_search").invoke(ctx, query="a&b=c#d")
    assert opened == ["https://www.google.com/search?q=a%26b%3Dc%23d"]


async def test_web_search_empty_query_asks(ctx, opened):
    reply = await get_skill("google_search").invoke(ctx, query="for ")
    assert not reply.success and opened == []


async def test_web_search_long_query_is_capped(ctx, opened):
    await get_skill("google_search").invoke(ctx, query="word " * 200)
    assert len(opened[0]) < 260


async def test_youtube_opens_results_page(ctx, opened):
    reply = await get_skill("youtube").invoke(ctx, query="lofi beats")
    assert reply.success
    assert opened == ["https://www.youtube.com/results?search_query=lofi+beats"]


async def test_open_website_known_and_domain(ctx, opened):
    r1 = await get_skill("open_website").invoke(ctx, site="google calendar")
    r2 = await get_skill("open_website").invoke(ctx, site="github com")
    assert opened == ["https://calendar.google.com", "https://github.com"]
    assert r1.text == "Opening Google Calendar."
    assert r2.text == "Opening github.com."


async def test_open_website_unknown_is_refused_without_opening(ctx, opened):
    reply = await get_skill("open_website").invoke(ctx, site="notepad")
    assert not reply.success and opened == []


async def test_not_on_windows_is_an_honest_refusal(ctx, monkeypatch):
    def unavailable(url):
        raise winops.WinOpsUnavailable("no")
    monkeypatch.setattr(winops, "open_url", unavailable)
    reply = await get_skill("open_website").invoke(ctx, site="reddit")
    assert not reply.success and "windows" in reply.text.lower()


async def test_browser_failure_is_spoken(ctx, monkeypatch):
    def broken(url):
        raise winops.WinOpsError("boom")
    monkeypatch.setattr(winops, "open_url", broken)
    reply = await get_skill("youtube").invoke(ctx, query="x")
    assert not reply.success and "couldn't" in reply.text


async def test_invocation_is_audited(ctx, opened, tmp_path):
    await get_skill("google_search").invoke(ctx, query="cats")
    assert ctx.ladder is not None
    assert AuditLog(path=tmp_path / "audit.log").verify().entries >= 1


@pytest.mark.parametrize("text,browser", [
    ("open youtube in brave", "brave"), ("open youtube in chrome", "chrome"),
    ("open github.com in firefox", "firefox"), ("open gmail using edge", "edge"),
])
def test_open_website_picks_up_the_browser(router, text, browser):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "open_website"
    assert m.params.get("browser") == browser


async def test_named_browser_uses_open_url_in(monkeypatch, tmp_path):
    from nova.config import Config
    from nova.security.audit import AuditLog
    from nova.security.ladder import Ladder
    from nova.skills.registry import SkillCtx, get_skill

    calls = []
    monkeypatch.setattr(winops, "open_url_in", lambda b, u: calls.append((b, u)))
    monkeypatch.setattr(winops, "open_url", lambda u: calls.append(("default", u)))
    ctx = SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "a.log")))
    reply = await get_skill("open_website").invoke(ctx, site="youtube", browser="Brave")
    assert reply.success and reply.text == "Opening YouTube in Brave."
    assert calls == [("brave", "https://www.youtube.com")]
    reply = await get_skill("open_website").invoke(ctx, site="youtube", browser="netscape")
    assert not reply.success and len(calls) == 1
