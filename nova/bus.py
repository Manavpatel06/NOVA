"""In-process publish/subscribe event bus.

A failing subscriber is logged and never breaks the publisher or any other
subscriber (docs/CONTRACTS.md T0.1).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Awaitable, Callable

from nova.log import get_logger

logger = get_logger("bus")

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    """Minimal async pub/sub. Topics are plain strings; there is no wildcard
    matching or topic hierarchy in M0."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Register an async handler for `topic`. Multiple handlers per topic
        are allowed and are invoked in subscription order."""
        self._subscribers[topic].append(handler)

    async def publish(self, topic: str, payload: Any) -> None:
        """Invoke every handler subscribed to `topic` with `payload`.

        Never raises: a handler that throws is logged (with traceback) and
        the remaining handlers still run.
        """
        # Snapshot so a handler that subscribes/unsubscribes mid-publish
        # can't mutate the list we're iterating.
        for handler in list(self._subscribers.get(topic, ())):
            try:
                await handler(payload)
            except Exception:
                logger.exception(
                    "subscriber %r for topic %r raised; continuing",
                    getattr(handler, "__qualname__", handler),
                    topic,
                )
