"""NOVA itself, running on the hub (ADR 0023).

Same thinking as the laptop app, minus the laptop: one sentence goes to the
instant lane (an exact, simple command) or to the Claude conversation, and
every action goes through SkillSpec.invoke() -- the same ladder, audit and
spoken approval. Only skills that make sense on the hub are offered
(HUB_SKILLS); the laptop's (volume, apps, screenshots...) arrive with the
laptop helper in phase 2.

Who is talking is a Channel: the phone's talk page, the laptop's mic (through
NOVA Link), the hub's own mic, or a phone call. An approval question is asked
on the same channel the sentence came from, and the answer is the next thing
said there.

Every other skill (volume, apps, screenshots, power...) is offered too, and
runs on the laptop through NOVA Link (ADR 0024): the hub's ladder approves
it first, exactly as before, then the laptop helper runs the handler. If the
laptop isn't connected, NOVA says so -- it never pretends.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
from typing import Any, Callable, Protocol

from nova.log import get_logger
from nova.security.ladder import Ladder
from nova.security.tiers import ArmedBy, Origin
from nova.security.voice_confirm import VoiceConfirmChannel

_log = get_logger("hub.assistant")

#: skills that run on the hub itself; anything else needs a device helper
HUB_SKILLS = frozenset({
    "time", "date", "weather", "capabilities",
    "remember", "recall", "forget",
    "remind_me", "set_timer", "list_reminders", "cancel_reminders",
    "upgrade_nova", "upgrade_status", "discard_upgrade",
    "call_me",
})
NOT_HERE = "That needs your laptop, and it isn't connected right now (is it on, with NOVA Link running?)."
#: never run remotely: applying an upgrade is the hub's own job (ADR 0024)
NOT_REMOTE = frozenset({"apply_upgrade"})
LAPTOP = "laptop"
RUN_TIMEOUT_S = 60.0
RESOLVE_TIMEOUT_S = 10.0
NO_BRAIN = "I can't think right now: Claude isn't reachable from the hub. Simple commands still work."


class Channel(Protocol):
    async def ask(self, prompt: str) -> "str | None":
        """Say `prompt` and return the next thing said here (None: no answer)."""


class HubAssistant:
    def __init__(
        self,
        config: Any,
        *,
        audit: Any,
        egress: Any,
        brain: Any | None = None,
        skills_provider: Callable[[], dict[str, Any]] | None = None,
        journal: bool = True,
        devices: Any | None = None,
        local_skills: dict[str, Any] | None = None,
    ) -> None:
        import nova.skills  # noqa: F401 -- registers the skills
        from nova.brain.router import Router
        from nova.skills.registry import all_skills

        self.config = config
        self.audit = audit
        self.egress = egress
        self._all = skills_provider or all_skills
        self._journal = journal
        self.devices = devices
        self._extra = dict(local_skills or {})   # hub-side replacements (apply_upgrade)
        self._proxies: dict[str, Any] = {}
        self._channel: Channel | None = None
        self._strict = False
        self._lock = asyncio.Lock()
        self.ladder = Ladder(audit, confirm_channel=VoiceConfirmChannel(
            self._ask, fallback=None,
            names_provider=lambda: list(config.wake.names),
            strict_provider=lambda: self._strict,
        ))
        self.router = Router(confidence_floor=config.brain.confidence_floor,
                             ambiguity_margin=config.brain.ambiguity_margin)
        self.brain = brain if brain is not None else self._make_brain()

    # --- setup ------------------------------------------------------------

    def skills(self) -> dict[str, Any]:
        """Everything NOVA can do from here: hub skills run here, the rest
        run on the laptop (when a device link exists)."""
        out = {}
        for name, spec in self._all().items():
            if name in self._extra:
                out[name] = self._extra[name]
            elif name in HUB_SKILLS:
                out[name] = spec
            elif self.devices is not None and name not in NOT_REMOTE:
                out[name] = self._proxy(spec)
        return out

    def is_remote(self, name: str) -> bool:
        return name in self._proxies

    def _proxy(self, spec: Any) -> Any:
        proxy = self._proxies.get(spec.name)
        if proxy is not None and proxy.tier is spec.tier:
            return proxy
        from nova.skills.registry import Reply

        name = spec.name

        async def handler(ctx, **params):
            out = await self._on_device(LAPTOP, "run", name, params, RUN_TIMEOUT_S)
            return Reply(text=out.get("text") or ("Done." if out.get("success") else "That didn't work."),
                         success=bool(out.get("success")))

        handler.__signature__ = inspect.signature(spec.handler)
        resolve = None
        if spec.resolve_for_confirm is not None:
            async def resolve(ctx, params):
                out = await self._on_device(LAPTOP, "resolve", name, dict(params), RESOLVE_TIMEOUT_S)
                return out.get("resolved")

        proxy = dataclasses.replace(spec, handler=handler, resolve_for_confirm=resolve)
        self._proxies[name] = proxy
        return proxy

    async def _on_device(self, device: str, kind: str, skill: str, params: dict, timeout: float) -> dict:
        if self.devices is None or not self.devices.online(device):
            return {"text": NOT_HERE, "success": False}
        if skill not in self.devices.skills(device):
            return {"text": f"Your {device} doesn't have {skill.replace('_', ' ')} (update NOVA there).",
                    "success": False}
        fut = self.devices.submit(device, kind, skill, params)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.devices.give_up(fut)
            return {"text": f"Your {device} didn't answer in time.", "success": False}

    def _offline(self, name: str) -> bool:
        return self.is_remote(name) and (self.devices is None or not self.devices.online(LAPTOP))

    def _make_brain(self):
        if self.config.brain.mode != "max":
            return None
        from nova import DATA_DIR
        from nova.brain.claude_arm import ClaudeCliNotFound, ClaudeSession, find_cli
        from nova.brain.max_brain import MaxBrain

        brain_dir = DATA_DIR / "brain"  # empty working dir: nothing for the CLI to read

        def session(system_prompt: str) -> ClaudeSession:
            brain_dir.mkdir(parents=True, exist_ok=True)
            return ClaudeSession(egress=self.egress, audit=self.audit, model=self.config.brain.model,
                                 system_prompt=system_prompt, web_search=self.config.brain.web_search,
                                 cwd=str(brain_dir))

        def cli_ok() -> bool:
            try:
                find_cli()
                return True
            except ClaudeCliNotFound:
                return False

        return MaxBrain(self.config, session_factory=session, skills_provider=self.skills,
                        cli_available=cli_ok, context_provider=self._context)

    def _context(self) -> str:
        from datetime import datetime

        now = datetime.now().strftime("%A %B %d %Y, %I:%M %p").replace(" 0", " ")
        laptop = ("laptop connected" if self.devices is not None and self.devices.online(LAPTOP)
                  else "laptop NOT connected: laptop skills will fail, say so instead of trying")
        return (f"{now}; {self.config.weather.location_name}; talking from: "
                f"{getattr(self._channel, 'name', 'phone')}; {laptop}")

    async def warm_up(self) -> None:
        if self.brain is not None and hasattr(self.brain, "warm_up"):
            await self.brain.warm_up()

    # --- one sentence -----------------------------------------------------

    async def _ask(self, prompt: str) -> "str | None":
        if self._channel is None:
            return None
        return await self._channel.ask(prompt)

    async def turn(self, text: str, channel: Channel) -> str:
        """Everything after speech became text; returns what NOVA says."""
        text = (text or "").strip()
        if not text:
            return "I didn't catch that."
        async with self._lock:
            self._channel = channel
            self._actions: list[str] = []
            try:
                reply = await self._turn(text)
            finally:
                self._channel = None
                self._strict = False
        if self._journal and self.config.brain.journal:
            try:
                from nova.memory.store import default_store

                await asyncio.to_thread(default_store().log_turn, text, reply, self._actions)
            except Exception as exc:  # noqa: BLE001 -- memory is best-effort
                _log.warning("Journal write failed (%s)", exc)
        return reply

    def _ctx(self, *, tainted: bool = False):
        from nova.skills.registry import SkillCtx

        armed = getattr(self._channel, "armed_by", ArmedBy.PTT)
        return SkillCtx(config=self.config, ladder=self.ladder, egress=self.egress, armed_by=armed,
                        origin=Origin.CLAUDE if tainted else Origin.ROUTER)

    async def _turn(self, text: str) -> str:
        instant = self._instant(text)
        if instant is not None:
            spec = self.skills().get(instant.spec.name)
            if spec is None or self._offline(spec.name):
                return NOT_HERE
            try:
                reply = await spec.invoke(self._ctx(), **instant.params)
            except Exception as exc:  # noqa: BLE001 -- never retried: it may have half-run
                _log.error("instant %s raised (%s)", instant.spec.name, type(exc).__name__)
                return f"Sorry, something went wrong with {instant.spec.name}."
            if reply.success:
                self._actions.append(instant.spec.name)
            note = getattr(self.brain, "note", None)
            if note is not None:
                note(text, reply.text)
            return reply.text
        if self.brain is None or not self.brain.available():
            return NO_BRAIN
        ran: list[str] = []
        allowed = self.skills()

        async def invoke_tool(name: str, params: dict[str, Any], tainted: bool) -> tuple[str, bool]:
            spec = allowed.get(name)
            if spec is None or self._offline(name):
                return NOT_HERE, False
            ran.append(name)
            self._strict = tainted
            try:
                reply = await spec.invoke(self._ctx(tainted=tainted), **params)
            except TypeError as exc:  # wrong/missing params: tell the model, let it retry
                return f"bad parameters for {name}: {exc}", False
            except Exception as exc:  # noqa: BLE001
                _log.error("tool %s raised (%s)", name, type(exc).__name__)
                return f"{name} failed", False
            finally:
                self._strict = False
            if reply.success:
                self._actions.append(name)
            return reply.text, reply.success

        try:
            result = await self.brain.respond(text, invoke_tool)
        except Exception as exc:  # noqa: BLE001 -- never let the brain take the turn down
            _log.error("brain crashed (%s)", type(exc).__name__)
            result = None
        if result is None:
            return "Something went wrong partway through that." if ran else NO_BRAIN
        return result.text

    def _instant(self, text: str):
        allowed = set(self.config.brain.instant_skills) & set(self.skills())
        words = text.lower().replace(",", " ").split()
        if not allowed or not words or len(words) > 8:
            return None
        if {"and", "then", "also", "but", "after", "before", "while", "unless", "if"} & set(words):
            return None
        try:
            match = self.router.route(text)
        except Exception:  # noqa: BLE001
            return None
        if match.spec is None or match.clarification is not None or match.confidence < 1.0:
            return None
        return match if match.spec.name in allowed else None


def hub_apply_upgrade(devices: Any, restart: Callable[[], None]) -> Any:
    """apply_upgrade as the hub's NOVA runs it (ADR 0024): the same skill --
    CONFIRM tier, same read-back of the request, files and version, checked
    again after "Nova, confirm" -- but installed here: the hub fast-forwards
    its own NOVA and pushes, tells the laptop helper to install that exact
    version, then restarts itself to load the new skill."""
    from nova.brain.hub_tier import HubUnavailable, hub_post
    from nova.skills.registry import Reply, get_skill
    from nova.skills.upgrades import _ready

    original = get_skill("apply_upgrade")

    async def handler(ctx, upgrade: str = "the ready one"):
        try:
            ready = await _ready(ctx)
            if ready is None:
                return Reply(text="There's no upgrade ready to install.", success=False)
            out = await hub_post(ctx.config, ctx.egress, "/v1/forge/apply",
                                 {"id": ready["id"], "commit": ready["commit"]}, timeout_s=150.0)
        except HubUnavailable as exc:
            return Reply(text=f"I couldn't install it: {exc}.", success=False)
        if not out.get("ok"):
            return Reply(text=f"I couldn't install it: {out.get('error', 'the forge said no')}.", success=False)
        laptop = ""
        if devices is not None and devices.online(LAPTOP) and out.get("pushed"):
            fut = devices.submit(LAPTOP, "update", "apply_upgrade", {"commit": ready["commit"]})
            try:
                res = await asyncio.wait_for(asyncio.wrap_future(fut), 120)
                laptop = " The laptop has it too." if res.get("success") else " The laptop couldn't take it yet."
            except asyncio.TimeoutError:
                devices.give_up(fut)
                laptop = " The laptop didn't answer; it'll need an update later."
        elif not out.get("pushed"):
            laptop = " (GitHub didn't get it; run 'nova-hub github' once.)"
        restart()
        return Reply(text=f"Installed.{laptop} I'm restarting to load it; give me about twenty seconds.")

    handler.__signature__ = inspect.signature(original.handler)
    return dataclasses.replace(original, handler=handler)
