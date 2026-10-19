# ADR 0014 — Record through the Windows audio engine (MME) first; WDM-KS last

**Date:** 2026-10-14 · **Supersedes:** the host-API order in ADR 0004 (its zero-endpoint and RMS-tiebreak reasoning still stands)

## Evidence (scripts/mic_check.py, Manav saying "Nova, what time is it?", 5 s per endpoint)

| idx | host API | speech peak RMS | voice-to-room | frames called speech |
|---|---|---|---|---|
| 0 | MME (Sound Mapper) | 0.054 | 73 dB | 25% |
| 1 | MME mic array | 0.061 | 66 dB | 75% |
| 12 | WASAPI mic array | 0.037 | very high | 25% |
| 17 | WDM-KS mic | 0 (dead endpoint) | — | 0% |
| 22 | WDM-KS mic array | 0.034 | 24 dB | 54% |

The engine paths are no longer squashed (ADR 0004's August premise) and are
cleaner than raw WDM-KS, whose ~0.002 noise floor the others don't have.
WDM-KS also bypasses the audio engine, so Windows' microphone indicator never
lit while NOVA listened, which Manav reasonably read as "the mic is off".

## Decision

`HOSTAPI_PRIORITY`: MME → DirectSound → WASAPI → WDM-KS. The cached device
choice (`data/audio_profile.json`) was moved aside on 2026-10-14 so the next
start re-probes under the new order.

## Also verified

Both device 22's and device 1's real recordings, replayed through the real
`Capture` class, the real VAD and the shipped `config/nova.yaml`, produce a
voice turn ("It's …") in an offline full-app simulation.
