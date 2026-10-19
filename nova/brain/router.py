"""The router: text in, a skill (or a clarifying question) out.

The calibration *method* behind the two config numbers below (not just the
current numbers) is written up durably in docs/ROUTER_CALIBRATION.md --
read that before re-tuning confidence_floor/ambiguity_margin for a grown
registry; this docstring is the in-code summary, that doc is the one meant
to survive independently of this file.

## Matching, deterministically, against `examples` alone

No LLM, no second pattern list anywhere: every skill's `examples` (already
exposed by `nova.skills.registry`) are the *only* source of truth for what
that skill sounds like. Adding a skill means adding a file with `examples`;
it never means touching this one.

For each skill, the router scores the input text against every one of that
skill's examples and keeps the best. The score is a 50/50 blend of two
off-the-shelf, deterministic string-similarity primitives, chosen for what
each one is actually good at:

  - **Token-set Jaccard** (`|A ∩ B| / |A ∪ B|` over lowercased, stopword-
    stripped word sets) — answers "do the same salient words appear",
    robust to word order and extra filler words ("could you open notepad
    for me" vs. "open notepad").
  - **`difflib.SequenceMatcher.ratio()`** (stdlib Ratcliff/Obershelp) — a
    character-level similarity that answers "does the phrasing as a whole
    resemble a known example", which catches near-miss typos and short
    phrasings that Jaccard's set semantics would score too harshly (e.g.
    single-word inputs, where a one-word set intersection swings wildly).

Neither alone is enough — Jaccard is blind to phrasing/typos, char-ratio is
blind to word order and padding — so the score is their unweighted average.
This is a crude, legible metric, not a fabricated-precision one: every
number that comes out of it is `0.5 * jaccard + 0.5 * ratio`, both terms
independently inspectable.

## Confidence floor and ambiguity margin — how the two numbers were picked

`config.brain.confidence_floor` and `config.brain.ambiguity_margin` were
calibrated empirically against a small labelled phrase set (see
`scripts/verify_router.py`, which prints the full score table this
docstring's numbers came from) covering: exact examples, paraphrases,
filler-padded phrasings, single-word utterances, and clearly off-topic /
gibberish text. The lowest any clean match (including paraphrases the
registry never saw verbatim, e.g. "start notepad", "pull up task manager")
scored against its correct skill was 0.487; the highest a deliberately
off-topic or gibberish phrase ("what's the weather today", "send an email
to john", a keyboard-mash string) scored against *either* M0 skill was
0.235. The floor (0.42) sits inside that gap, noticeably closer to the
clean-match floor (0.487) than the off-topic ceiling (0.235) — biased
toward asking rather than guessing, per the constitution's
confidence-honesty rule, not toward maximizing accept rate. The ambiguity
margin (0.08) is small on purpose: with only two skills in M0 there is
little real cross-domain overlap to measure against, so it is set to catch
near-ties (which a floating-point-adjacent score difference on paraphrased
input can produce) without being large enough to force spurious
clarifications between two genuinely different top scores. Both numbers
should be re-calibrated the moment M0's two-skill registry grows — this
docstring documents the *method*, not a promise that 0.42/0.08 are correct
forever.

One real false positive turned up during calibration and is worth recording
because it shaped the design, not just the constants: an early version
computed the char-ratio term over raw sentences, and "what's the weather
today" scored 0.433 against the `time` skill — above a first-draft floor —
purely because "what's the ..." is a long shared prefix with "what's the
time". Fix was two-fold: WH-question words ("what", "what's", "when",
"where", "who", "why", "how", "which") joined the stopword list (they're
near-universal in *any* question, so their presence is not skill-
distinguishing signal), and the char-ratio term now runs over the
stopword-stripped, sorted content tokens rather than the raw sentence, so
shared filler can no longer inflate similarity on its own.

## Parameter extraction — deliberately crude, and says so

The `@skill` decorator has no slot/placeholder syntax (`examples` are literal
utterances like "open notepad", not templates like "open {app}"), so there is
no schema to extract from generically. What this router does instead, for a
skill whose handler declares exactly one parameter (found via
`inspect.signature`, never a hardcoded skill/param name):

1. Derive that skill's "trigger words" as the *first token of each of its
   own examples* — e.g. open_app's examples start with "open" or "launch",
   so `{"open", "launch"}` is derived from the registry, not hand-maintained.
2. Strip leading trigger words and a small generic stopword list from the
   input's tokens.
3. What remains is the parameter value — *if* it's non-empty and not
   implausibly long (more than 5 tokens). Empty or too-long: clarify, don't
   guess. A skill with more than one parameter always clarifies for now —
   this heuristic cannot confidently split unstructured text into multiple
   slots, and guessing wrong is worse than asking.

This is tuned to the "verb + object" shape every M0 example happens to have.
It is not a general slot-filling system, and the module docstring says so on
purpose so the next skill author doesn't assume it's smarter than it is.
"""

from __future__ import annotations

import difflib
import inspect
import re
from dataclasses import dataclass

from nova.security.untrusted import Untrusted
from nova.skills.registry import SkillSpec, all_skills

