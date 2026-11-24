"""The laptop's side of "NOVA's brain on the hub" (2026-11-24, ADR 0021).

RemoteSession has the same shape as claude_arm.ClaudeSession (start, send,
close, turns, running), so MaxBrain doesn't know or care where Claude runs.
It sends each message to the hub (nova.hub.brain_host) over Tailscale.

AutoSession is what NOVA actually uses with `brain.where: auto`: the hub
first; if the hub can't be reached before the conversation has started, the
laptop's own `claude` takes over for this conversation (same as before the
hub existed). A failure mid-conversation is reported up to MaxBrain, which
starts a fresh conversation next time -- never a silent switch that would
lose what was said.
"""
from __future__ import annotations

from typing import Any, Callable

from nova.brain.claude_arm import ClaudeSessionError, SessionReply
from nova.brain.hub_tier import HubUnavailable, hub_post
from nova.log import get_logger

_log = get_logger("brain.remote")


class RemoteSession:
    def __init__(self, config: Any, egress: Any, *, system_prompt: str, web_search: bool, model: str) -> None:
        self._config = config
        self._egress = egress
        self._prompt = system_prompt
        self._web = web_search
        self._model = model
        self._sid = ""
        self.turns = 0

    @property
    def running(self) -> bool:
        return bool(self._sid)

    async def start(self) -> None:
        if self._sid:
            return
        out = await hub_post(self._config, self._egress, "/v1/brain/start",
                             {"system_prompt": self._prompt, "web_search": self._web, "model": self._model},
                             timeout_s=70.0)
        sid = out.get("session") if isinstance(out, dict) else None
        if not isinstance(sid, str) or not sid:
            raise HubUnavailable("hub didn't start a brain session")
        self._sid = sid
        self.turns = 0
        _log.info("Brain session started on the hub")

    async def send(self, text: str, *, timeout_s: float) -> SessionReply:
        if not self._sid:
            await self.start()
        out = await hub_post(self._config, self._egress, "/v1/brain/send",
                             {"session": self._sid, "message": text, "timeout_s": min(float(timeout_s), 180.0)},
                             timeout_s=float(timeout_s) + 10.0)
        if not isinstance(out, dict) or "error" in out or not isinstance(out.get("text"), str):
            self._sid = ""
            raise ClaudeSessionError(str((out or {}).get("error", "bad reply from the hub")) if isinstance(out, dict)
                                     else "bad reply from the hub")
        self.turns += 1
        used = out.get("used_tools")
        if not isinstance(used, list):
            # can't tell whether web content got in: with web search on, assume
            # it did, so every action this conversation needs "Nova, confirm"
            used = ["unknown"] if self._web else []
        return SessionReply(text=out["text"], used_tools=[str(u) for u in used], is_error=bool(out.get("is_error")))

    async def close(self) -> None:
        sid, self._sid = self._sid, ""
        if sid:
            try:
                await hub_post(self._config, self._egress, "/v1/brain/close", {"session": sid}, timeout_s=5.0)
            except HubUnavailable:
                pass


class AutoSession:
    """Hub first; the laptop's own CLI if the hub isn't there."""

    def __init__(self, remote: RemoteSession, local_factory: Callable[[], Any] | None) -> None:
        self._remote = remote
        self._local_factory = local_factory
        self._active: Any = remote
        self.where = "hub"

    @property
    def turns(self) -> int:
        return getattr(self._active, "turns", 0)

    @property
    def running(self) -> bool:
        return bool(getattr(self._active, "running", False))

    def _fall_back(self, why: str) -> bool:
        if self._active is not self._remote or self._remote.turns > 0 or self._local_factory is None:
            return False
        _log.warning("Hub brain unavailable (%s); using this laptop's Claude for this conversation", why)
        self._active = self._local_factory()
        self.where = "laptop"
        return True

    async def start(self) -> None:
        try:
            await self._active.start()
        except HubUnavailable as exc:
            if not self._fall_back(str(exc)):
                raise
            await self._active.start()

    async def send(self, text: str, *, timeout_s: float) -> SessionReply:
        try:
            return await self._active.send(text, timeout_s=timeout_s)
        except HubUnavailable as exc:
            if not self._fall_back(str(exc)):
                raise ClaudeSessionError(f"hub unavailable: {exc}") from exc
            return await self._active.send(text, timeout_s=timeout_s)

    async def close(self) -> None:
        await self._active.close()
