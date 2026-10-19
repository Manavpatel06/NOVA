"""NOVA's brain on Manav's Claude Max plan (2026-10-15).

"I don't want to use the API key. I want it to link with my Claude Max
plan." So the brain talks to Claude through the official `claude` program
(Claude Code), already signed in with his subscription -- one long-lived
session (nova.brain.claude_arm.ClaudeSession) that stays warm between
sentences and remembers the conversation.

Claude Code's own tools are switched off (--tools "" plus at most the
server-side WebSearch), so NOVA's skills are offered as a catalogue in the
system prompt and Claude answers in a small JSON protocol:

    {"say": "Opening YouTube in Brave.", "actions": [{"tool": "open_website",
     "params": {"site": "youtube", "browser": "brave"}}], "final": true}

NOVA runs each action through the app's invoke_tool (the same
SkillSpec.invoke / Ladder path as everything else: tiers, audit, spoken
approval). With "final": true and every action succeeding, `say` is spoken
straight away -- one round trip for most commands. Otherwise the results go
back to Claude for one more reply (failures, chained steps, questions).

Trust is the same as the API brain (ADR 0016): actions on Manav's own
addressed words run directly; once web results or an untrusted_output
skill's reply have entered this session, every action needs "Nova,
confirm" until the session is restarted (idle timeout or turn limit).

Usage counts against his Max plan's limits; nothing is billed per token.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from nova.brain.agent import AgentResult, InvokeTool, SYSTEM_PROMPT, _memory_context, tool_definitions
from nova.config import Config
from nova.log import get_logger
from nova.security.redact import redact

_log = get_logger("brain.max")

PROTOCOL = """
HOW YOU REPLY -- this is strict. Your entire reply is ONE JSON object and nothing else (no prose, no code fences):
{"say": "<what NOVA speaks>", "actions": [{"tool": "<tool name>", "params": {"<param>": "<string value>"}}], "final": true}
- "actions": the tools to run, in order (empty list when you are only talking). Use only tools listed below, \
only their listed params, all values as strings.
- "final": true when "say" already describes the finished result assuming the actions succeed (e.g. \
"say": "Opening YouTube in Brave."). false when you need to see the results first (to read them out, or to \
decide a next step) -- you will then get a message starting with "RESULTS:" and reply with a new JSON object.
- Never put a question to Manav and an action in the same reply.