_STOPWORDS = {
    "a", "an", "the", "please", "could", "you", "your", "to", "for", "of",
    "on", "my", "me", "i", "can", "would", "is", "it", "its", "do", "does",
    "did", "will", "and", "in", "at", "now", "up", "just", "that", "this",
    "want", "need", "like", "go", "ahead",
    # WH-question words: near-universal across any question phrasing, so
    # (unlike a skill's real content words) their presence tells the router
    # nothing about *which* skill is meant. Stripping them stops "what's the
    # weather" and "what's the time" from scoring close on shared filler.
    "what", "what's", "whats", "when", "where", "who", "why", "how", "which",
}

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _content_tokens(text: str) -> set[str]:
    return {t for t in _tokenize(text) if t not in _STOPWORDS}


def _similarity(text: str, example: str) -> float:
    text_tokens = _content_tokens(text)
    example_tokens = _content_tokens(example)

    # F43 (nova-security, HIGH): if BOTH sides reduce to zero content
    # tokens, there are no content words to compare -- e.g. "what can you
    # do" (T2.1's capabilities skill; "what"/"can"/"you"/"do" are all
    # stopwords) against emoji, an empty transcript, or whitespace/control-
    # byte noise, all of which are things a real STT pipeline hands the
    # router on garbled audio, not hypothetical inputs. Falling through to
    # the normal jaccard/char_ratio math below is wrong two different ways
    # here: jaccard's 0/0 always resolves to its 0.0 fallback regardless of
    # whether the two phrases are related, and char_ratio would run
    # `difflib.SequenceMatcher(None, "", "").ratio()`, which returns 1.0 --
    # two empty sequences count as "equal" by difflib's convention -- so
    # *every* zero-content-token pair, matching or not, blended to a flat
    # 0.5 and cleared the confidence floor (0.42). Caught by
    # test_adversarial_input_always_degrades_to_a_clarification_never_a_crash
    # (emoji case) the moment T2.1 added the first all-stopword example
    # this registry has ever had.
    #
    # The fix is not a flat 0.0 either: "what can you do" must still match
    # itself (it's one of capabilities' mandated examples), and a flat 0.0
    # would refuse that as readily as it refuses emoji, throwing away the
    # one case where an all-stopword phrase genuinely is the signal. So
    # when there are no content words on either side, the function words
    # themselves are the only signal left -- compare *those*, honestly,
    # instead of pretending there's none (0.0) or that absence-of-signal
    # is agreement (the difflib-on-two-empty-strings behavior above).
    # Sorted the same way the content-token path sorts its tokens, for the
    # same word-order-robustness reason.
    if not text_tokens and not example_tokens:
        text_raw = " ".join(sorted(_tokenize(text)))
        example_raw = " ".join(sorted(_tokenize(example)))
        return difflib.SequenceMatcher(None, text_raw, example_raw).ratio()

    union = text_tokens | example_tokens
    jaccard = len(text_tokens & example_tokens) / len(union) if union else 0.0
    # Char-level ratio runs over the stopword-stripped, sorted-and-rejoined
    # content tokens -- not the raw sentence -- so shared filler ("what's
    # the ...", "could you ... for me") can't inflate similarity between
    # otherwise unrelated phrases sharing only their function words.
    text_joined = " ".join(sorted(text_tokens))
    example_joined = " ".join(sorted(example_tokens))
    char_ratio = difflib.SequenceMatcher(None, text_joined, example_joined).ratio()
    return 0.5 * jaccard + 0.5 * char_ratio


@dataclass(frozen=True)
class Match:
    spec: SkillSpec | None
    params: dict[str, object]
    confidence: float
    clarification: str | None  # set when NOVA should ask instead of act


