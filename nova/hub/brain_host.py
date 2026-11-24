"""NOVA's brain, running on the hub (2026-11-24, docs/decisions/0021).

The laptop's MaxBrain used to start `claude` (Claude Code, signed in with
Manav's Max plan) on the laptop itself. Now the hub runs that session and
the laptop talks to it through nova.brain.remote_session.RemoteSession:

    laptop MaxBrain --(text, over Tailscale)--> hub BrainHost --> claude CLI
                    <--(Claude's JSON reply)---

Only the conversation moves. NOVA's tools still run on the laptop, through
the same ladder and approvals -- the hub never acts on the laptop. It saves
the laptop the memory and CPU of a resident `claude` process, and the hub is
where the brain will also answer the phone later.

One session at a time (one owner). The session's asyncio work runs on a
private event loop in a background thread, because the hub's HTTP listener
is thread-based.
"""
from __future__ import annotations

import asyncio
import secrets
import threading
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("hub.brain")

MAX_PROMPT = 60_000
MAX_MESSAGE = 30_000
MAX_TIMEOUT_S = 180.0


class BrainError(ValueError):
    pass


class BrainHost:
    def __init__(self, session_factory: Callable[[str, bool, str], Any]) -> None:
        """`session_factory(system_prompt, web_search, model)` returns an
        object with async start()/send(text, timeout_s=)/close() --
        nova.brain.claude_arm.ClaudeSession in production."""
        self._factory = session_factory
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="brain-loop", daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self._session: Any = None
        self._id = ""

    def _run(self, coro: Any, timeout: float) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except BaseException:
            fut.cancel()  # a timed-out start must not go on to spawn claude afterwards
            try:
                fut.result(5)
            except BaseException:  # noqa: BLE001
                pass
            raise

    def start(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise BrainError("body must be a JSON object")
        prompt, web, model = body.get("system_prompt"), body.get("web_search"), body.get("model")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
            raise BrainError(f"system_prompt must be 1..{MAX_PROMPT} characters")
        if not isinstance(web, bool):
            raise BrainError("web_search must be true or false")
        if not isinstance(model, str) or not 1 <= len(model) <= 60 or not model.replace("-", "").replace(".", "").isalnum():
            raise BrainError("bad model name")
        with self._lock:
            old, self._session = self._session, None
            if old is not None:
                self._close(old)
            session = self._factory(prompt, web, model)
            try:
                self._run(session.start(), 60.0)
            except Exception as exc:  # noqa: BLE001 -- never leave a half-started claude behind
                self._close(session)
                raise BrainError(f"couldn't start Claude on the hub ({type(exc).__name__})") from None
            self._session, self._id = session, secrets.token_hex(8)
        _log.info("brain session started on the hub (model %s, web %s)", model, web)
        return {"session": self._id}

    def send(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise BrainError("body must be a JSON object")
        sid, message, timeout = body.get("session"), body.get("message"), body.get("timeout_s", 60.0)
        if not isinstance(message, str) or not message or len(message) > MAX_MESSAGE:
            raise BrainError(f"message must be 1..{MAX_MESSAGE} characters")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= MAX_TIMEOUT_S:
            raise BrainError(f"timeout_s must be 1..{MAX_TIMEOUT_S:.0f}")
        with self._lock:
            if self._session is None or not isinstance(sid, str) or not secrets.compare_digest(sid, self._id):
                raise BrainError("no such session (start a new one)")
            session = self._session
        try:
            reply = self._run(session.send(message, timeout_s=float(timeout)), float(timeout) + 5.0)
        except Exception as exc:  # noqa: BLE001 -- the laptop decides what to do next
            with self._lock:
                if self._session is session:
                    self._session, self._id = None, ""
            _log.warning("brain session failed (%s)", type(exc).__name__)
            return {"error": f"session failed: {type(exc).__name__}"}
        return {"text": reply.text, "used_tools": list(reply.used_tools), "is_error": bool(reply.is_error)}

    def close(self, body: Any = None) -> dict[str, Any]:
        with self._lock:
            sid = body.get("session") if isinstance(body, dict) else None
            if self._session is not None and isinstance(sid, str) and secrets.compare_digest(sid, self._id):
                self._close(self._session)
                self._session, self._id = None, ""
                return {"ok": True}
        return {"ok": False}

    def active(self) -> bool:
        return self._session is not None

    def _close(self, session: Any) -> None:
        try:
            self._run(session.close(), 15.0)
        except Exception:  # noqa: BLE001
            pass
