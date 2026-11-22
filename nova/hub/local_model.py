"""The hub's local model (2026-11-22, docs/decisions/0019).

A small model in Ollama on nova-hub (default qwen3.5:2b, CPU) acts as NOVA's
fast lane in front of Claude: for one sentence it decides "tool" (a single,
complete command one listed tool does), "chat" (small talk), or "escalate"
(everything else goes to Claude on the Max plan). It never runs anything
itself -- it only returns a decision, and the laptop re-validates it before
anything happens (nova.brain.hub_tier).

Ollama is reached only through Egress.local_json (127.0.0.1, fixed port).
The reply is forced into a JSON schema (Ollama structured outputs), and
anything that doesn't fit becomes "escalate": when in doubt, Claude.
"""
from __future__ import annotations

import json
import time
from typing import Any

from nova.log import get_logger

_log = get_logger("hub.local_model")

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["tool", "chat", "escalate"]},
        "tool": {"type": "string"},
        "params": {"type": "object", "additionalProperties": {"type": "string"}},
        "say": {"type": "string"},
    },
    "required": ["route", "tool", "params", "say"],
}

SYSTEM_PROMPT = """You are the fast lane of NOVA, Manav's voice assistant. For ONE sentence Manav said, pick a route:

"tool": the sentence is ONE complete command that exactly one tool below does, and every value the tool needs is said in the sentence. Put the tool name in "tool" and its values in "params", copied from the sentence.
"chat": ONLY greetings, thanks, "how are you", "who are you" and similar small talk. Put one short, friendly sentence in "say", spoken as NOVA.
"escalate": EVERYTHING ELSE. Questions that need facts, maths, advice, opinions, writing, code, the news or the web; more than one step; anything that refers to earlier conversation ("it", "that", "again", "more"); anything no tool below does exactly; or whenever you are not completely sure.

When in doubt, choose "escalate". A wrong "tool" or "chat" is much worse than "escalate".
Reply with JSON only: {"route": ..., "tool": "", "params": {}, "say": ""} -- leave fields empty when they don't apply.

TOOLS:
"""

#: Bounds on what the laptop may send, so a request can't balloon the prompt.
MAX_TOOLS = 150
MAX_TEXT = 300
_ESCALATE = {"route": "escalate", "tool": "", "params": {}, "say": ""}


def catalogue_text(tools: list[dict[str, Any]]) -> str:
    """One line per tool: name(param, optional?): description."""
    lines = []
    for t in tools[:MAX_TOOLS]:
        name = str(t.get("name", ""))[:60]
        if not name:
            continue
        params = [str(p)[:40] for p in (t.get("params") or [])][:8]
        required = {str(p) for p in (t.get("required") or [])}
        plist = ", ".join(p if p in required else f"{p}?" for p in params)
        lines.append(f"- {name}({plist}): {str(t.get('description', ''))[:200]}")
    return "\n".join(lines)


def parse_decision(content: str) -> dict[str, Any]:
    """The model's JSON reply as a clean decision; "escalate" for anything
    malformed."""
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return dict(_ESCALATE, reason="not json")
    if not isinstance(obj, dict) or obj.get("route") not in ("tool", "chat", "escalate"):
        return dict(_ESCALATE, reason="bad shape")
    params = obj.get("params")
    if not isinstance(params, dict):
        params = {}
    return {
        "route": obj["route"],
        "tool": str(obj.get("tool") or "")[:60],
        "params": {str(k)[:40]: str(v)[:200] for k, v in list(params.items())[:8]},
        "say": str(obj.get("say") or "")[:300],
    }


class LocalModel:
    """Talks to Ollama through Egress.local_json. One request at a time is
    the caller's job (the hub serializes them)."""

    def __init__(self, egress: Any, *, model: str, port: int, timeout_s: float = 60.0) -> None:
        self._egress = egress
        self.model = model
        self._port = port
        self._timeout = timeout_s

    def installed(self) -> list[str]:
        reply = self._egress.local_json(self._port, "/api/tags", timeout=5.0)
        return [m.get("name", "") for m in reply.get("models", []) if isinstance(m, dict)]

    def loaded(self) -> list[str]:
        reply = self._egress.local_json(self._port, "/api/ps", timeout=5.0)
        return [m.get("name", "") for m in reply.get("models", []) if isinstance(m, dict)]

    def warm_up(self) -> None:
        """Load the model and keep it in memory (keep_alive -1)."""
        self._egress.local_json(self._port, "/api/generate",
                                {"model": self.model, "prompt": "", "keep_alive": -1}, timeout=self._timeout)

    def route(self, text: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            return dict(_ESCALATE, reason="empty")
        body = {
            "model": self.model,
            "messages": [
                # The system prompt comes first and only changes when the
                # tool list does, so Ollama reuses its cached prefix and
                # only the sentence itself has to be read each time.
                {"role": "system", "content": SYSTEM_PROMPT + catalogue_text(tools)},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "format": ROUTE_SCHEMA,
            "think": False,
            "keep_alive": -1,
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 160},
        }
        started = time.monotonic()
        try:
            reply = self._egress.local_json(self._port, "/api/chat", body, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 -- a broken local model means Claude, not a crash
            _log.warning("Local model unavailable (%s)", exc)
            return dict(_ESCALATE, reason="model unavailable")
        content = ((reply or {}).get("message") or {}).get("content", "")
        decision = parse_decision(content)
        decision["ms"] = int((time.monotonic() - started) * 1000)
        return decision
