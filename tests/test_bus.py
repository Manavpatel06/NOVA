"""Contract: docs/CONTRACTS.md T0.1 — nova.bus.EventBus.

    class EventBus:
        def subscribe(self, topic: str, handler: Callable[[Any], Awaitable[None]]) -> None
        async def publish(self, topic: str, payload: Any) -> None   # never raises into publisher

Rules under test:
  - publish() delivers to every subscriber of the topic and to no others;
  - a subscriber that raises never breaks the publisher, and never starves
    sibling subscribers (regardless of registration order);
  - publish() itself never raises, even when every subscriber raises.

pytest.ini sets asyncio_mode = auto, so plain `async def test_...` runs
under pytest-asyncio without an explicit marker.
"""
from __future__ import annotations

import pytest

from nova.bus import EventBus


async def test_publish_delivers_to_single_subscriber():
    bus = EventBus()
    received = []

    async def handler(payload):
        received.append(payload)

    bus.subscribe("topic.a", handler)
    await bus.publish("topic.a", {"x": 1})

    assert received == [{"x": 1}]


async def test_publish_delivers_to_multiple_subscribers_in_subscription_order():
    bus = EventBus()
    order = []

    async def first(payload):
        order.append(("first", payload))

    async def second(payload):
        order.append(("second", payload))

    bus.subscribe("topic.a", first)
    bus.subscribe("topic.a", second)
    await bus.publish("topic.a", "hello")

    assert order == [("first", "hello"), ("second", "hello")]


async def test_publish_does_not_deliver_to_other_topics():
    bus = EventBus()
    a_calls, b_calls = [], []

    async def on_a(payload):
        a_calls.append(payload)

    async def on_b(payload):
        b_calls.append(payload)

    bus.subscribe("topic.a", on_a)
    bus.subscribe("topic.b", on_b)
    await bus.publish("topic.a", "only-a")

    assert a_calls == ["only-a"]
    assert b_calls == []


async def test_publish_to_topic_with_no_subscribers_does_not_raise():
    bus = EventBus()
    await bus.publish("nobody.listens", {"anything": True})  # must not raise


async def test_subscriber_exception_does_not_propagate_to_publisher():
    bus = EventBus()

    async def exploder(payload):
        raise RuntimeError("boom")

    bus.subscribe("topic.a", exploder)
    await bus.publish("topic.a", None)  # must not raise


@pytest.mark.parametrize("bad_first", [True, False])
async def test_subscriber_exception_does_not_starve_other_subscribers(bad_first):
    """Order must not matter: a raising handler anywhere in the chain must
    not prevent its siblings (registered before OR after it) from running."""
    bus = EventBus()
    good_calls = []

    async def bad(payload):
        raise ValueError("nope")

    async def good(payload):
        good_calls.append(payload)

    if bad_first:
        bus.subscribe("topic.a", bad)
        bus.subscribe("topic.a", good)
    else:
        bus.subscribe("topic.a", good)
        bus.subscribe("topic.a", bad)

    await bus.publish("topic.a", "payload")

    assert good_calls == ["payload"]


async def test_publish_never_raises_even_if_every_subscriber_raises():
    bus = EventBus()

    async def bad1(payload):
        raise RuntimeError("first")

    async def bad2(payload):
        raise RuntimeError("second")

    bus.subscribe("topic.a", bad1)
    bus.subscribe("topic.a", bad2)
    await bus.publish("topic.a", None)  # must not raise


async def test_payload_identity_is_passed_through_unchanged():
    bus = EventBus()
    received = []
    payload = {"nested": ["object", 1, 2]}

    async def handler(p):
        received.append(p)

    bus.subscribe("topic.a", handler)
    await bus.publish("topic.a", payload)

    assert received[0] is payload


async def test_two_bus_instances_do_not_share_subscribers():
    bus1 = EventBus()
    bus2 = EventBus()
    calls = []

    async def handler(payload):
        calls.append(payload)

    bus1.subscribe("topic.a", handler)
    await bus2.publish("topic.a", "should-not-be-seen")

    assert calls == []


async def test_subscribing_same_handler_twice_invokes_it_twice():
    bus = EventBus()
    calls = []

    async def handler(payload):
        calls.append(payload)

    bus.subscribe("topic.a", handler)
    bus.subscribe("topic.a", handler)
    await bus.publish("topic.a", "x")

    assert calls == ["x", "x"]


async def test_subscribing_during_publish_does_not_affect_the_in_flight_publish():
    """A handler that subscribes a new handler mid-publish must not cause
    that new handler to be invoked as part of the *same* publish() call."""
    bus = EventBus()
    late_calls = []

    async def late_handler(payload):
        late_calls.append(payload)

    async def registrar(payload):
        bus.subscribe("topic.a", late_handler)

    bus.subscribe("topic.a", registrar)
    await bus.publish("topic.a", "first")
    assert late_calls == []  # not invoked during the publish that added it

    await bus.publish("topic.a", "second")
    assert late_calls == ["second"]  # but is invoked on the next publish
