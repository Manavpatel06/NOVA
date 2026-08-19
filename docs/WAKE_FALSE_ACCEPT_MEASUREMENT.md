# Wake-word false-accept measurement — T1.6, F27

Owner: nova-dev-2. Companion to `nova/audio/wake.py` and `nova/config.py`'s
`WakeConfig.threshold` bounds (T1.5).

**Status update, read this first**: the run below was invalidated after the
fact — F36 (output device landing on an indirection layer) and F40 (input
prober selecting a loopback/monitor-capture device) were both still live
when it was taken, and either could have corrupted the acoustic numbers.
After both were fixed, a real false accept was found and confirmed
reproducible: **"hey jazz"**, an ordinary two-word phrase, cleared the old
0.55 default, then kept getting worse every time it was sampled again
(0.5125, then 0.6449, then 0.9321 across three separate sessions). See the
**"Final outcome (post-fix)"** section at the end for what that means and
what shipped. The measurement methodology and the direct-fed/acoustic
divergence finding below are still accurate and still the reason the final
numbers are trusted at all — only the specific acoustic scores in Result 2
predate the device fixes and should not be read as current.

Every number below came from:

```
D:\NOVA\.venv\Scripts\python.exe scripts\measure_wake_false_accept.py
```

Raw per-clip results (category, text, voice, peak score, and every acoustic
repeat trial): `data/wake_false_accept_measurement.json`.

## Why this exists

F27: `WakeConfig.threshold` (T1.5) enforces `> 0.0` and `<= 1.0` as
domain-correctness bounds, but deliberately does **not** enforce a real
security floor above zero. The three numbers available before this
measurement — 0.997 true-positive, 0.00005 on one control phrase, zero
detections across ~15s of noise — describe behavior *at the current default
(0.55)*, not the false-accept curve across the range someone would actually
lower the threshold into. nova-security's threat model
(`docs/THREAT_MODEL_ALWAYS_ON.md` §4) explicitly declined to invent a number
from those three points and asked for a real measurement instead. This is
that measurement.

## What was built

`WakeWord` (the real production detector — same ONNX sessions, same
pipeline) scored against ~180 Kokoro-synthesized clips across five
categories, plus a 10-phrase acoustic subset run through real speakers and
a real mic with 3 repeat trials each. See the script's own module docstring
for why reaching into `WakeWord`'s "private" methods (`_run_detection_step`,
`_warmup_locked`) is deliberate — it lets the harness capture the peak score
across a whole clip rather than only a threshold-crossing boolean, and reset
every clip to the same cold-boot-into-silence state `WakeWord.start()`
itself establishes (a real, previously-found bug: an unwarmed classifier
window scores everything near-zero for the first ~2.3s regardless of
content).

## Result 1: the direct-fed curve (no speakers, no mic)

134 genuine negative clips across four categories (phonetic near-misses,
other wake words, "hey"-sentences, ordinary speech), 6 Kokoro voices for the
adversarial categories. **42 additional clips were excluded from this
count** — phrases like "okay jarvis", "hey jarvi", "um hey jarvis" still
contain the literal wake keyword with filler or a dropped consonant; scoring
near the true positive (0.13–0.999) there is correct robustness, not a
false-accept, and folding them into the negative set would have made the
curve look worse than it is for the wrong reason.

True positive (6 voices): 0.9961–0.9988. Consistent with the original
0.997 measurement.

| Threshold | Genuine negatives that fire (of 134) |
|---:|---:|
| 0.01 | 58 |
| 0.10 | 40 |
| 0.25 | 26 |
| 0.40 | 15 |
| 0.45 | 9 |
| 0.50 | 6 |
| **0.55 (current default)** | **6** |
| 0.80 | 6 |
| 0.90 | 5 |
| 0.99 | 2 |

