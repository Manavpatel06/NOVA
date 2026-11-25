"""How much of Manav's monthly Claude API credit NOVA has used (ADR 0026).

Manav gets $100 of API credit a month and wants it used before the Max plan.
Every API reply carries its token counts; this turns them into dollars with
Anthropic's list prices and keeps a running total per calendar month
(data/api_spend.json). Under the budget, the hub's NOVA answers with the
API; at the budget -- or the moment Anthropic says the credit has run out --
it switches to the Max plan until the 1st of next month.

The total is an estimate from list prices (no billing API is consulted);
the budget leaves headroom below $100 for that reason.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("brain.spend")

#: $ per million tokens: input, 5-minute cache write, cache read, output
#: (platform.claude.com/docs/en/about-claude/pricing, 2026-11-25). Prompts
#: over 100k tokens cost 5x on Haiku 5.5; NOVA's are a few thousand.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-sonnet-5-5": (2.0, 2.50, 0.10, 10.0),
    "claude-opus-5-5": (4.0, 5.0, 0.20, 20.0),
}
#: only Haiku 5.5 is priced by prompt length (5x over 100k tokens)
LONG_PROMPT_MODELS = frozenset({"claude-haiku-5-5"})
#: an unknown model is charged as if it were expensive, never as free
UNKNOWN = (5.0, 6.25, 0.50, 25.0)
LONG_PROMPT = 100_000
WEB_SEARCH_EACH = 10.0 / 1000


def cost(model: str, usage: Any) -> float:
    if not isinstance(usage, dict):
        return 0.0

    def n(key: str) -> int:
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    p_in, p_write, p_read, p_out = PRICES.get(model, UNKNOWN)
    inp, write, read, out = (n("input_tokens"), n("cache_creation_input_tokens"),
                             n("cache_read_input_tokens"), n("output_tokens"))
    scale = 5.0 if model in LONG_PROMPT_MODELS and inp + write + read > LONG_PROMPT else 1.0
    dollars = scale * (inp * p_in + write * p_write + read * p_read + out * p_out) / 1_000_000
    server = usage.get("server_tool_use")
    if isinstance(server, dict):
        searches = server.get("web_search_requests")
        if isinstance(searches, (int, float)) and searches > 0:
            dollars += int(searches) * WEB_SEARCH_EACH
    return dollars


class Spend:
    def __init__(self, path: Path, *, budget_usd: float, now: Callable[[], datetime] = datetime.now) -> None:
        self._path = Path(path)
        self.budget = float(budget_usd)
        self._now = now
        self._lock = threading.Lock()

    def _month(self) -> str:
        return self._now().strftime("%Y-%m")

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("month") != self._month():
            data = {"month": self._month(), "usd": 0.0, "calls": 0, "exhausted": False}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self._path)

    def record(self, model: str, usage: Any) -> float:
        dollars = cost(model, usage)
        with self._lock:
            data = self._load()
            data["usd"] = round(float(data.get("usd", 0.0)) + dollars, 6)
            data["calls"] = int(data.get("calls", 0)) + 1
            self._save(data)
        return dollars

    def mark_exhausted(self) -> None:
        """Anthropic said the credit is gone: Max plan until next month."""
        with self._lock:
            data = self._load()
            if not data.get("exhausted"):
                _log.warning("API credit used up for %s; NOVA uses the Max plan until next month", data["month"])
            data["exhausted"] = True
            self._save(data)

    def allowed(self) -> bool:
        with self._lock:
            data = self._load()
        return not data.get("exhausted") and float(data.get("usd", 0.0)) < self.budget

    def status(self) -> dict[str, Any]:
        with self._lock:
            data = self._load()
        return {"month": data["month"], "usd": round(float(data.get("usd", 0.0)), 2), "budget_usd": self.budget,
                "calls": int(data.get("calls", 0)), "exhausted": bool(data.get("exhausted"))}
