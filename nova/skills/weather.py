"""weather -- READ tier: current conditions and a 2-day forecast (2026-10-15).

Data: Open-Meteo (https://open-meteo.com) -- free, no API key, no account.
Every request goes through `ctx.egress` (nova.security.egress.Egress, the
one module allowed to touch the network), so both hosts must be on
`security.egress_allowlist` in config/nova.yaml:

  api.open-meteo.com            the forecast
  geocoding-api.open-meteo.com  only for "weather in <city>"

The default place is `config.weather` (Tempe, AZ). What leaves the machine:
that place's coordinates, or the city name Manav said -- nothing else.

Replies are written for the ear:
  "It's 84 and sunny in Tempe, high of 91 later."
  "Tomorrow in Tempe: mostly sunny, high of 93, low of 72."
  "Doesn't look like it, only a 5% chance of rain today."

The response body is weather data from the internet. It is only ever read
for numbers (temperatures, a WMO code, a percentage) -- no text from it is
spoken except the geocoder's place name, which is length-capped and
character-filtered first, and nothing from it can select or parameterise an
action.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.skills.web import percent_encode

logger = get_logger("skills.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_TIMEOUT_S = 8.0

# WMO weather interpretation codes (Open-Meteo docs) -> (day word, night word)
_WMO: dict[int, tuple[str, str]] = {
    0: ("sunny", "clear"),
    1: ("mostly sunny", "mostly clear"),
    2: ("partly cloudy", "partly cloudy"),
    3: ("cloudy", "cloudy"),
    45: ("foggy", "foggy"),
    48: ("foggy", "foggy"),
    51: ("drizzly", "drizzly"),
    53: ("drizzly", "drizzly"),
    55: ("drizzly", "drizzly"),
    56: ("icy with freezing drizzle", "icy with freezing drizzle"),
    57: ("icy with freezing drizzle", "icy with freezing drizzle"),
    61: ("rainy", "rainy"),
    63: ("rainy", "rainy"),
    65: ("pouring", "pouring"),
    66: ("icy with freezing rain", "icy with freezing rain"),
    67: ("icy with freezing rain", "icy with freezing rain"),
    71: ("snowy", "snowy"),
    73: ("snowy", "snowy"),
    75: ("snowing heavily", "snowing heavily"),
    77: ("snowy", "snowy"),
    80: ("showery", "showery"),
    81: ("showery", "showery"),
    82: ("stormy with heavy showers", "stormy with heavy showers"),
    85: ("snowy", "snowy"),
    86: ("snowing heavily", "snowing heavily"),
    95: ("stormy", "stormy"),
    96: ("stormy with hail", "stormy with hail"),
    99: ("stormy with hail", "stormy with hail"),
}


def describe(code: Any, *, is_day: bool = True) -> str:
    """A WMO weather code as a spoken word ('sunny', 'partly cloudy', ...)."""
    try:
        words = _WMO.get(int(code))
    except (TypeError, ValueError):
        words = None
    if words is None:
        return "hard to call"
    return words[0] if is_day else words[1]


_WHEN = r"(?P<when>today|tonight|tomorrow|right now|now|outside|this morning|this afternoon|this evening|later|later today)"
_PLACE = r"(?P<place>(?!(?:today|tonight|tomorrow|now|right now)$)[a-z][a-z' -]{1,40}?)"
_IN = rf"(?: (?:in|for|at) {_PLACE})"
_Q = r"(?:what's|what is|whats|how's|how is|hows|tell me|give me|show me|get me|check)"

_PATTERNS = [
    rf"{_Q} (?:the )?weather(?: like)?(?: going to be| gonna be)?{_IN}?(?: (?:for )?{_WHEN})?",
    rf"{_Q} (?:the )?weather(?: like)?(?: going to be| gonna be)?(?: {_WHEN}){_IN}",
    rf"(?:the )?weather(?: forecast| report| update)?{_IN}?(?: (?:for )?{_WHEN})?",
    rf"(?:the )?weather(?: forecast| report| update)?(?: {_WHEN}){_IN}",
    rf"(?:{_Q} )?(?:the )?(?:forecast|temperature|temp)(?: like)?{_IN}?(?: (?:for )?{_WHEN})?",
    rf"(?:what's|what is|whats) it like outside{_IN}?",
    rf"how (?:hot|cold|warm|chilly) is it(?: outside)?{_IN}?(?: {_WHEN})?",
    rf"how (?:hot|cold|warm) (?:is it going to|is it gonna|will it) (?:be|get)(?: {_WHEN})?{_IN}?",
    rf"(?:will|is) it (?P<rain>rain|snow)(?:ing)?{_IN}?(?: {_WHEN})?",
    rf"(?:is it going to|is it gonna|is it|it's going to|does it look like it's going to) (?P<rain>rain|snow|storm|pour)(?:ing)?{_IN}?(?: {_WHEN})?",
    rf"(?:do i|will i|should i) (?:need|take|bring|grab) (?:an |my |a )?(?P<rain>umbrella|raincoat)(?: {_WHEN})?",
    rf"(?:any |is there (?:a |any )?)?(?:chance of|chances of) (?P<rain>rain|snow)(?: {_WHEN})?{_IN}?",
]

_WHEN_WORDS = {"today", "tonight", "tomorrow", "now", "right now", "outside", "this morning",
               "this afternoon", "this evening", "later", "later today"}


def _unit_symbol(unit: str) -> str:
    return "" if unit == "fahrenheit" else " Celsius"


def _forecast_url(lat: float, lon: float, tz: str, unit: str) -> str:
    return (
        f"{FORECAST_URL}?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,weather_code,is_day"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&temperature_unit={unit}&timezone={percent_encode(tz)}&forecast_days=2"
    )


def _clean_place(place: str) -> str | None:
    p = re.sub(r"\s+", " ", (place or "").lower()).strip()
    p = re.sub(r"^(?:the )", "", p)
    if not re.fullmatch(r"[a-z][a-z' -]{1,40}", p):
        return None
    return p


async def _fetch_json(ctx: SkillCtx, url: str) -> Any:
    body = await asyncio.to_thread(ctx.egress.fetch, url, timeout=_TIMEOUT_S)
    return json.loads(body.decode("utf-8"))


def _num(seq: Any, i: int) -> float | None:
    try:
        v = seq[i]
        return float(v) if v is not None else None
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _deg(x: float | None) -> str:
    return "?" if x is None else str(round(x))


@skill(
    name="weather",
    tier=Tier.READ,
    examples=[
        "what's the weather",
        "weather tomorrow",
        "is it going to rain today",
        "what's the weather in mumbai",
        "how hot is it outside",
    ],
    description=(
        "Current weather and today/tomorrow forecast (default: Manav's home, Tempe AZ). "
        "Optional: when (today/tonight/tomorrow), place (a city name), rain (rain/snow question)."
    ),
    trusts_input=False,
    untrusted_output=True,
    patterns=_PATTERNS,
)
async def weather(ctx: SkillCtx, when: str = "today", place: str = "", rain: str = "") -> Reply:
    """Speak the weather for `place` (default config.weather) `when`."""
    cfg = ctx.config.weather
    unit = cfg.temperature_unit.strip().lower()
    if unit not in ("fahrenheit", "celsius"):
        return Reply(text="My weather units are set wrong -- they should be fahrenheit or celsius.", success=False)
    if ctx.egress is None:
        return Reply(text="I can't check the weather right now.", success=False)

    when = (when or "today").strip().lower()
    place = (place or "").strip().lower()
    # "forecast for tomorrow" can land "tomorrow" in the place slot, and the
    # router's normalizer drops a trailing "now" ("... in denver right now"
    # -> place "denver right"); put those words back where they belong.
    m = re.fullmatch(r"(?:(?P<p>.*?) )?(?P<w>today|tonight|tomorrow|right|now|this morning|this afternoon|this evening|later)", place)
    if m:
        place = m.group("p") or ""
        if m.group("w") in ("today", "tonight", "tomorrow"):
            when = m.group("w")
    if when not in _WHEN_WORDS:
        when = "today"
    day = 1 if when == "tomorrow" else 0

    lat, lon, tz, name = cfg.latitude, cfg.longitude, cfg.timezone, cfg.location_name
    try:
        if place:
            cleaned = _clean_place(place)
            if cleaned is None:
                return Reply(text="I didn't catch which place you meant.", success=False)
            geo = await _fetch_json(ctx, f"{GEOCODE_URL}?name={percent_encode(cleaned)}&count=1&language=en&format=json")
            results = geo.get("results") if isinstance(geo, dict) else None
            if not results:
                return Reply(text=f"I couldn't find {cleaned.title()}.", success=False)
            top = results[0]
            lat, lon = float(top["latitude"]), float(top["longitude"])
            tz = str(top.get("timezone") or "auto")
            raw_name = str(top.get("name") or cleaned.title())
            name = re.sub(r"[^\w' .-]", "", raw_name)[:40] or cleaned.title()
        data = await _fetch_json(ctx, _forecast_url(lat, lon, tz, unit))
    except Exception as exc:  # noqa: BLE001 -- any network/parse failure is one honest spoken refusal
        logger.error("weather fetch failed: %s", type(exc).__name__)
        return Reply(text="I couldn't reach the weather service.", success=False)

    try:
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        hi = _num(daily.get("temperature_2m_max"), day)
        lo = _num(daily.get("temperature_2m_min"), day)
        pop = _num(daily.get("precipitation_probability_max"), day)
        code = (daily.get("weather_code") or [None, None])[day]
        now_t = _num([current.get("temperature_2m")], 0)
        now_code = current.get("weather_code")
        is_day = bool(current.get("is_day", 1))
    except (AttributeError, TypeError) as exc:
        logger.error("weather response had an unexpected shape: %s", type(exc).__name__)
        return Reply(text="The weather service sent something I couldn't read.", success=False)

    u = _unit_symbol(unit)
    day_word = "tomorrow" if day else ("tonight" if when == "tonight" else "today")

    if rain:
        if pop is None:
            return Reply(text=f"I can't tell the chance of rain {day_word}.", success=False)
        what = "snow" if rain == "snow" else "rain"
        p = round(pop)
        where = f" in {name}" if place else ""
        if p >= 60:
            return Reply(text=f"Yes, looks like it -- {p}% chance of {what}{where} {day_word}.")
        if p >= 30:
            return Reply(text=f"Maybe. There's a {p}% chance of {what}{where} {day_word}.")
        return Reply(text=f"Doesn't look like it, only a {p}% chance of {what}{where} {day_word}.")

    if day == 1:
        if hi is None:
            return Reply(text="I couldn't get tomorrow's forecast.", success=False)
        return Reply(text=f"Tomorrow in {name}: {describe(code)}, high of {_deg(hi)}{u}, low of {_deg(lo)}.")

    if when == "tonight":
        return Reply(text=f"Tonight in {name}: {describe(now_code, is_day=False)}, down to {_deg(lo)}{u}.")

    if now_t is None:
        return Reply(text="I couldn't get the current weather.", success=False)
    text = f"It's {_deg(now_t)}{u} and {describe(now_code, is_day=is_day)} in {name}"
    if hi is not None and hi - now_t >= 2:
        text += f", high of {_deg(hi)} later."
    elif hi is not None:
        text += ", right around today's high."
    else:
        text += "."
    return Reply(text=text)
