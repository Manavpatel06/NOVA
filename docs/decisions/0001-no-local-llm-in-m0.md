# ADR 0001: No local LLM in M0/M1 — rules first, LLM later behind an interface

**Status:** decided (`docs/TASKS.md`, "Decisions taken — do not re-litigate"). Not yet revisited.

## Context

`docs/ARCHITECTURE.md`'s brain layer describes three tiers: a deterministic router, a local LLM, and the Claude Code arm. Only the first exists in code today (`nova/brain/router.py`). The team could have built a small local LLM into M0 to handle open-ended phrasing from the start.

## Decision

M0 ships with **no local LLM at all**. Intent routing is handled entirely by `nova/brain/router.py`'s deterministic, example-driven matcher: it scores input against every skill's declared `examples` using a blended Jaccard/character-similarity metric, with a calibrated confidence floor and ambiguity margin, and asks a clarifying question rather than guessing when neither is cleared. Open-ended language that doesn't match a skill's examples is not currently handled by anything smarter — it just gets a clarifying question.

## Why

- **Cost.** A local LLM is the single heaviest thing on the RAM/VRAM budget (`docs/ARCHITECTURE.md`'s resource table: brain-loaded state targets ~1.5 GB RSS / ~5.5 GB VRAM). Loading one before there's anything for it to meaningfully add over deterministic routing on a two-skill registry is spending the budget for no measured benefit.
- **Determinism first.** The constitution's confidence-honesty rule ("if a router/intent match is below the confidence floor or crosses domains, NOVA asks a clarifying question") is far easier to reason about, calibrate, and test against a deterministic scorer than against an LLM's own confidence signal, which is a separate, harder problem the team hasn't tackled yet.
- **The architecture already has the seam.** `nova/brain/router.py` is a self-contained module the rest of the turn loop (`nova/app.py`) calls through a narrow interface (`route(text) -> Match`). A local LLM can slot in later as a second tier behind that same seam without a rewrite — this was designed in from the start specifically so the decision to defer it wouldn't cost a redesign later.

## Consequences

- Anything not phrased close to a skill's `examples` gets a clarifying question, not a helpful paraphrase-understanding response. This is a real, current UX limitation, not a hidden one — see `README.md`'s "what actually works today" for how narrow M0's language coverage is.
- `docs/ROADMAP.md`'s M3 ("a brain worth talking to") is where this gets revisited: local LLM, lazy-loaded, for open language and natural replies, with memory and a humanizer layer built around it.
- Every skill's `examples` remaining the *only* source of truth for what it sounds like (no LLM prompt to also keep in sync) is a direct consequence of this decision, not a separate one — see `docs/ADDING_A_SKILL.md`.
