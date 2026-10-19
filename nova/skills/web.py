"""web -- browser skills, all ACT tier (2026-10-15).

  web_search   "google how tall is mount everest", "look up python decorators"
  youtube      "search youtube for lofi beats", "play interstellar trailer on youtube"
  open_website "open github.com", "go to reddit", "open google calendar"

Every one of these ends in `winops.open_url()`, which only ever hands a
validated https URL to the default browser. The user's words never become
a host: searches go to a fixed google.com / youtube.com URL with the query
percent-encoded into the query string, and open_website only opens a site
from the built-in map below or a spoken domain that passes
`winops.safe_https_url`.

open_website vs open_app: "open notepad" / "open spotify" belong to
nova/skills/open_app.py, which has no patterns and is matched by its
examples. So open_website's patterns only fire when the target is a KNOWN
site name or is shaped like a domain ("github dot com", "example.com" --
the router's normalizer turns the dot into a space, hence the TLD list).
A bare unknown word ("open notepad") never matches here.

Privacy: queries are never logged -- only that a search happened.
"""

from __future__ import annotations

import asyncio
import re

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

logger = get_logger("skills.web")

_MAX_QUERY = 200
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")


def percent_encode(text: str, *, plus_for_space: bool = True) -> str:
    """Percent-encode `text` (UTF-8) for a URL query value. Our own tiny
    version because urllib is a transport module, banned outside
    nova/security/egress.py. Only RFC 3986 unreserved characters pass
    through; everything else -- '&', '#', '/', '=', '%', non-ASCII -- is
    %XX-escaped, so a query can never add a parameter or change the path."""
    out: list[str] = []
    for ch in text:
        if ch in _UNRESERVED:
            out.append(ch)
        elif ch == " " and plus_for_space:
            out.append("+")
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def _clean_query(query: str) -> str:
    q = re.sub(r"\s+", " ", query or "").strip()
    return q[:_MAX_QUERY].strip()


async def _open(url: str, what: str, browser: str = "") -> Reply | None:
    """Open `url` (in `browser` if one is named); None on success, or a
    spoken failure Reply."""
    safe = winops.safe_https_url(url)
    if safe is None:
        logger.warning("refusing to open a non-https / unsafe URL for %s", what)
        return Reply(text="I couldn't open that.", success=False)
    b = " ".join((browser or "").lower().split())
    if b in ("", "browser", "the browser", "default", "my browser"):
        b = ""
    if b and b not in winops.BROWSERS:
        return Reply(text=f"I don't know a browser called {browser}.", success=False)
    try:
        if b:
            await asyncio.to_thread(winops.open_url_in, b, safe)
        else:
            await asyncio.to_thread(winops.open_url, safe)
    except winops.WinOpsUnavailable:
        return Reply(text="I can only open the browser on Windows.", success=False)
    except (winops.WinOpsError, ValueError, OSError) as exc:
        logger.error("opening the browser for %s failed: %s", what, type(exc).__name__)
        if b and "isn't installed" in str(exc):
            return Reply(text=f"{browser.title()} doesn't seem to be installed.", success=False)
        return Reply(text="I couldn't open the browser.", success=False)
    return None


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

_NOT_YT = r"(?!(?:on |in )?youtube\b)(?!.*\b(?:on|in) youtube$)"
_ENGINE = r"(?:google|the web|the internet|online|the net)"


def _strip_search_padding(q: str) -> str:
    q = re.sub(r"^(?:search )?(?:for|about)(?: |$)", "", q)
    q = re.sub(r" (?:on|in|using|with) google$| online$| on the (?:web|internet)$", "", q)
    return q.strip()


