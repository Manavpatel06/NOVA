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


# Words that may never become an accepted name even if the speech model
# hears "Nova" as them: they start ordinary sentences far too often.
UNSAFE_NAMES = frozenset({
    "a", "an", "and", "are", "but", "can", "could", "do", "for", "go", "hey", "hi", "hmm", "how",
    "i", "if", "is", "it", "know", "let", "no", "not", "now", "oh", "ok", "okay", "on", "or",
    "please", "so", "that", "the", "then", "this", "uh", "um", "we", "well", "what", "when",
    "where", "why", "yeah", "yes", "you",
    "all", "at", "be", "did", "does", "has", "have", "he", "in", "just", "look", "maybe", "me",
    "my", "of", "one", "right", "she", "sure", "thank", "thanks", "there", "they", "to", "was",
    "with", "wait", "hello", "yo", "hm", "mm", "huh", "ah", "er",
})


def first_name_word(text: str) -> str | None:
    """The word in the name position: the first word, or the second if the
    first is a greeting ("Hey Nova ..."). Lower-case letters only."""
    words = [w.group().lower() for w in _WORD.finditer(text or "")]
    if not words:
        return None
    if words[0] in _GREETINGS and len(words) > 1:
        return words[1]
    return words[0]


def load_learned_names(path) -> list[str]:
    """Names taught by scripts/teach_name.py; [] if none or unreadable."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = [str(n).strip().lower() for n in data.get("names", [])]
    except Exception:  # noqa: BLE001 -- a broken file must never stop NOVA
        return []
    return [n for n in names if n and n.isalpha() and n not in UNSAFE_NAMES]
