# ADR 0011: pre-warm models at boot vs. stay lazy — priced, not decided

**Status: open. This is Manav's decision, not the team's — see
`docs/M2_OPEN_BRAIN_PLAN.md` T2.2, which asked for this trade to be written
down "with both numbers, and Manav decides," explicitly not chosen silently.**

## Context

NOVA lazy-loads `Transcriber` (Whisper `large-v3-turbo`) and `Synthesizer`
(Kokoro ONNX) on first real use, not at construction (`nova/app.py`'s own
docstring: "Construction is cheap and loads no models"). T2.2's measurement
(`docs/T2.2_LATENCY.md`) put a real number on what that costs: **the first
turn in a process is 4.50-4.83s of time-to-first-sound, vs. 1.07-1.10s
steady state** — a 3-4x slowdown, paid exactly once per process lifetime, on
whichever turn happens to be first.

Pre-warming (calling `Transcriber.load()`/`Synthesizer.load()` during
`NovaApp.start()`, before the hotkey is even armed) would move that cost from
"whenever Manav first speaks" to "during boot, before he's interacting with
anything." Not obviously free: F16 (`docs/decisions/0006-open-question-memory-does-not-return-to-baseline.md`)
already established that once these models have run once *in a process*,
host RSS does not return to idle for that process's remaining lifetime —
CTranslate2's caching allocator and (to a lesser extent) onnxruntime's own
allocator keep the memory rather than returning it to the OS.

## The two numbers, both measured on this machine

**Idle RAM cost, if pre-warmed** (F16, independently reproduced twice):

| State | RSS |
|---|---|
| Cold, before either model has run | ~22-59 MB |
| Whisper only, after one real transcription | ~735-770 MB |
| Whisper + Kokoro, both used at least once | ~850-900 MB |

**Latency benefit, if pre-warmed** (T2.2, this task, `docs/T2.2_LATENCY.md`):

| | time-to-first-sound |
|---|---|
| First turn, not pre-warmed | 4.50-4.83s |
| Steady state (pre-warmed or not — same number once warm) | 1.07-1.10s |
| **Benefit of pre-warming** | **~3.4-3.7s saved, on exactly one turn per process lifetime** |

## What pre-warming actually trades, precisely

This is narrower than "memory vs. speed" in general, for two reasons T2.2's
data makes concrete:

1. **The RAM cost is not "pre-warm vs. never pay it."** Per F16, the moment
   Manav uses NOVA even once in a session, RSS rises to the same ~850-900 MB
   floor and *stays there for the rest of that process's life* — pre-warming
   doesn't change the eventual floor, only *when* it's reached. The real
   question is narrower: **does NOVA pay ~790-840 MB above cold-idle
   during any stretch where it turns out Manav doesn't use it that
   session** (left running overnight, a boot where he doesn't end up
   talking to it) — pre-warming pays that unconditionally at boot; staying
   lazy pays it only if/when actually used.
2. **The latency benefit is a one-time discount on exactly one turn.**
   Every turn after the first already runs at steady-state speed (1.07-1.10s)
   whether or not the process pre-warmed — pre-warming cannot make turn #2
   onward any faster than they already measure. It only decides whether
   *turn #1* costs 4.5-4.8s (lazy) or ~1.1s (pre-warmed, assuming boot-time
   loading — Whisper ~2.9-3.6s + Kokoro ~1.3-1.5s of one-time load work,
   serial — finishes before Manav's first real utterance, which for a
   background-started assistant is the ordinary case).

## Why this is plausibly what Manav actually experienced

T2.2's first-turn total (4.50-4.83s) plus that reply's own ~1.6-1.8s of
speech is a 6.1-6.6s wall time to finish speaking — inside the "~5-8s, felt
slow" range from the 2026-08-20 test that opened this milestone. The turn
that felt slow was very likely a cold-load turn, which pre-warming directly
targets; steady state is already well inside the plan's 1.5s budget either
way.

## The trade, stated as a decision to make, not a recommendation

- **Pre-warm:** boot pays ~790-840 MB extra RSS unconditionally (on a 16 GB
  machine Manav also uses), and every process start does ~4.2s of serial
  model-loading work before the hotkey is meaningfully useful (loading can
  happen while the hotkey is still being armed / while Manav hasn't pressed
  it yet, so this is not necessarily *visible* delay, but it is real work
  happening). In exchange, the very first real turn of every NOVA session
  runs at steady-state speed (~1.1s) instead of 4.5-4.8s.
- **Stay lazy (current behavior):** boot stays at ~22-59 MB RSS and does no
  loading work until Manav actually speaks. In exchange, whichever turn
  happens to be first in a session — which, for an always-on assistant that
  may idle-unload per T1.2's `Governor`, could recur more than once per day,
  not just once ever — pays the 4.5-4.8s cost, felt as "slow," exactly the
  complaint that opened this milestone.

**Not decided here.** Both numbers are now real, not modelled, per T2.2's
instructions. Manav's call.
