# Router confidence calibration — method, not numbers

Owner: nova-dev-core. Companion to `nova/brain/router.py` (read that module's
docstring first — this doc is the durable, standalone version of the same
story, kept here so it survives independently of the code and gets found by
whoever re-derives `config.brain.confidence_floor` /
`config.brain.ambiguity_margin` next). Referenced by finding F15 in
`docs/TASKS.md`.

## The one thing to preserve

**The method, not the numbers.** `confidence_floor=0.42` and
`ambiguity_margin=0.08` were measured against a two-skill registry
(`time`, `open_app`) in M0. They are correct for that registry and nothing
promises they stay correct once a third skill lands, especially one whose
vocabulary overlaps an existing skill's. Re-running the method below takes
minutes; trusting stale numbers produces a router that occasionally, subtly,
misroutes — the "confidence" the constitution demands has to be re-earned
each time the thing it's measuring changes.

## Why calibrate at all, rather than pick a threshold

The constitution's confidence-honesty rule ("below the confidence floor, NOVA
asks rather than answers as if certain") is only real if the floor actually
separates real matches from noise on this specific registry's vocabulary. A
threshold chosen by feel is decoration — it happens to work until a skill's
examples share more words with another skill's than expected, at which point
it silently stops meaning anything. Calibration makes the number defensible:
every clean-match score and every off-topic score that went into picking it
is reproducible by rerunning `scripts/verify_router.py`.

## The method

1. **Build a labelled phrase set.** For the current skill registry, write
   phrases in five categories:
   - Literal examples (copied verbatim from each skill's `examples`).
   - Paraphrases the registry has never seen verbatim (rephrase each skill's
     intent in your own words — this is the category that actually exercises
     the similarity function rather than just confirming exact-match works).
   - Filler-padded phrasings ("could you...", "...for me", "please...") —
     these are what stress-test whether shared function words inflate scores
     between *different* intents.
   - Single-word or very short utterances, where small token sets make
     Jaccard swing hard in either direction.
   - Off-topic / gibberish text: things no current skill should match,
     spanning plausible near-misses (a skill NOVA doesn't have yet, e.g.
     "what's the weather") and pure noise (a keyboard-mash string).

2. **Score every phrase against every skill**, using the router's actual
   scoring function (`nova.brain.router._similarity`) against each skill's
   best-matching example — not a hand-simulated approximation. This is what
   `scripts/verify_router.py`'s score table does; run it, don't reconstruct
   it by hand.

3. **Record two numbers from the table:**
   - `worst_clean` — the lowest score any phrase in the "should match"
     categories (literal, paraphrase, filler-padded, short) scored against
     its *correct* skill.
   - `best_offtopic` — the highest score any phrase in the off-topic/
     gibberish category scored against *any* skill.

   If `worst_clean <= best_offtopic`, the vocabulary genuinely overlaps and
   no single floor can separate them — that's a signal to fix the similarity
   function or the skill's examples, not to split the difference and hope.

4. **Set `confidence_floor` inside the gap `(best_offtopic, worst_clean)`**,
   biased toward `worst_clean` — i.e. closer to the clean-match side than the
   noise side. This means *more* borderline input gets a clarifying question
   rather than an accepted guess, which is the direction the constitution's
   confidence-honesty rule asks for: asking too often costs a follow-up
   question, guessing wrong costs trust and possibly launches the wrong
   process.

5. **Set `ambiguity_margin` small** — just large enough to catch genuine
   near-ties (two skills whose best-matching examples are both plausible
   readings of the input) without being so large it forces a clarification
   between two scores that are honestly just different. With a small
   registry there's little real cross-domain overlap to measure this
   against, so treat it as "catches floating-point-adjacent near-ties" until
   the registry is large enough to have real ambiguous pairs to calibrate
   against directly.

6. **Re-run whenever the registry changes materially** — a new skill, a
   skill's `examples` edited, or a new paraphrase category discovered in
   real usage. "Materially" is a judgment call; when in doubt, rerun — it's
   cheap.

## A pitfall found during M0 calibration, worth not rediscovering

An early version of the similarity function ran the character-level
(`difflib.SequenceMatcher.ratio()`) half of the score over raw sentences.
`"what's the weather today"` scored **0.433** against the `time` skill —
above a first-draft floor — purely because `"what's the "` is a long shared
prefix with `"what's the time"`. The words that actually distinguish the two
phrases (`weather`/`today` vs. `time`) never entered into it.

Fix, in two parts: WH-question words (`what`, `what's`, `when`, `where`,
`who`, `why`, `how`, `which`) joined the stopword list — they're near-
universal across *any* question and carry no skill-distinguishing signal —
and the character-ratio term now runs over the stopword-stripped, sorted
content tokens instead of the raw sentence, so shared filler can no longer
inflate similarity on its own. After the fix, the same phrase scores 0.136.

The general lesson: any string-similarity term that operates on raw text
(not a curated token set) is vulnerable to exactly this — shared function
words masquerading as shared meaning. Test with filler-padded phrasings
specifically; they're what surfaces it.

## M0's numbers, for reference (not a promise)

Measured against `{time, open_app}` via `scripts/verify_router.py`:

| | value |
|---|---|
| worst clean-match score | 0.487 |
| best off-topic/gibberish score | 0.235 |
| `confidence_floor` chosen | 0.42 |
| `ambiguity_margin` chosen | 0.08 |

## Tooling

`scripts/verify_router.py` is the reusable calibration script — not a one-off.
When adding a skill: extend the labelled phrase set with paraphrases of the
new skill's intent and at least one phrase that plausibly *could* be confused
with an existing skill, rerun the script, read the new `worst_clean` /
`best_offtopic` gap off its printed table, and update
`config.brain.confidence_floor` / `ambiguity_margin` in both
`nova/config.py`'s defaults and `config/nova.yaml` to match — one source of
truth, same rule as every other config value.