The 6 negatives that still fire at 0.55 are all in one sub-category:
deliberately-crafted nonsense syllables built to rhyme with "jarvis" —
**"hey carvis" (0.974–0.994 across 3 voices), "hey harvis" (0.44–0.98), "hey
jazz" (0.35–0.95)**. These are not real English words. Genuine plausible
name-confusions that *are* real words — "hey travis", "hey charlotte", "hey
darling", "hey jared" — all score 0.33–0.49, below the current default,
across every voice tested.

**Read this as**: digitally, the current default already separates ordinary
speech and plausible name-confusion from the wake phrase with real margin.
It does not, and by construction cannot, separate the wake phrase from a
syllable engineered to rhyme with it — some of those score statistically
indistinguishable from "hey jarvis" itself (0.994 vs. 0.996–0.999). No
threshold value fixes that; it is a property of the classifier's phonetic
discrimination, not the floor.

## Result 2: the acoustic subset overturns part of the direct-fed ranking

10 phrases, 3 repeat trials each, real speakers, real mic, one voice
(af_heart, matching the original T1.1 measurement).

**A methodology problem was found and fixed first.** The first acoustic run
constructed a fresh `Player()` per trial. Identical clips (same text, same
voice, seconds apart) swung from peak score 0.98 to 0.015 — not real-world
variance, an artifact of `Player`'s default output-device probe landing
inconsistently on "Microsoft Sound Mapper – Output" (an MME indirection
layer that resolves to whatever Windows' *current* default is at
stream-open time, not probe time — this machine's actual OS default is an
HDMI/TV endpoint, the same failure class as the already-documented F18).
Fixed by pinning one concrete physical device (`Speakers (Realtek(R)
Audio)`) and reusing one `Player` instance across all trials
(`_pick_stable_output_device` in the script). This is a real, separate
finding from F27 — flagged to the lead, not fixed beyond what this script
needed.

With that fixed, results (peak of 3 trials; full trial lists in the JSON):

| Phrase | Direct-fed | Acoustic (3 trials) | Acoustic peak |
|---|---:|---|---:|
| hey jarvis (TP) | 0.9985 | 0.9968, 0.9979, 0.9979 | 0.9979 |
| hey carvis | 0.9735 | 0.0243, 0.0066, 0.0592 | 0.0592 |
| hey harvis | 0.0870 | 0.0072, 0.0126, 0.0030 | 0.0126 |
| hey jazz | 0.8335 | 0.9738, 0.3489, 0.8164 | 0.9738 |
| hey travis | 0.4549 | 0.0201, 0.0064, 0.0137 | 0.0201 |
| **hey charlotte** | **0.0831** | **0.4088, 0.4370, 0.4382** | **0.4382** |
| hey google | 0.0001 | ~0.0002–0.0012 | 0.0012 |
| alexa | 0.00001 | ~0.0001–0.0006 | 0.0006 |
| hey how's it going | 0.0004 | ~0.0003–0.0007 | 0.0007 |
| turn on the lights | 0.00002 | ~0.0001–0.0002 | 0.0002 |

**The direct-fed ranking gets this wrong in both directions.** "Hey carvis"
looked like the single most dangerous negative digitally (0.97) and
acoustically turned out to be one of the safest (0.02–0.06, stable across 3
trials). "Hey charlotte" looked safe digitally (0.08) and acoustically
turned out stable and substantial (0.41–0.44 across 3 trials) — the
highest genuine acoustic negative measured, well above where a floor might
have been picked from the direct-fed curve alone, though still below the
current 0.55 default.

"Hey jazz" is the one case that stayed genuinely unresolved: even with the
device bug fixed, its three acoustic trials spread from 0.35 to 0.97 —
real trial-to-trial variance this measurement did not diagnose. Reported as
open, not smoothed over.

## What this does and doesn't support

**Supports**: nothing acoustically measured here crosses the current default
(0.55) — including "hey charlotte", the highest genuine acoustic negative
found, which peaked at 0.44. The current default is not contradicted by this
data.