@skill(
    name="google_search",
    tier=Tier.ACT,
    examples=[
        "google how tall is mount everest",
        "search for cheap flights to delhi",
        "look up python decorators",
        "search the web for pizza near me",
        "google search best laptops",
    ],
    description="Search Google in the browser. One parameter: query (str) -- what to search for.",
    trusts_input=False,
    patterns=[
        # "search google for X", "look up X on google", "search the web for X"
        rf"(?:search|look up|look|check) (?:on |in )?{_ENGINE} (?:for|about) (?P<query>.+)",
        rf"(?:do a )?google search (?:for )?(?P<query>.+)",
        # "google X" -- but "google calendar" alone is a site, not a search
        r"google (?!(?:calendar|drive|docs|maps|mail|search)$)(?P<query>.+)",
        # "search for X", "search X", "look up X", "find X online"
        rf"(?:search for|search about|search up|search|look up|lookup) (?!{_ENGINE} (?:for|about) ){_NOT_YT}(?P<query>.+)",
        rf"(?:find|look for) {_NOT_YT}(?P<query>.+?) (?:online|on google|on the (?:web|internet))",
    ],
)
async def google_search(ctx: SkillCtx, query: str, browser: str = "") -> Reply:
    """Open a Google search for `query` in the default browser."""
    q = _clean_query(_strip_search_padding(_clean_query(query)))
    if not q:
        return Reply(text="What should I search for?", success=False)
    url = "https://www.google.com/search?q=" + percent_encode(q)
    failed = await _open(url, "google_search", browser)
    if failed is not None:
        return failed
    logger.info("google_search: opened a Google search (%d chars)", len(q))
    return Reply(text="Here's what Google found.")


# ---------------------------------------------------------------------------
# youtube
# ---------------------------------------------------------------------------


@skill(
    name="youtube",
    tier=Tier.ACT,
    examples=[
        "search youtube for lofi beats",
        "play lofi beats on youtube",
        "youtube interstellar trailer",
        "find cat videos on youtube",
    ],
    description="Search YouTube in the browser. One parameter: query (str) -- what to look for.",
    trusts_input=False,
    patterns=[
        r"(?:search|look up|look|check|find) (?:on |in )?youtube (?:for |about )?(?P<query>.+)",
        r"(?:search for|search|look up|look for|find|play|watch|put on|show me|pull up) (?P<query>.+?) (?:on|in|from) youtube",
        r"youtube (?:search (?:for )?|play |find )?(?P<query>(?!(?:com|dot com|website|site|page)$).+)",
        r"open youtube and (?:search (?:for )?|play |find |look up )(?P<query>.+)",
    ],
)
async def youtube(ctx: SkillCtx, query: str, browser: str = "") -> Reply:
    """Open YouTube search results for `query`."""
    q = _clean_query(re.sub(r"^(?:search (?:for )?|for |play |videos? (?:of|about) )", "", _clean_query(query)))
    if not q:
        return Reply(text="What should I look up on YouTube?", success=False)
    url = "https://www.youtube.com/results?search_query=" + percent_encode(q)
    failed = await _open(url, "youtube", browser)
    if failed is not None:
        return failed
    logger.info("youtube: opened a YouTube search (%d chars)", len(q))
    return Reply(text="Here it is on YouTube.")


# ---------------------------------------------------------------------------
# open_website
# ---------------------------------------------------------------------------

#: spoken name -> (URL, spoken name). Keys are already in normalized form
#: (lower case, no punctuation). Deliberately NOT here: spotify, discord,
#: notepad... -- desktop apps belong to open_app.
SITES: dict[str, tuple[str, str]] = {
    "youtube": ("https://www.youtube.com", "YouTube"),
    "gmail": ("https://mail.google.com", "Gmail"),
    "google mail": ("https://mail.google.com", "Gmail"),
    "my email": ("https://mail.google.com", "Gmail"),
    "google": ("https://www.google.com", "Google"),
    "github": ("https://github.com", "GitHub"),
    "git hub": ("https://github.com", "GitHub"),
    "reddit": ("https://www.reddit.com", "Reddit"),
    "linkedin": ("https://www.linkedin.com", "LinkedIn"),
    "linked in": ("https://www.linkedin.com", "LinkedIn"),
    "chatgpt": ("https://chatgpt.com", "ChatGPT"),
    "chat gpt": ("https://chatgpt.com", "ChatGPT"),
    "claude": ("https://claude.ai", "Claude"),
    "claude ai": ("https://claude.ai", "Claude"),
    "netflix": ("https://www.netflix.com", "Netflix"),
    "amazon": ("https://www.amazon.com", "Amazon"),
    "twitter": ("https://x.com", "X"),
    "x": ("https://x.com", "X"),
    "instagram": ("https://www.instagram.com", "Instagram"),
    "insta": ("https://www.instagram.com", "Instagram"),
    "whatsapp web": ("https://web.whatsapp.com", "WhatsApp Web"),
    "google drive": ("https://drive.google.com", "Google Drive"),
    "my drive": ("https://drive.google.com", "Google Drive"),
    "google calendar": ("https://calendar.google.com", "Google Calendar"),
    "google docs": ("https://docs.google.com", "Google Docs"),
    "google maps": ("https://www.google.com/maps", "Google Maps"),
    "outlook": ("https://outlook.office.com/mail/", "Outlook"),
    "canvas": ("https://canvas.asu.edu", "Canvas"),
    "asu canvas": ("https://canvas.asu.edu", "Canvas"),
    "my asu": ("https://my.asu.edu", "My ASU"),
    "myasu": ("https://my.asu.edu", "My ASU"),
}

