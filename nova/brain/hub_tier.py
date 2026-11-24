"""The hub's local model as NOVA's middle lane (2026-11-22, docs/decisions/0019).

    instant lane (exact patterns, on the laptop)
      -> HUB LANE (small local model on nova-hub, free)
      -> Claude on the Max plan (everything the hub isn't sure about)

The hub only proposes. Nothing it says is trusted until it passes the checks
below, and every check that fails sends the sentence on to Claude -- never
to a guess:

- only READ/ACT skills are offered at all; CONFIRM-tier skills, skills with
  a confirm phrase, and skills whose output is untrusted stay with Claude
- the tool must exist and be one that was offered; params must be the
  tool's own, all required ones present
- every text value must actually appear in what Manav said (a small model
  that invents an app or a site name is caught here); plain numbers are
  allowed because "forty" arrives as "40"
- a sentence with a negation or condition ("never", "don't", "if", ...)
  is never acted on by the hub; numbers only when Manav said a number, and
  if he said digits, the value must be one of them
- small talk only when the sentence itself is recognisably small talk
  (checked here, not by the model), and the reply must be short with no
  numbers in it -- so the hub can't make up facts ("your meeting is at 3")
- a few skills are never offered (NEVER_FROM_HUB): turning Wi-Fi off would
  cut the hub and Claude off mid-sentence
- the hub must answer within hub.timeout_s; if it fails, it's skipped for a
  minute so one dead hub never slows every sentence

The skill then runs through the normal SkillSpec.invoke() path -- same
ladder, audit and spoken approval as everything else.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nova.log import get_logger
from nova.security.redact import redact
from nova.security.tiers import Tier

_log = get_logger("brain.hub_tier")

CHAT_MAX_WORDS = 12
CHAT_MAX_CHARS = 200
VALUE_MAX_CHARS = 120
DOWN_FOR_S = 60.0
_NUMBER = re.compile(r"^-?\d{1,4}(\.\d{1,2})?%?$")
#: skills the hub never gets, whatever their tier (see module docstring)
NEVER_FROM_HUB = frozenset({"set_radio"})
#: words that change what a command means -- the hub doesn't act on these
_HEDGES = frozenset({
    "not", "never", "no", "dont", "don't", "doesnt", "isnt", "wont", "cant", "cannot", "shouldnt",
    "without", "unless", "if", "when", "whenever", "after", "before", "until", "later", "tomorrow",
    "should", "would", "whether", "maybe",
})
_NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "fifteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "half", "max", "maximum", "full", "min", "minimum",
})
#: small talk, decided here: the whole sentence is one of these
_SMALL_TALK = re.compile(
    r"^(?:(?:hey|hi|hello|yo|ok|okay|good|great)\s+)*"
    r"(?:hi|hey|hello|thanks|thank you|thank you so much|thanks a lot|cheers|good morning|good afternoon|"
    r"good evening|good night|how are you|how are you doing|how's it going|hows it going|what's up|whats up|"
    r"who are you|what's your name|whats your name|nice|cool|awesome|great job|well done|you're awesome|"
    r"youre awesome|love you|bye|goodbye|see you|see ya)"
    r"(?:\s+(?:nova|buddy|man|today|there|again|so much))*$"
)


def offered(spec: Any) -> bool:
    """Skills the hub may pick: no approvals, no untrusted output."""
    return (
        spec.name not in NEVER_FROM_HUB
        and spec.tier in (Tier.READ, Tier.ACT)
        and not getattr(spec, "confirm_phrase", None)
        and not getattr(spec, "untrusted_output", False)
    )


def catalogue(skills: dict[str, Any]) -> list[dict[str, Any]]:
    """What the hub is told about: name, one-line description, params."""
    out = []
    for spec in sorted(skills.values(), key=lambda s: s.name):
        if not offered(spec):
            continue
        params, required = [], []
        for name, p in inspect.signature(spec.handler).parameters.items():
            if name == "ctx":
                continue
            params.append(name)
            if p.default is inspect.Parameter.empty:
                required.append(name)
        examples = "; ".join(f'"{e}"' for e in spec.examples[:2])
        out.append({"name": spec.name, "description": f"{spec.description} e.g. {examples}"[:200],
                    "params": params, "required": required})
    return out


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9%.]+", " ", text.lower()).split())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def is_small_talk(text: str) -> bool:
    return bool(_SMALL_TALK.match(" ".join(_words(text.replace("\u2019", "'")))))


@dataclass(frozen=True)
class HubDecision:
    kind: str  # "tool" | "chat"
    tool: str = ""
    params: dict[str, str] | None = None
    say: str = ""
    ms: int = 0


def check(decision: Any, text: str, skills: dict[str, Any]) -> HubDecision | None:
    """The hub's proposal if it passes every rule above, else None (Claude)."""
    if not isinstance(decision, dict):
        return None
    route = decision.get("route")
    ms = decision.get("ms") if isinstance(decision.get("ms"), int) else 0
    if route == "chat":
        say = decision.get("say")
        if (isinstance(say, str) and say.strip() and len(say) <= CHAT_MAX_CHARS
                and not re.search(r"\d", say)
                and len(text.split()) <= CHAT_MAX_WORDS and is_small_talk(text)):
            return HubDecision(kind="chat", say=say.strip(), ms=ms)
        return None
    if route != "tool":
        return None
    spec = skills.get(decision.get("tool"))
    if spec is None or not offered(spec):
        return None
    params = decision.get("params") or {}
    if not isinstance(params, dict):
        return None
    sig = inspect.signature(spec.handler).parameters
    allowed = {n for n in sig if n != "ctx"}
    required = {n for n, p in sig.items() if n != "ctx" and p.default is inspect.Parameter.empty}
    clean = {k: v.strip() for k, v in params.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
    if len(clean) != len(params) or not set(clean) <= allowed or not required <= set(clean):
        return None
    words = _words(text)
    if _HEDGES & {w.replace("'", "") for w in words} or _HEDGES & set(words):
        return None
    said = _norm(text)
    digits = set(re.findall(r"\d+", text))
    for value in clean.values():
        if len(value) > VALUE_MAX_CHARS:
            return None
        if _NUMBER.match(value):
            number = re.sub(r"[^\d.-]", "", value).split(".")[0].lstrip("-")
            if digits:
                if number not in digits:
                    return None  # he said a different number
            elif not _NUMBER_WORDS & set(words):
                return None  # he said no number at all
            continue
        if not _norm(value) or f" {_norm(value)} " not in f" {said} ":
            return None  # a value Manav didn't say
    return HubDecision(kind="tool", tool=spec.name, params=clean, ms=ms)


class HubTier:
    def __init__(
        self,
        config: Any,
        egress: Any,
        *,
        skills_provider: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from nova.skills.registry import all_skills

        self._config = config
        self._egress = egress
        self._skills = skills_provider or all_skills
        self._clock = clock
        self._down_until = 0.0

    @property
    def host(self) -> str:
        return self._config.hub.host.strip().lower()

    def enabled(self) -> bool:
        return bool(self.host) and self._config.hub.local_tier and self._clock() >= self._down_until

    def _url(self, path: str) -> str:
        return f"https://{self.host}{path}"

    async def decide(self, text: str) -> HubDecision | None:
        """Ask the hub about one sentence; None means "give it to Claude"."""
        if not self.enabled():
            return None
        skills = self._skills()
        terms = tuple(self._config.security.redact_terms)
        body = {"text": redact(text, redact_terms=terms)[:300], "tools": catalogue(skills)}
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(self._egress.post_json, self._url("/v1/route"), body,
                                  headers={}, timeout=self._config.hub.timeout_s),
                timeout=self._config.hub.timeout_s + 1.0,
            )
        except Exception as exc:  # noqa: BLE001 -- the hub is optional
            self._down_until = self._clock() + DOWN_FOR_S
            _log.warning("Hub didn't answer (%s); using Claude for the next minute", type(exc).__name__)
            return None
        decision = check(reply, text, skills)
        _log.info("Hub lane: %s", f"{decision.kind} {decision.tool} ({decision.ms} ms)" if decision
                  else f"to Claude ({reply.get('route') if isinstance(reply, dict) else '?'})")
        return decision

    async def push_memory(self, memory_dir: Path, *, timeout_s: float = 30.0) -> bool:
        """Mirror NOVA's memory folder to the hub (on start and on exit)."""
        if not self.host:
            return False
        root = Path(memory_dir)
        files: dict[str, str] = {}
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix in (".json", ".md", ".txt") and len(files) < 2000:
                    rel = path.relative_to(root).as_posix()
                    if rel.count("/") <= 1:
                        files[rel] = path.read_text(encoding="utf-8", errors="replace")
        if not files:
            return False
        try:
            # complete: the hub drops mirrored files the laptop no longer has,
            # so something NOVA forgot here is forgotten there too
            await asyncio.wait_for(
                asyncio.to_thread(self._egress.post_json, self._url("/v1/memory"),
                                  {"files": files, "complete": True}, headers={}, timeout=timeout_s, method="PUT"),
                timeout=timeout_s + 1.0,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Memory backup to the hub failed (%s)", type(exc).__name__)
            return False
        _log.info("Memory mirrored to the hub (%d files)", len(files))
        return True


async def hub_post(config: Any, egress: Any, path: str, body: dict[str, Any], *, timeout_s: float = 8.0) -> dict[str, Any]:
    """One owner-only call to the hub (reminders and the like). Raises
    HubUnavailable when there's no hub or it can't be reached."""
    host = config.hub.host.strip().lower()
    if not host or egress is None:
        raise HubUnavailable("no hub configured")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(egress.post_json, f"https://{host}{path}", body, headers={}, timeout=timeout_s),
            timeout=timeout_s + 1.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise HubUnavailable(type(exc).__name__) from exc


class HubUnavailable(RuntimeError):
    pass
