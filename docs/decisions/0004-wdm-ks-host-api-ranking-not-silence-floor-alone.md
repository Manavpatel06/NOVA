# ADR 0004: Rank candidate mics by host API, not by RMS/silence-floor alone

**Status:** decided, implemented in `nova/audio/devices.py` (`HOSTAPI_PRIORITY`, `_rank`, `_choose_winner`).

## Context

`probe_input_devices()` records ~1s of real audio from every input-capable device and must pick one winner. The obvious rule — reject exact digital zero, then take whichever survivor has the highest RMS — is not enough on this hardware, and the reason is specific, not hypothetical:

- Device 17 (WDM-KS, "Microphone (Realtek HD Audio Mic input)") is a **lying endpoint**: it opens successfully and returns exact `0.0` on every sample, 3/3 trials. A zero check alone catches this one.
- The **same physical microphone**, exposed again through WASAPI, MME, and DirectSound, is not lying — it returns real signal, but at ~1e-4 RMS, systematically squashed by the Windows audio engine's AGC and noise suppression (APOs) that WDM-KS bypasses by talking to the driver directly. That's **non-zero**, so it passes any all-zero test cleanly, and it is quiet enough to be functionally useless for speech recognition. A device that "works" by a naive zero check can still be the wrong one to pick.
- The genuine, unsuppressed signal (WDM-KS idx 22) measures roughly two to three orders of magnitude louder (RMS in the 0.05–0.07 range across several probe runs — see `docs/TASKS.md`'s measured-facts table for why this number itself isn't fixed) than its own AGC-suppressed duplicates on other host APIs.

Ranking by RMS alone, across all survivors regardless of host API, is only safe if the suppressed duplicates always lose on RMS to the real device — true on this hardware today, but not a property the code should depend on, since AGC behavior and comparative loudness aren't guaranteed to hold on a different machine or after a Windows Update changes APO defaults.

## Decision

Rank survivors first by host API tier (`WDM-KS: 0, MME: 1, DirectSound: 2, WASAPI: 3` — lower wins), and use RMS only as the tiebreak *within* a tier, not as the primary sort key across all candidates. WDM-KS wins by construction whenever a verified WDM-KS candidate exists, regardless of whether some other host API's suppressed duplicate happens to measure louder on a given trial.

## Why

The AGC/noise-suppression gap between WDM-KS and everything else is **systematic**, not incidental — it's an architectural property of how Windows' audio engine sits between the app and WASAPI/MME/DirectSound endpoints, and WDM-KS is specifically the API that bypasses that engine. Encoding that as a host-API preference makes the selection robust to *how much* quieter the suppressed duplicates measure on any given probe run; encoding it as "just pick highest RMS" makes the selection only accidentally correct, dependent on the suppression always being severe enough to lose a straight RMS contest — which is the same class of assumption that let the original silence-floor check alone look sufficient until device 17 was actually measured.

## Consequences

- A device that opens fine but has a much *quieter* WDM-KS signal than a hypothetical louder WASAPI duplicate would still be selected, because the ranking trusts host-API tier over raw loudness. This hasn't been observed as a problem on this hardware, but it's the trade this rule makes deliberately — see `nova/audio/devices.py`'s module docstring for the "never trust the name or the host API alone" framing, which still applies to a device *within* the same tier.
- This ranking is only correct if it's re-applied on every real device-list query, never cached as a static index — see finding F1 in `docs/TASKS.md` (cached index isn't a stable identity across reboots/driver reloads; a device reorder can silently rebind capture to the wrong physical device even though the fingerprint and the ranking rule both still "work"). The ranking rule and the index-caching problem are two different concerns that must not be conflated: this ADR is about *which device wins*, not about *whether the cached winner is still the same device next time*.