_SITE_ALT = "|".join(re.escape(k) for k in sorted(SITES, key=len, reverse=True))
# TLDs accepted when spoken without the word "dot" (the normalizer turns
# "example.com" into "example com"). Short English words (in, me, us, uk)
# only count after an explicit "dot", so "open file in" is never a domain --
# and neither is "open notepad app" or "open samsung tv".
_TLD_BARE = r"(?:com|org|net|io|edu|gov|dev|ai|gg)"
_TLD_DOT = r"(?:com|org|net|io|edu|gov|dev|ai|gg|tv|co|app|ly|in|me|us|uk|ca|de|fm|so|xyz|info)"
_LABEL = r"[a-z0-9][a-z0-9-]{0,62}"
_DOMAIN = rf"(?:{_LABEL}(?: dot | )){{0,3}}?(?:{_LABEL} dot {_TLD_DOT}|{_LABEL} {_TLD_BARE})"
_OPEN_VERB = r"(?:open(?: up)?|go to|goto|take me to|bring up|pull up|launch|visit|navigate to|load)"
_SITE_TAIL = r"(?: website| site| page| dot com)?(?: (?:in|on|with|using) (?:the |my )?(?P<browser>browser|brave|chrome|google chrome|edge|microsoft edge|firefox|opera|vivaldi)(?: browser)?)?"


def resolve_site(site: str) -> tuple[str, str] | None:
    """(https URL, spoken name) for a spoken site, or None. Known names
    first; otherwise a spoken domain rebuilt and run through
    winops.safe_https_url."""
    s = re.sub(r"\s+", " ", (site or "").lower()).strip()
    s = re.sub(r"^(?:the |my )", "", s) if s not in SITES else s
    s = re.sub(r" (?:website|site|page)$", "", s)
    if s in SITES:
        return SITES[s]
    s = re.sub(r"^(?:https?(?::|\s)\s*(?://)?\s*)?(?:www(?: dot | |\.))?", "", s)
    s = re.sub(r" dot ", ".", s)
    s = s.replace(" ", ".")
    if not re.fullmatch(r"[a-z0-9.-]{3,253}", s) or "." not in s:
        return None
    url = winops.safe_https_url(f"https://{s}")
    if url is None:
        return None
    return url, s


@skill(
    name="open_website",
    tier=Tier.ACT,
    examples=[
        "open github dot com",
        "go to youtube",
        "open reddit website",
        "open gmail in the browser",
        "open google calendar",
    ],
    description=(
        "Open a website in the browser: a known site (youtube, gmail, github, reddit, canvas, "
        "google calendar, ...) or a spoken domain like 'example.com'. One parameter: site (str)."
    ),
    trusts_input=False,
    patterns=[
        rf"{_OPEN_VERB} (?:the |my )?(?P<site>{_SITE_ALT}){_SITE_TAIL}",
        rf"{_OPEN_VERB} (?:the (?:website |site )?)?(?P<site>(?:www |www dot )?{_DOMAIN})(?: website| site)?(?: (?:in|on|with|using) (?:the |my )?(?P<browser>browser|brave|chrome|google chrome|edge|microsoft edge|firefox|opera|vivaldi)(?: browser)?)?",
    ],
)
async def open_website(ctx: SkillCtx, site: str, browser: str = "") -> Reply:
    """Open a known site or a spoken https domain in the default browser."""
    resolved = resolve_site(site)
    if resolved is None:
        return Reply(text="I don't know that website.", success=False)
    url, name = resolved
    failed = await _open(url, "open_website", browser)
    if failed is not None:
        return failed
    logger.info("open_website: opened %s", url)
    where = f" in {browser.title()}" if browser and browser.lower() not in ("browser", "the browser", "my browser") else ""
    return Reply(text=f"Opening {name}{where}.")