**Does not support**: picking a new, tighter floor. Ten phrases, one voice,
one room, one session is not enough statistical power to derive one,
and the direct-fed-vs-acoustic divergence just demonstrated (in both
directions, on real words, not just adversarial nonsense) is itself
evidence that a much larger *acoustic-first* campaign — more phrases, more
trials, more rooms, more times of day, more mic positions — is needed
before a number derived from this kind of testing should be trusted for a
security decision. A number picked from the direct-fed curve alone,
before the acoustic check, would have been actively wrong: it would have
flagged the wrong phrase as the dangerous one.

**Does not support, and cannot, by construction**: any claim about
protection against a deliberately crafted phonetic near-rhyme ("hey
carvis"-class attacks). Some of those score statistically indistinguishable
from the true positive digitally; whether that survives acoustic
conditions in general (as opposed to specifically not surviving it for the
three examples measured here) is unknown. This is a classifier-capability
question, not a threshold-tuning one — it strengthens, not weakens, the
case in `docs/THREAT_MODEL_ALWAYS_ON.md` for F28 (tier-capping acoustic-only
arming) rather than trusting the threshold as the access-control mechanism
on its own.

## Final outcome (post-fix)

The recommendation immediately below this heading was written before F36
and F40 landed and is kept for the record; it is superseded by this
section.

Once F36 (output landing on an indirection layer) and F40 (input prober
selecting a loopback device) were both fixed, `scripts/measure_wake_false_
accept.py` was re-run — the fourth attempt, after two earlier invalidations
each traced to a different bug (F37: direct-fed scoring inverting the
danger ranking; F39: the output indirection layer; F40: the loopback
input). That run passed its own positive control ("hey jarvis" 0.9960–
0.9967) and found a real false accept: **"hey jazz"** peaked at 0.6449
across 3 acoustic trials, above the 0.55 default.

A floor of 0.70 (default 0.75) was derived from that single run's peak —
and immediately refuted by the next one. Before committing, the same
measurement was re-run independently and "hey jazz" peaked at **0.9321**;
six dedicated follow-up trials of that phrase alone gave a 0.466–0.924
range, two of six still above 0.75. **The observed ceiling rose every
single time anyone sampled more: 0.5125, then 0.6449, then 0.9321 across
three separate sessions.** That is the signature of a right tail this
project cannot bound by sampling harder — a floor derived from an observed
maximum will keep being exceeded by the next run, and will look reasonable
right up until it doesn't. Raising the floor further to chase "hey jazz"
was considered and rejected on that basis, not attempted and abandoned.

A second, independent reason rules out chasing a higher floor (recorded as
**F41**): every true-positive score in this entire measurement, including
the 0.996–0.999 quoted throughout this document, is Kokoro-synthetic — a
TTS voice through speakers into a mic. No human has ever spoken to this
detector. Pushing the floor higher to try to clear "hey jazz" risks landing
on a threshold Manav's own real voice can't reliably clear either, with no
way to measure that risk before he actually tries it.

**Shipped**: `WakeConfig.threshold` default raised to **0.75**, floor
enforced at **0.70** (`nova/config.py`'s `Bounds`) — explicitly labelled in
the code as **defence in depth, not a closed floor**. It roughly halves
the observed false-accept exposure (of the six dedicated "hey jazz" trials,
3 fire at 0.55, 2 still fire at 0.75) without claiming to close it.

**F27's real resolution is F28, not this number.** This measurement is now
the strongest concrete argument for capping acoustic-only arming below ACT
tier: if a wake-armed action can't reach ACT, a false accept costs a
spurious listening state, not a launched process — a containable failure,
unlike a threshold that holds until it doesn't. Put to Manav in those terms
rather than resolved unilaterally in code.

---

## Recommendation (superseded — kept for the record, see above)

**F27 stays open.** The current default (0.55) is not shown to be unsafe by
anything measured here; nothing supports moving it, in either direction,
with any confidence. `scripts/measure_wake_false_accept.py` and this
machine's audio-device fix are reusable infrastructure for whoever runs the
larger acoustic campaign that would actually be needed to close F27 with a
number. Until then, "no defensible floor is derivable yet, and here is the
evidence for why" is the honest state of F27 — not a placeholder for "we
didn't get to it."
