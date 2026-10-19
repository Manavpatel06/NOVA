"""weather (nova/skills/weather.py) -- READ tier, Open-Meteo through Egress.

No real network: a fake egress returns canned Open-Meteo JSON and records
every URL, so the tests prove exactly what would leave the machine."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

import nova.skills  # noqa: F401
from nova.brain.router import Router
from nova.config import Config, ConfigError, WeatherConfig, load_config
from nova.security.audit import AuditLog
from nova.security.egress import Egress, EgressError
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills.registry import SkillCtx, get_skill
from nova.skills.weather import describe


def _forecast(now=84.2, code=0, is_day=1, hi=(91.4, 93.0), lo=(70.1, 72.4), pop=(5, 70), dcode=(0, 1)):
    return {
        "current": {"time": "2026-10-15T14:00", "temperature_2m": now, "weather_code": code, "is_day": is_day},
        "daily": {
            "time": ["2026-10-15", "2026-10-16"],
            "weather_code": list(dcode),
            "temperature_2m_max": list(hi),
            "temperature_2m_min": list(lo),
            "precipitation_probability_max": list(pop),
        },
    }


class FakeEgress:
    def __init__(self, forecast=None, geo=None, raises=None):
        self.urls: list[str] = []
        self.timeouts: list[float] = []
        self.forecast = forecast if forecast is not None else _forecast()
        self.geo = geo
        self.raises = raises

    def fetch(self, url, timeout=10.0):
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self.raises:
            raise self.raises
        if url.startswith("https://geocoding-api.open-meteo.com/"):
            return json.dumps(self.geo).encode()
        assert url.startswith("https://api.open-meteo.com/v1/forecast?")
        return json.dumps(self.forecast).encode()


@pytest.fixture
def ladder(tmp_path):
    return Ladder(AuditLog(path=tmp_path / "audit.log"))


def _ctx(ladder, egress, config=None):
    return SkillCtx(config=config or Config(), ladder=ladder, egress=egress)


@pytest.fixture(scope="module")
def router():
    return Router()


# -- config -------------------------------------------------------------------


def test_default_location_is_tempe_fahrenheit():
    w = Config().weather
    assert (w.location_name, w.latitude, w.longitude, w.timezone, w.temperature_unit) == (
        "Tempe", 33.4255, -111.94, "America/Phoenix", "fahrenheit")


def test_shipped_yaml_allowlists_open_meteo_and_sets_tempe():
    cfg = load_config()
    assert "api.open-meteo.com" in cfg.security.egress_allowlist
    assert "geocoding-api.open-meteo.com" in cfg.security.egress_allowlist
    assert cfg.weather.location_name == "Tempe"


def test_latitude_out_of_range_is_a_config_error(tmp_path):
    p = tmp_path / "nova.yaml"
    p.write_text("weather:\n  latitude: 123.0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p, learned_apps_path=tmp_path / "none.json", audit_log_path=tmp_path / "a.log")


def test_unknown_weather_key_is_a_config_error(tmp_path):
    p = tmp_path / "nova.yaml"
    p.write_text("weather:\n  city: Tempe\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p, learned_apps_path=tmp_path / "none.json", audit_log_path=tmp_path / "a.log")


# -- routing --------------------------------------------------------------------


@pytest.mark.parametrize("text, params", [
    ("what's the weather", {}),
    ("what's the weather like", {}),
    ("how's the weather", {}),
    ("weather", {}),
    ("weather tomorrow", {"when": "tomorrow"}),
    ("what's the weather tonight", {"when": "tonight"}),
    ("what's the weather like tomorrow", {"when": "tomorrow"}),
    ("is it going to rain today", {"rain": "rain", "when": "today"}),
    ("will it rain tomorrow", {"rain": "rain", "when": "tomorrow"}),
    ("is it raining", {"rain": "rain"}),
    ("do i need an umbrella", {"rain": "umbrella"}),
    ("chance of rain tomorrow", {"rain": "rain", "when": "tomorrow"}),
    ("weather in Mumbai", {"place": "mumbai"}),
    ("what's the weather in mumbai tomorrow", {"place": "mumbai", "when": "tomorrow"}),
    ("what's the weather like tomorrow in new york", {"when": "tomorrow", "place": "new york"}),
    ("how hot is it outside", {}),
    ("what's the temperature", {}),
    ("what's the forecast", {}),
    ("show me the weather", {}),
    ("check the weather for tomorrow", {"when": "tomorrow"}),
])
def test_weather_routes(router, text, params):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "weather", m.clarification
    assert m.params == params


@pytest.mark.parametrize("text, other", [("what's the time", "time"), ("what's the date", "date"),
                                         ("what time is it", "time")])
def test_weather_does_not_steal_time_or_date(router, text, other):
    assert router.route(text).spec.name == other


def test_weather_is_read_tier():
    assert get_skill("weather").tier is Tier.READ


def test_describe_wmo_codes():
    assert describe(0) == "sunny"
    assert describe(0, is_day=False) == "clear"
    assert describe(2) == "partly cloudy"
    assert describe(63) == "rainy"
    assert describe(95) == "stormy"
    assert describe(1234) == "hard to call"
    assert describe(None) == "hard to call"


# -- replies ----------------------------------------------------------------------


async def test_current_weather_reply_and_request(ladder):
    eg = FakeEgress()
    reply = await get_skill("weather").invoke(_ctx(ladder, eg))
    assert reply.success
    assert reply.text == "It's 84 and sunny in Tempe, high of 91 later."
    (url,) = eg.urls
    assert "latitude=33.4255" in url and "longitude=-111.9400" in url
    assert "temperature_unit=fahrenheit" in url
    assert "timezone=America%2FPhoenix" in url
    assert eg.timeouts[0] <= 10


async def test_current_at_the_days_high(ladder):
    eg = FakeEgress(forecast=_forecast(now=91.0, hi=(91.4, 93)))
    reply = await get_skill("weather").invoke(_ctx(ladder, eg))
    assert reply.text == "It's 91 and sunny in Tempe, right around today's high."


async def test_tomorrow(ladder):
    reply = await get_skill("weather").invoke(_ctx(ladder, FakeEgress()), when="tomorrow")
    assert reply.text == "Tomorrow in Tempe: mostly sunny, high of 93, low of 72."


async def test_tonight_uses_night_words(ladder):
    reply = await get_skill("weather").invoke(_ctx(ladder, FakeEgress()), when="tonight")
    assert reply.text == "Tonight in Tempe: clear, down to 70."


@pytest.mark.parametrize("pop, start", [(5, "Doesn't look like it"), (40, "Maybe."), (80, "Yes")])
async def test_rain_question(ladder, pop, start):
    eg = FakeEgress(forecast=_forecast(pop=(pop, pop)))
    reply = await get_skill("weather").invoke(_ctx(ladder, eg), rain="rain", when="today")
    assert reply.text.startswith(start) and f"{pop}% chance of rain today" in reply.text


async def test_rain_tomorrow_reads_tomorrows_probability(ladder):
    reply = await get_skill("weather").invoke(_ctx(ladder, FakeEgress()), rain="umbrella", when="tomorrow")
    assert reply.text == "Yes, looks like it -- 70% chance of rain tomorrow."


async def test_celsius_config(ladder):
    cfg = replace(Config(), weather=replace(WeatherConfig(), temperature_unit="celsius"))
    eg = FakeEgress(forecast=_forecast(now=29.0, hi=(33, 34)))
    reply = await get_skill("weather").invoke(_ctx(ladder, eg, cfg))
    assert "temperature_unit=celsius" in eg.urls[0]
    assert reply.text == "It's 29 Celsius and sunny in Tempe, high of 33 later."


async def test_bad_unit_is_said_not_guessed(ladder):
    cfg = replace(Config(), weather=replace(WeatherConfig(), temperature_unit="kelvin"))
    eg = FakeEgress()
    reply = await get_skill("weather").invoke(_ctx(ladder, eg, cfg))
    assert not reply.success and eg.urls == []


async def test_other_city_geocodes_then_forecasts(ladder):
    geo = {"results": [{"name": "Mumbai", "latitude": 19.07283, "longitude": 72.88261, "timezone": "Asia/Kolkata"}]}
    eg = FakeEgress(forecast=_forecast(now=88, code=2, hi=(89, 90)), geo=geo)
    reply = await get_skill("weather").invoke(_ctx(ladder, eg), place="mumbai")
    assert reply.text == "It's 88 and partly cloudy in Mumbai, right around today's high."
    assert eg.urls[0].startswith("https://geocoding-api.open-meteo.com/v1/search?name=mumbai&count=1")
    assert "latitude=19.0728" in eg.urls[1] and "timezone=Asia%2FKolkata" in eg.urls[1]


async def test_place_with_trailing_when_word_is_split(ladder):
    geo = {"results": [{"name": "Denver", "latitude": 39.7, "longitude": -105.0, "timezone": "America/Denver"}]}
    eg = FakeEgress(geo=geo)
    reply = await get_skill("weather").invoke(_ctx(ladder, eg), place="denver right")
    assert "name=denver&" in eg.urls[0] and "Denver" in reply.text
    eg2 = FakeEgress()
    reply2 = await get_skill("weather").invoke(_ctx(ladder, eg2), place="tomorrow")
    assert len(eg2.urls) == 1 and reply2.text.startswith("Tomorrow in Tempe")


async def test_place_name_is_encoded_and_cannot_add_parameters(ladder):
    eg = FakeEgress(geo={"results": []})
    reply = await get_skill("weather").invoke(_ctx(ladder, eg), place="new york")
    assert "name=new+york&count=1" in eg.urls[0]
    assert not reply.success and "New York" in reply.text
    eg2 = FakeEgress(geo={"results": []})
    bad = await get_skill("weather").invoke(_ctx(ladder, eg2), place="x&latitude=1")
    assert not bad.success and eg2.urls == []


async def test_hostile_geocoder_name_is_filtered_before_speaking(ladder):
    geo = {"results": [{"name": "<b>Evil</b>](http://x)" + "A" * 100, "latitude": 1, "longitude": 1}]}
    eg = FakeEgress(geo=geo)
    reply = await get_skill("weather").invoke(_ctx(ladder, eg), place="evil")
    assert "<" not in reply.text and "(" not in reply.text and "http" not in reply.text.split(" in ")[0]


async def test_no_egress_is_an_honest_refusal(ladder):
    reply = await get_skill("weather").invoke(_ctx(ladder, None))
    assert not reply.success and "weather" in reply.text


@pytest.mark.parametrize("exc", [EgressError("not allowlisted"), TimeoutError("slow"), OSError("down")])
async def test_network_failures_are_spoken(ladder, exc):
    reply = await get_skill("weather").invoke(_ctx(ladder, FakeEgress(raises=exc)))
    assert not reply.success and reply.text == "I couldn't reach the weather service."


async def test_garbage_body_is_spoken_not_raised(ladder):
    class Junk(FakeEgress):
        def fetch(self, url, timeout=10.0):
            return b"<html>not json"
    reply = await get_skill("weather").invoke(_ctx(ladder, Junk()))
    assert not reply.success


async def test_missing_fields_are_spoken_not_raised(ladder):
    reply = await get_skill("weather").invoke(_ctx(ladder, FakeEgress(forecast={"current": {}, "daily": {}})))
    assert not reply.success


async def test_through_the_real_egress_with_a_fake_transport(ladder, tmp_path, monkeypatch):
    """The real Egress (allowlist + audit) in front of a fake urlopen."""
    import nova.security.egress as egress_mod

    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        seen.append((url, timeout))
        return Resp(json.dumps(_forecast()).encode())

    monkeypatch.setattr(egress_mod.urllib.request, "urlopen", fake_urlopen)
    eg = Egress(["api.open-meteo.com"], audit=AuditLog(path=tmp_path / "egress.log"))
    reply = await get_skill("weather").invoke(_ctx(ladder, eg))
    assert reply.success and seen and seen[0][1] == 8.0

    denied = Egress([], audit=AuditLog(path=tmp_path / "egress2.log"))
    seen.clear()
    reply2 = await get_skill("weather").invoke(_ctx(ladder, denied))
    assert not reply2.success and seen == []
