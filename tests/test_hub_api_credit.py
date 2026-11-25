"""ADR 0026: the hub's NOVA thinks with Manav's monthly API credit first,
then the Max plan."""
from __future__ import annotations

import dataclasses
import os
import stat
from dataclasses import dataclass
from datetime import datetime

import pytest

from nova.brain.agent import Agent
from nova.brain.spend import Spend, cost
from nova.config import Config
from nova.hub.assistant import FirstAvailable


# --- the money -------------------------------------------------------------------

def test_haiku_turn_cost_from_list_prices():
    usage = {"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 6000,
             "output_tokens": 200}
    # 1000*0.10 + 6000*0.01 + 200*0.50 per million = 0.00026
    assert cost("claude-haiku-5-5", usage) == pytest.approx(0.00026)


def test_web_searches_and_unknown_models_are_never_free():
    assert cost("claude-haiku-5-5", {"server_tool_use": {"web_search_requests": 2}}) == pytest.approx(0.02)
    assert cost("some-future-model", {"output_tokens": 1_000_000}) == pytest.approx(25.0)
    assert cost("claude-haiku-5-5", {"input_tokens": -5, "output_tokens": "x"}) == 0.0
    assert cost("claude-haiku-5-5", None) == 0.0


def test_long_prompts_cost_more():
    assert cost("claude-haiku-5-5", {"input_tokens": 200_000}) == pytest.approx(0.10)  # 5x of 0.02


def test_the_budget_is_per_month_and_resets(tmp_path):
    now = [datetime(2026, 11, 25)]
    s = Spend(tmp_path / "s.json", budget_usd=1.0, now=lambda: now[0])
    assert s.allowed()
    s.record("claude-haiku-5-5", {"output_tokens": 2_000_000})   # $1.00
    assert not s.allowed() and s.status()["usd"] == 1.0
    now[0] = datetime(2026, 12, 1)
    assert s.allowed() and s.status()["usd"] == 0.0


def test_used_up_credit_switches_off_until_next_month(tmp_path):
    now = [datetime(2026, 11, 25)]
    s = Spend(tmp_path / "s.json", budget_usd=90, now=lambda: now[0])
    s.mark_exhausted()
    assert not s.allowed() and s.status()["exhausted"]
    now[0] = datetime(2026, 12, 1)
    assert s.allowed()


# --- the API brain counts its spend --------------------------------------------------

class _Egress:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def post_json(self, url, body, *, headers, timeout=20.0, method="POST"):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _api(tmp_path, egress, spend):
    cfg = dataclasses.replace(Config(), brain=dataclasses.replace(Config().brain, mode="api", web_search=False))
    return Agent(cfg, egress, key_provider=lambda: "sk-ant-test-key-xxxxxxxxxxxxxxxxxxxx",
                 skills_provider=lambda: {}, spend=spend)


async def test_each_reply_is_counted(tmp_path):
    spend = Spend(tmp_path / "s.json", budget_usd=90)
    reply = {"content": [{"type": "text", "text": "Hi."}], "stop_reason": "end_turn",
             "usage": {"input_tokens": 100, "output_tokens": 10}}
    out = await _api(tmp_path, _Egress([reply]), spend).respond("hello", None)
    assert out.text == "Hi." and spend.status()["calls"] == 1 and spend.status()["usd"] >= 0


async def test_an_out_of_credit_error_hands_over_to_max(tmp_path):
    from nova.security.egress import EgressError

    spend = Spend(tmp_path / "s.json", budget_usd=90)
    err = EgressError("HTTP 400 from api.anthropic.com: Your credit balance is too low to access the Anthropic API.")
    err.status = 400
    api = _api(tmp_path, _Egress([err]), spend)
    assert api.available()
    assert await api.respond("hello", None) is None       # nothing happened: the next brain may answer
    assert not api.available() and spend.status()["exhausted"]


async def test_over_budget_the_api_brain_steps_aside(tmp_path):
    spend = Spend(tmp_path / "s.json", budget_usd=0.0)
    assert not _api(tmp_path, _Egress([]), spend).available()


# --- best brain first -------------------------------------------------------------------

@dataclass
class _R:
    text: str


class _B:
    def __init__(self, name, available=True, answer=True):
        self.name, self._available, self._answer = name, available, answer
        self.asked, self.noted = [], []

    def available(self):
        return self._available

    def note(self, said, reply):
        self.noted.append(said)

    async def respond(self, text, invoke_tool):
        self.asked.append(text)
        return _R(self.name) if self._answer else None


async def test_api_first_then_max():
    api, mx = _B("api"), _B("max")
    assert (await FirstAvailable([("api", api), ("max", mx)]).respond("hi", None)).text == "api"
    assert mx.asked == []


async def test_max_answers_when_the_api_cant():
    for api in (_B("api", available=False), _B("api", answer=False)):
        mx = _B("max")
        assert (await FirstAvailable([("api", api), ("max", mx)]).respond("hi", None)).text == "max"


async def test_instant_commands_are_noted_to_every_brain():
    api, mx = _B("api"), _B("max")
    FirstAvailable([("api", api), ("max", mx)]).note("mute", "Muted.")
    assert api.noted == ["mute"] and mx.noted == ["mute"]


def test_the_hub_builds_api_first_by_default(tmp_path):
    from nova.hub.assistant import HubAssistant
    from nova.security.audit import AuditLog

    a = HubAssistant(Config(), audit=AuditLog(tmp_path / "a.log"), egress=None, journal=False)
    assert isinstance(a.brain, FirstAvailable) and [n for n, _ in a.brain.brains] == ["api", "max"]
    assert a.spend is not None


# --- the key file on the hub ------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="the hub is Linux; Windows uses Credential Manager")
def test_key_file_is_private_and_a_loose_one_is_ignored(tmp_path, monkeypatch):
    import nova
    from nova.brain import keys

    monkeypatch.setattr(nova, "DATA_DIR", tmp_path)
    keys.set_file_key("sk-ant-api03-" + "x" * 40)
    assert stat.S_IMODE((tmp_path / "anthropic_key").stat().st_mode) == 0o600
    assert keys._file_key().startswith("sk-ant-")
    os.chmod(tmp_path / "anthropic_key", 0o644)
    assert keys._file_key() is None
    with pytest.raises(ValueError):
        keys.set_file_key("not a key")
