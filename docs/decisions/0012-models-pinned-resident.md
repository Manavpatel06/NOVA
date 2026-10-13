# ADR 0012 — models stay resident (governor ceiling raised to 2048 MB)

**Date:** 2026-10-13 · **Decided by:** Manav · **Applied by:** Cowork session (config-only change)

## Decision

`governor.rss_ceiling_mb` raised 900 → 2048, which is above the measured
"both models resident" footprint (~1150–1350 MB), so the governor never
unloads under normal operation. This settles two open items in the
direction Manav chose ("keep it on the GPU; don't churn my RAM/CPU"):

- **F21** — the ~8 MiB VRAM creep per model reload is avoided by not
  reloading. The leak itself is not fixed; it is simply never triggered.
  The finding stays open as a code issue, closed as an operational risk.
- **ADR 0011 (pre-warm vs idle RAM)** — resolved toward residency: after
  the first turn of a session, everything stays warm (~1.1 s replies).
  First turn still pays the one-time load (~5 s); pre-warm-at-launch is a
  possible follow-up, not decided here.

## Cost, stated honestly

- Host RSS settles ~1.15–1.35 GB while NOVA runs (vs ~0.9 GB retained even
  after unload — F16 showed unloading never returned to baseline anyway,
  so themarginal cost of residency is roughly 250–450 MB).
- VRAM holds ~2.4–2.5 GiB of the 8 GiB card while NOVA runs.

## For the team

- Add an explicit `governor.resident: true` knob instead of this
  ceiling-as-switch, and a test pinning "no unload happens below ceiling".
- Where inference runs today: Whisper = GPU (cuda/float16); Kokoro TTS and
  the wake word = CPU (onnxruntime CPU package, deliberate for the wake
  word). Moving Kokoro to GPU means onnxruntime-gpu + provider wiring +
  VRAM budget check — evaluate, don't assume.
- Manav's standing directive (2026-10-13): prefer proven open-source
  libraries/APIs over building from scratch where they make NOVA easier,
  faster or more accurate — subject to the constitution's privacy/egress
  rules. He also has Anthropic API credits now, so the Claude arm may use
  the API as an alternative to the `claude` CLI; decide per T2.4.
