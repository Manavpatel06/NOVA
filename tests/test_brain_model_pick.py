"""The right model for each task (2026-11-25): hard questions -> the stronger
model, commands and small talk -> the fast one; both priced in the budget."""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from nova.brain.agent import needs_thinking
from nova.brain.spend import cost
from nova.config import BrainConfig, Config
from tests.test_brain_agent import make_agent, recorder, say


@pytest.mark.parametrize("text", [
    "explain how attention works in transformers", "why is the sky blue",
    "should I take CSE 575 or 576 next semester", "compare python and rust for my project",
    "write me an email to my professor asking for an extension", "think hard about this one",
    "help me plan my week", "what are the pros and cons of living on campus",
])
def test_questions_that_need_thinking(text):
    assert needs_thinking(text)


@pytest.mark.parametrize("text", [
    "open spotify", "what time is it", "set a timer for ten minutes", "turn the volume up",
    "tell me a joke", "what's the weather", "play some lofi", "remind me to call mom at 6",
])
def test_commands_and_small_talk_stay_fast(text):
    assert not needs_thinking(text)


def _models(texts, **brain):
    a, eg, t = make_agent([say("ok") for _ in texts], **brain)
    invoke, _ = recorder()
    used = []
    for i, text in enumerate(texts):
        t["now"] = i * 30.0
        asyncio.run(a.respond(text, invoke))
        used.append(eg.bodies[-1]["model"])
    return used


def test_the_request_uses_the_picked_model_and_a_short_follow_up_stays():
    used = _models(["open spotify", "explain how vaccines train the immune system", "and what about mRNA ones",
                    "what time is it"])
    assert used == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5"]


def test_the_follow_up_window_ends():
    a, eg, t = make_agent([say("ok"), say("ok")])
    invoke, _ = recorder()
    asyncio.run(a.respond("explain black holes", invoke))
    t["now"] = 500.0
    asyncio.run(a.respond("open notes", invoke))
    assert [b["model"] for b in eg.bodies] == ["claude-sonnet-5-5", "claude-haiku-5-5"]


def test_no_hard_model_means_always_the_fast_one():
    assert _models(["explain black holes"], hard_model="") == ["claude-haiku-5-5"]


def test_the_stronger_model_is_priced_honestly():
    usage = {"input_tokens": 50_000, "output_tokens": 50_000}
    assert cost("claude-sonnet-5-5", usage) == pytest.approx(0.60)    # $2 in, $10 out per MTok
    assert cost("claude-haiku-5-5", usage) == pytest.approx(0.03)     # 20x cheaper
    big = {"input_tokens": 200_000}
    assert cost("claude-sonnet-5-5", big) == pytest.approx(0.40)   # no long-prompt surcharge
    assert cost("claude-haiku-5-5", big) == pytest.approx(0.10)    # 5x over 100k


def test_spend_records_the_model_actually_used():
    a, eg, t = make_agent([say("ok")])

    class _S:
        def __init__(self):
            self.rec = []

        def allowed(self):
            return True

        def record(self, model, usage):
            self.rec.append(model)

    a._spend = _S()
    invoke, _ = recorder()
    asyncio.run(a.respond("explain black holes", invoke))
    assert a._spend.rec == ["claude-sonnet-5-5"]