TOOLS (NOVA's abilities on his laptop):
"""


def tool_catalogue(skills: dict[str, Any]) -> str:
    lines = []
    for t in tool_definitions(skills):
        props = t["input_schema"]["properties"]
        req = set(t["input_schema"]["required"])
        params = ", ".join(p if p in req else f"{p}?" for p in props) or "no params"
        lines.append(f"- {t['name']}({params}): {t['description']}")
    return "\n".join(lines)


def parse_reply(text: str) -> dict[str, Any] | None:
    """The JSON object in Claude's reply, tolerating code fences or stray
    prose around it. None if there is no usable object."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _actions(obj: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    out = []
    for a in obj.get("actions") or []:
        if not isinstance(a, dict) or not isinstance(a.get("tool"), str):
            continue
        params = a.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        out.append((a["tool"], {str(k): str(v) for k, v in params.items()}))
    return out[:5]


class MaxBrain:
    """Same interface as nova.brain.agent.Agent: available(), respond(), forget()."""

    def __init__(
        self,
        config: Config,
        *,
        session_factory: Callable[[str], Any],
        skills_provider: Callable[[], dict[str, Any]] | None = None,
        context_provider: Callable[[], str] | None = None,
        cli_available: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from nova.skills.registry import all_skills

        self._config = config
        self._session_factory = session_factory
        self._skills_provider = skills_provider or all_skills
        self._context_provider = context_provider or (lambda: "")
        self._cli_available = cli_available or (lambda: True)
        self._clock = clock
        self._session: Any = None
        self._last_turn_at: float | None = None
        self._tainted = False
        self._unavailable_until = 0.0
        self._carry: str | None = None

    def system_prompt(self) -> str:
        prompt = SYSTEM_PROMPT + PROTOCOL + tool_catalogue(self._skills_provider())
        memory = _memory_context()
        if memory:
            prompt += "\n\n" + redact(memory, redact_terms=tuple(self._config.security.redact_terms))
        return prompt

    def available(self) -> bool:
        return (self._config.brain.mode == "max" and self._clock() >= self._unavailable_until
                and self._cli_available())

    async def warm_up(self) -> None:
        """Start the session ahead of the first sentence (app prewarm)."""
        if not self.available():
            return
        try:
            await self._ensure_session().start()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Couldn't start the Claude session yet (%s); will retry on first use", exc)

    def _ensure_session(self) -> Any:
        if self._session is None:
            self._session = self._session_factory(self.system_prompt())
            self._tainted = False
        return self._session

    def note(self, said: str, reply: str) -> None:
        """Something NOVA handled instantly (two-speed brain) -- told to the
        session with the next message, so follow-ups like "turn it back up"
        still make sense."""
        line = f"(Handled instantly without you: Manav said {said!r}; NOVA replied {reply!r})"
        self._carry = f"{self._carry}\n{line}" if self._carry else line

    async def forget(self) -> None:
        sess, self._session = self._session, None
        self._tainted = False
        self._carry = None
        self._last_turn_at = None
        if sess is not None:
            await sess.close()

    async def respond(self, text: str, invoke_tool: InvokeTool) -> AgentResult | None:
        cfg = self._config.brain
        now = self._clock()
        if self._session is not None and (
            (self._last_turn_at is not None and now - self._last_turn_at > cfg.history_idle_s)
            or getattr(self._session, "turns", 0) >= max(4, 3 * cfg.history_turns)
        ):
            await self.forget()  # fresh memory, and any web taint goes with it

        terms = tuple(self._config.security.redact_terms)
        context = redact(self._context_provider(), redact_terms=terms)
        carry, self._carry = self._carry, None
        message = (f"{carry}\n" if carry else "") + (f"[{context}]\n" if context else "") + f"Manav: {text}"
        session = self._ensure_session()
        skills = self._skills_provider()
        tainted = self._tainted
        actions_done: list[str] = []
        used_web = False
        tools_ran = False
        say = ""

        for step in range(cfg.max_steps):
            try:
                reply = await session.send(message, timeout_s=max(cfg.request_timeout_s, 30.0))
            except Exception as exc:  # noqa: BLE001
                _log.error("Max brain: %s", exc)
                self._session = None
                if not tools_ran:
                    self._unavailable_until = self._clock() + 20.0  # router for a moment, then retry
                    return None
                say = say or "Something went wrong partway through that."
                break
            if reply.used_tools:
                used_web = True
                tainted = True
                self._tainted = True
            if reply.is_error:
                _log.error("Max brain: Claude returned an error result (%d chars)", len(reply.text))
                if not tools_ran:
                    return None
                break

            obj = parse_reply(reply.text)
            if obj is None:
                # plain text instead of JSON: speak it, never act on it
                say = reply.text.strip()[:400]
                break
            say = str(obj.get("say") or "").strip()
            todo = _actions(obj)
            if not todo:
                break

            results = []
            all_ok = True
            for name, params in todo:
                tools_ran = True
                try:
                    out, ok = await invoke_tool(name, params, tainted)
                except Exception as exc:  # noqa: BLE001
                    out, ok = f"error: {exc}", False
                all_ok = all_ok and ok
                if ok:
                    actions_done.append(name)
                if getattr(skills.get(name), "untrusted_output", False):
                    tainted = True
                    self._tainted = True
                results.append(f"- {name}: {'ok' if ok else 'FAILED'} -- {redact(str(out), redact_terms=terms)}")

            if obj.get("final") is True and all_ok and say:
                # Spoken now, no second round trip. The results ride along at
                # the start of the next message so the session's memory of
                # what happened stays right ("close it").
                self._carry = "(Results of your last actions: " + "; ".join(r[2:] for r in results) + ")"
                break
            message = "RESULTS:\n" + "\n".join(results) + "\nReply with the JSON object."
        else:
            _log.warning("Max brain hit max_steps (%d)", cfg.max_steps)

        self._last_turn_at = self._clock()
        _log.info("Max brain: actions=%s, web=%s", actions_done or "none", used_web)
        return AgentResult(text=say or "Done.", actions=actions_done, used_web=used_web)

    async def close(self) -> None:
        await self.forget()
