"""Approve actions by voice (Manav, 2026-10-15).

When an action needs approval, NOVA says what it is about to do and listens
for the next sentence:

* an ordinary approval ("Should I open Notepad?") -> a plain "yes", "yeah",
  "sure", "do it", "go ahead" approves. Anything else is a no.
* a risky action -- a skill whose own tier is CONFIRM (shutdown, deleting,
  sending) -> only "Nova, confirm" approves. A stray "yes" from someone in
  the room cannot trigger it.

The answer must be SHORT and contain nothing but approval words, so "yes,
but open Chrome instead" or "no wait" is a decline, never a guess. No answer
within the time limit is a timeout (audited separately from a decline).

When voice can't be used (mic muted from the window, no speech detector)
the request goes to the fallback channel (the Yes/No dialog) unchanged.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nova.brain.address import strip_address
from nova.log import get_logger
from nova.security.ladder import ConfirmTimeout
from nova.security.tiers import Tier

if TYPE_CHECKING:
    from nova.skills.registry import SkillSpec

_log = get_logger("security.voice_confirm")

#: ask(prompt) -> what was said (str), None for no answer in time, or
#: VOICE_UNAVAILABLE when the mic can't be used right now.
Ask = Callable[[str], Awaitable["str | None | object"]]
VOICE_UNAVAILABLE = object()

_YES = {
    "yes", "yeah", "yep", "yup", "yes please", "sure", "ok", "okay", "do it", "go ahead",
    "yes do it", "yes go ahead", "go for it", "please do", "absolutely", "of course",
    "confirm", "confirmed", "yes confirm", "sure go ahead", "ok do it", "okay do it", "affirmative",
}
_WORD = re.compile(r"[a-z']+")


def _norm(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def describe(skill: "SkillSpec", params: dict[str, Any]) -> str:
    """What NOVA says it is about to do: the skill's confirm_phrase filled
    with params, else its name plus the param values."""
    phrase = getattr(skill, "confirm_phrase", None)
    params = {k: _cap(str(v)) for k, v in params.items()}
    if phrase:
        try:
            return phrase.format(**params)
        except (KeyError, IndexError, ValueError):
            pass
    words = skill.name.replace("_", " ")
    values = " ".join(str(v) for v in params.values() if str(v).strip())
    return f"{words} {values}".strip()


def _cap(text: str, limit: int = 60) -> str:
    """Model- or speech-supplied values are read aloud in the question; keep
    them short so they can't become a speech of their own."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def is_risky(skill: "SkillSpec") -> bool:
    return skill.tier is Tier.CONFIRM


def decide(answer: str, *, risky: bool, names: list[str] | tuple[str, ...] = ("nova",)) -> bool:
    """True only for an unambiguous approval (see module docstring)."""
    if not answer:
        return False
    if risky:
        command = strip_address(answer, names)
        if not command:
            return False
        return _norm(command) in {"confirm", "confirmed", "i confirm", "yes confirm", "confirm it"}
    command = strip_address(answer, names)
    text = _norm(command if command is not None else answer)
    return text in _YES


class VoiceConfirmChannel:
    """ConfirmChannel that asks out loud and listens for the answer."""

    def __init__(self, ask: Ask, *, fallback=None, names_provider: Callable[[], list[str]] | None = None,
                 strict_provider: Callable[[], bool] | None = None) -> None:
        self._ask = ask
        # True while the action was proposed after web/outside content entered
        # the conversation: then even an ordinary action needs "Nova, confirm".
        self._strict = strict_provider or (lambda: False)
        self._fallback = fallback
        self._names = names_provider or (lambda: ["nova"])

    async def request(self, skill: "SkillSpec", params: dict[str, Any]) -> bool:
        risky = is_risky(skill) or bool(self._strict())
        what = describe(skill, params)
        prompt = f"Should I {what}? Say 'Nova, confirm' to go ahead." if risky else f"Should I {what}?"
        answer = await self._ask(prompt)
        if answer is VOICE_UNAVAILABLE:
            if self._fallback is None:
                raise ConfirmTimeout("voice confirmation unavailable and no fallback channel")
            _log.info("Voice confirm unavailable (mic off); asking with the dialog instead")
            return await self._fallback.request(skill, params)
        if answer is None:
            _log.info("Voice confirm for %s: no answer in time", skill.name)
            raise ConfirmTimeout(f"no spoken answer for '{skill.name}'")
        approved = decide(str(answer), risky=risky, names=self._names())
        _log.info("Voice confirm for %s (%s): %s", skill.name, "risky" if risky else "normal",
                  "approved" if approved else "declined")
        return approved
