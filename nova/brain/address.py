"""Is this sentence addressed to NOVA? (prefix wake, Manav 2026-10-14)

In prefix mode NOVA transcribes each utterance while its window is open and
acts only when the sentence *starts* with its name: "Nova, open Spotify",
"Hey Nova what time is it". Anything else is somebody talking -- to Manav,
on a call, on the TV -- and is dropped without being read or logged.

`strip_address()` is the single decision point. It is deliberately strict:

* the name must be the FIRST word (after an optional "hey"/"hi"/"ok"/
  "okay"/"yo"). "I told Nova yesterday" is not addressed to NOVA.
* matching is on whole words, case-insensitive, punctuation-tolerant --
  Whisper writes "Nova," / "Nova." / "Hey, Nova!" interchangeably.
* a small set of near-spellings Whisper produces for the spoken word
  ("novah") is accepted; plausible ordinary words are NOT ("over", "no va",
  "novel"), because a false accept here means NOVA acting on overheard
  speech.

Returns the command with the address removed ("" for a bare "Nova"), or
None when the sentence is not addressed to NOVA.
"""
from __future__ import annotations

import re

_GREETINGS = frozenset({"hey", "hi", "hello", "ok", "okay", "yo"})

# Spellings Whisper has been seen to produce for the spoken name. Kept tiny
# on purpose: each extra variant is extra false-accept surface.
_NAME_VARIANTS: dict[str, frozenset[str]] = {
    "nova": frozenset({"nova", "novah"}),
}

_WORD = re.compile(r"[A-Za-z']+")


def _variants(names: list[str] | tuple[str, ...]) -> frozenset[str]:
    out: set[str] = set()
    for name in names:
        n = name.strip().lower()
        if not n:
            continue
        out |= _NAME_VARIANTS.get(n, frozenset({n}))
    return frozenset(out)


def strip_address(text: str, names: list[str] | tuple[str, ...] = ("nova",)) -> str | None:
    """Return the command addressed to NOVA, "" for a bare name, or None."""
    if not text:
        return None
    accepted = _variants(names)
    if not accepted:
        return None

    words = list(_WORD.finditer(text))
    if not words:
        return None

    idx = 0
    if words[0].group().lower() in _GREETINGS and len(words) > 1:
        idx = 1
    if words[idx].group().lower() not in accepted:
        return None

    rest = text[words[idx].end():]
    # drop the punctuation/spacing that follows the name: "Nova, ..." / "Nova. ..."
    rest = rest.lstrip(" \t,.;:!?-—–")
    return rest.strip()