class Router:
    """Deterministic, example-driven skill router. See module docstring for
    the scoring method and the two config numbers' provenance."""

    def __init__(
        self,
        skills: dict[str, SkillSpec] | None = None,
        *,
        confidence_floor: float = 0.42,
        ambiguity_margin: float = 0.08,
    ) -> None:
        # `skills=None` reads the live registry (production wiring: just
        # `Router()`). The explicit-dict form exists so nova-tester can
        # exercise routing logic against a small fixture registry without
        # depending on nova.skills' real, globally-discovered contents.
        self._skills: dict[str, SkillSpec] = dict(skills) if skills is not None else all_skills()
        self._confidence_floor = confidence_floor
        self._ambiguity_margin = ambiguity_margin

    def route(self, text: str) -> Match:
        if isinstance(text, Untrusted):
            raise TypeError(
                "Router.route() received Untrusted content directly -- reveal() it "
                "explicitly for an answer, or do not route on it at all "
                "(docs/SECURITY_SPEC.md section 1.3)"
            )
        if not isinstance(text, str):
            raise TypeError(f"Router.route() requires str, got {type(text).__name__}")

        exact = self._match_patterns(text)
        if exact is not None:
            return exact

        scored: list[tuple[float, SkillSpec, str]] = []
        for spec in self._skills.values():
            best_score = 0.0
            best_example = ""
            for example in spec.examples:
                score = _similarity(text, example)
                if score > best_score:
                    best_score = score
                    best_example = example
            scored.append((best_score, spec, best_example))
        scored.sort(key=lambda t: t[0], reverse=True)

        if not scored or scored[0][0] < self._confidence_floor:
            top_score = scored[0][0] if scored else 0.0
            return Match(spec=None, params={}, confidence=top_score, clarification=self._ask_low_confidence(text))

        top_score, top_spec, top_example = scored[0]
        if len(scored) > 1 and (top_score - scored[1][0]) < self._ambiguity_margin:
            return Match(
                spec=None,
                params={},
                confidence=top_score,
                clarification=self._ask_ambiguous(top_spec, scored[1][1]),
            )

        params = self._extract_params(text, top_spec)
        if params is None:
            return Match(spec=None, params={}, confidence=top_score, clarification=self._ask_missing_param(top_spec))

        return Match(spec=top_spec, params=params, confidence=top_score, clarification=None)

    # -- exact command shapes (skill `patterns`) --------------------------

    @staticmethod
    def normalize(text: str) -> str:
        """Lower-case, punctuation-free, polite padding removed -- what a
        skill's `patterns` are matched against."""
        t = text.strip().lower()
        t = re.sub(r"[\u2019`]", "'", t)
        t = re.sub(r"[^a-z0-9' %:/-]+", " ", t)  # drop , . ! ? etc.
        t = re.sub(r"\s+", " ", t).strip()
        for _ in range(3):
            t = re.sub(r"^(?:please|can you|could you|would you|will you|hey|ok|okay) ", "", t)
            t = re.sub(r" (?:please|for me|now|thanks|thank you)$", "", t)
        return t.strip()

    def _match_patterns(self, text: str) -> Match | None:
        """Every skill's `patterns` against the whole sentence. One hit is
        certain (confidence 1.0); hits from two different skills are a
        clarification, never a guess."""
        norm = self.normalize(text)
        if not norm:
            return None
        hits: list[tuple[SkillSpec, dict[str, object]]] = []
        for attr in ("patterns", "catch_all_patterns"):
            for spec in self._skills.values():
                for rx in getattr(spec, attr, ()) or ():
                    m = rx.fullmatch(norm)
                    if m is None:
                        continue
                    params = {k: v.strip() for k, v in m.groupdict().items() if v is not None and v.strip()}
                    hits.append((spec, params))
                    break
            if hits:
                break
        if not hits:
            return None
        if len({h[0].name for h in hits}) > 1:
            return Match(spec=None, params={}, confidence=1.0,
                         clarification=self._ask_ambiguous(hits[0][0], hits[1][0]))
        spec, params = hits[0]
        required = [
            n for n, p in inspect.signature(spec.handler).parameters.items()
            if n != "ctx" and p.default is inspect.Parameter.empty
        ]
        if any(n not in params for n in required):
            return Match(spec=None, params={}, confidence=1.0, clarification=self._ask_missing_param(spec))
        return Match(spec=spec, params=params, confidence=1.0, clarification=None)

    # -- clarifying questions -------------------------------------------

    @staticmethod
    def _ask_low_confidence(text: str) -> str:
        return f'I\'m not sure what you mean by "{text.strip()}" -- could you rephrase that?'

    @staticmethod
    def _ask_ambiguous(top: SkillSpec, second: SkillSpec) -> str:
        return f'Did you mean "{top.name}" or "{second.name}"? Could you be more specific?'

    @staticmethod
    def _ask_missing_param(spec: SkillSpec) -> str:
        return f'I understood you want "{spec.name}", but not what to do it with -- could you say that again?'

    # -- parameter extraction --------------------------------------------

    @staticmethod
    def _param_names(spec: SkillSpec) -> list[str]:
        sig = inspect.signature(spec.handler)
        return [name for name in sig.parameters if name != "ctx"]

    @staticmethod
    def _trigger_tokens(spec: SkillSpec) -> set[str]:
        """First token of each of the skill's own examples. Derived from
        `examples` alone -- never a hardcoded skill/param name -- so a new
        skill's phrasing is picked up automatically."""
        triggers: set[str] = set()
        for example in spec.examples:
            tokens = _tokenize(example)
            if tokens:
                triggers.add(tokens[0])
        return triggers

    def _extract_params(self, text: str, spec: SkillSpec) -> dict[str, object] | None:
        """Returns the params dict, or None if extraction isn't confident
        enough (caller turns that into a clarifying question)."""
        param_names = self._param_names(spec)
        if not param_names:
            return {}
        if len(param_names) > 1:
            # No schema exists to confidently split unstructured text into
            # more than one slot -- ask rather than guess (see module
            # docstring). No M0 skill hits this branch.
            return None

        tokens = _tokenize(text)
        triggers = self._trigger_tokens(spec)
        i = 0
        while i < len(tokens) and (tokens[i] in triggers or tokens[i] in _STOPWORDS):
            i += 1
        remainder = [t for t in tokens[i:] if t not in _STOPWORDS]

        if not remainder or len(remainder) > 5:
            return None

        return {param_names[0]: " ".join(remainder)}
