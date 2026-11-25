"""NOVA itself, running on the hub (ADR 0023).

Same thinking as the laptop app, minus the laptop: one sentence goes to the
instant lane (an exact, simple command) or to the Claude conversation, and
every action goes through SkillSpec.invoke() -- the same ladder, audit and
spoken approval. Only skills that make sense on the hub are offered
(HUB_SKILLS); the laptop's (volume, apps, screenshots...) arrive with the
laptop helper in phase 2.

Who is talking is a Channel: the phone's talk page today, later the laptop
helper, the hub's own mic and a phone call. An approval question is asked on
the same channel the sentence came from, and the answer is the next thing
said there.
"""
from __future__ import annotations

import asyncio
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
})
NOT_HERE = "That needs your laptop, and NOVA can't reach it yet (the laptop helper comes next)."
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
    ) -> None:
        import nova.skills  # noqa: F401 -- registers the skills
        from nova.brain.router import Router
        from nova.skills.registry import all_skills

        self.config = config
        self.audit = audit
        self.egress = egress
        self._all = skills_provider or all_skills
        self._journal = journal
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
        return {name: spec for name, spec in self._all().items() if name in HUB_SKILLS}

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
        return f"{now}; {self.config.weather.location_name}; talking from: {getattr(self._channel, 'name', 'phone')}"

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

        return SkillCtx(config=self.config, ladder=self.ladder, egress=self.egress, armed_by=ArmedBy.PTT,
                        origin=Origin.CLAUDE if tainted else Origin.ROUTER)

    async def _turn(self, text: str) -> str:
        instant = self._instant(text)
        if instant is not None:
            try:
                reply = await instant.spec.invoke(self._ctx(), **instant.params)
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
            if spec is None:
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
        allowed = set(self.config.brain.instant_skills) & HUB_SKILLS
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
