"""Input device discovery and empirical probing.

The WASAPI trap (settled finding — do not re-derive): Windows exposes the
same physical microphone through several host APIs, and at least one of
those endpoints opens successfully and streams digital zeros. A device that
*opens* is not evidence it *works*. The only trustworthy signal is a real
recorded second of audio.

On the reference laptop this trap did not even live on WASAPI — it was a
WDM-KS endpoint named plainly "Microphone (Realtek HD Audio Mic input)"
that opened fine and returned exact 0.0 for every sample. See
scripts/probe_audio.py output in the T0.2 report for the full measured
table. The lesson generalizes: never trust the name or the host API alone,
always trust the measured samples.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass

import numpy as np
import sounddevice as sd

from nova import DATA_DIR, AUDIO_PROFILE_PATH
from nova.audio.player import is_playback_active
from nova.log import get_logger

_log = get_logger(__name__)

PROFILE_PATH = AUDIO_PROFILE_PATH

# Lower number = tried first. WDM-KS talks to the driver directly, bypassing
# the Windows audio engine (APOs, AGC, noise suppression) that on this
# hardware silently squashes real mic signal down to near-nothing on the
# WASAPI/MME/DirectSound endpoints for the same physical device. MME and
# DirectSound are old but honest. WASAPI goes last: on this hardware it is
# where "opens fine, delivers near-nothing" lives most often.
HOSTAPI_PRIORITY: dict[str, int] = {
    "Windows WDM-KS": 0,
    "MME": 1,
    "Windows DirectSound": 2,
    "Windows WASAPI": 3,
}

#: Substrings identifying an input device that is not a microphone at all —
#: it captures whatever the machine is currently *playing*, not what's in
#: the room. Matched case-insensitively against the device name, same shape
#: as `nova.audio.output.DISPLAY_ENDPOINT_MARKERS`/`is_display_endpoint`.
#:
#: F40 (nova-dev-2, chasing the false-accept measurement's all-zero acoustic
#: run): the cached profile had drifted to device "PC Speaker (Realtek HD
#: Audio 2nd output with SST)", a loopback-style capture endpoint, rms=0.235
#: verified=True. Windows exposes several of these ("Stereo Mix", "PC
#: Speaker (...2nd output...)", "What U Hear") as ordinary-looking input
#: devices with real input channels — structurally indistinguishable from a
#: microphone in the device-info dict (`max_output_channels` is 0 on their
#: own entry, same as a real mic; see the report for why the two structural
#: signals considered instead of name-matching don't work on this hardware:
#: neither "also has output channels" nor "also has a WASAPI/MME/
#: DirectSound capture entry" holds — the second one actively contradicts
#: this module's own settled WDM-KS-first finding above, since these
#: loopback devices AND this machine's genuine mic are both WDM-KS-only).
#: This is a *design* flaw, not bad luck: `_rank` sorts by RMS after
#: hostapi priority, and a loopback endpoint capturing system audio is
#: overwhelmingly the loudest thing available whenever anything is playing
#: — an acoustic measurement (which plays audio to test the mic) is
#: precisely the program most likely to trigger this.
#:
#: Why this matters more than "NOVA goes deaf": had the loopback endpoint
#: returned *usable* audio instead of the nan/garbage it happened to return
#: here, NOVA would have been listening to its own output — manufacturing
#: the self-echo loop the wake-word duplex gate (F26) exists to prevent,
#: from the input side, with no wake word or duplex logic involved at all.
#:
#: Truncation note (same lesson as output.py's DISPLAY_ENDPOINT_MARKERS,
#: re-learned here): MME truncates device names to 31 characters. A marker
#: only protects a name if it's near the start of the string — "pc speaker"
#: and "stereo mix" and "what u hear" all survive a 31-char truncation
#: (verified against this machine's real names). "2nd output" does NOT: "PC
#: Speaker (Realtek HD Audio 2nd output with SST)" truncates to "...HD
#: Audio 2n" at 31 chars, losing the marker entirely — it is kept in this
#: list only as a defense-in-depth net for hostapis that don't truncate
#: (WDM-KS/DirectSound/WASAPI show full names) or for a future device where
#: an earlier marker doesn't already catch it. Do not treat its presence
#: here as proof it protects MME the way the earlier markers do.
#:
#: Ceiling, stated plainly rather than left implicit: name-matching can only
#: exclude loopback devices whose names look like these. A loopback
#: endpoint under an unfamiliar name — a different vendor's driver, a
#: virtual-cable app, a future Windows build — will not match any marker
#: here and will still be ranked purely on RMS, which is exactly the
#: property that made this whole class of device dangerous in the first
#: place. This list closes the *known* instances, not the underlying
#: "ranked by loudness during playback" vulnerability — see
#: `probe_input_devices`'s playback-guard for the mitigation that targets
#: the underlying property instead of the names.
LOOPBACK_ENDPOINT_MARKERS: tuple[str, ...] = (
    "stereo mix",
    "pc speaker",
    "what u hear",
    "wave speaker",
    "mirroring",
    "loopback",
    "2nd output",
)


def is_loopback_endpoint(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in LOOPBACK_ENDPOINT_MARKERS)


PROBE_SECONDS = 1.0

# Below this RMS a recording is treated as indistinguishable from digital
# silence, same verdict as an exact all-zero buffer. Calibrated against real
# hardware on the reference laptop: float32 quantization dither sits near
# 1e-7; every genuine capture measured there (including the Windows-audio-
# engine-suppressed WASAPI/MME duplicates of the working mic) came in at
# >= 6e-5 RMS, and the confirmed lying endpoint measured exactly 0.0 on
# every trial. 1e-6 sits ~10x above the dither floor and ~60x below the
# smallest real signal observed, so a quiet room still passes: this floor
# only catches literal zero and near-machine-epsilon noise, not a merely
# quiet mic.
SILENCE_RMS_FLOOR = 1e-6


@dataclass(frozen=True)
class DeviceProfile:
    index: int
    name: str
    hostapi: str
    samplerate: int
    verified: bool  # True only after real captured audio contained non-zero samples
    rms: float  # measured during probe


@dataclass(frozen=True)
class _Measurement:
    index: int
    name: str
    hostapi: str
    samplerate: int
    rms: float
    all_zero: bool
    verified: bool
    error: str | None


def _fingerprint_devices(devices: list[dict]) -> str:
    """Stable fingerprint of the current device list. Changes whenever a
    device is plugged/unplugged (new headset, etc.), which forces a
    re-probe instead of trusting a stale cached winner."""
    keys = sorted(
        f"{d['name']}|{d['hostapi']}|{d['max_input_channels']}|{d['max_output_channels']}"
        for d in devices
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _measure_device(index: int, info: dict, hostapi_name: str) -> _Measurement:
    sr = int(info["default_samplerate"]) if info.get("default_samplerate") else 16000
    try:
        rec = sd.rec(
            int(sr * PROBE_SECONDS),
            samplerate=sr,
            channels=1,
            dtype="float32",
            device=index,
        )
        sd.wait()
        samples = rec.flatten()
        all_zero = bool(np.all(samples == 0.0))
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        verified = (not all_zero) and rms >= SILENCE_RMS_FLOOR
        return _Measurement(index, info["name"], hostapi_name, sr, rms, all_zero, verified, None)
    except Exception as exc:  # device claims input channels but refuses to open/stream
        _log.warning("Probe failed to open device %d (%s): %s", index, info["name"], exc)
        return _Measurement(index, info["name"], hostapi_name, sr, 0.0, True, False, str(exc))


def enumerate_and_measure(
    devices: list[dict] | None = None,
    hostapis: list[dict] | None = None,
) -> list[_Measurement]:
    """Empirically probe every input-capable device. Records ~1s of real
    audio from each and measures it — never trusts that opening succeeded.

    Accepts an already-queried device/hostapi list so callers that also
    need those lists for other purposes (probe_input_devices, below) use
    one consistent snapshot instead of querying PortAudio twice and risking
    the two queries disagreeing. Defaults to querying fresh when omitted,
    for standalone callers like scripts/probe_audio.py.
    """
    if devices is None:
        devices = sd.query_devices()
    if hostapis is None:
        hostapis = sd.query_hostapis()
    results: list[_Measurement] = []
    for idx, info in enumerate(devices):
        if info.get("max_input_channels", 0) <= 0:
            continue
        hostapi_name = hostapis[info["hostapi"]]["name"]
        # F40: excluded BEFORE measurement, not after ranking -- a loopback
        # endpoint must never even become a candidate _rank() can compare,
        # since RMS ranking is exactly the property that makes these
        # dangerous (loudest-wins, and a loopback capturing system audio is
        # overwhelmingly the loudest thing available whenever anything is
        # playing). Loud, not silent: named explicitly so a human reading
        # the log can see what was excluded and why, same as
        # nova.audio.output's display-endpoint exclusion.
        if is_loopback_endpoint(info["name"]):
            _log.warning(
                "Excluding input device %d (%r, %s) — matches a known loopback/"
                "monitor-capture name pattern (F40); it would capture system "
                "audio, not the room.",
                idx, info["name"], hostapi_name,
            )
            continue
        results.append(_measure_device(idx, info, hostapi_name))
    return results


def _resolve_current_index(
    name: str, hostapi: str, devices: list[dict], hostapis: list[dict]
) -> int | None:
    """Find a device's CURRENT index by (name, hostapi) identity.

    nova-security (T0.2 fix): PortAudio/Windows device indices are not a
    stable identity across reboots, driver reloads, or a Windows Update
    touching the audio stack. `_fingerprint_devices` sorts before hashing,
    so it proves "the same set of devices is present" and says nothing
    about which position each one occupies -- a cached index can silently
    point at a different device after pure reordering, with the fingerprint
    still matching. Never hand back a cached index without re-resolving it
    against a freshly queried device list first.

    If the match is ambiguous (two devices with the identical name on the
    identical hostapi, e.g. two identical USB mics), the first one found
    wins. That's a real edge case but a secondary one -- not resolved
    further here.
    """
    for idx, info in enumerate(devices):
        if info.get("max_input_channels", 0) <= 0:
            continue
        hostapi_name = hostapis[info["hostapi"]]["name"]
        if info["name"] == name and hostapi_name == hostapi:
            return idx
    return None


def _rank(m: _Measurement) -> tuple[int, float]:
    priority = HOSTAPI_PRIORITY.get(m.hostapi, 99)
    return (priority, -m.rms)  # lower priority number first, then higher rms first


def _choose_winner(measurements: list[_Measurement]) -> _Measurement | None:
    verified = [m for m in measurements if m.verified]
    if verified:
        return sorted(verified, key=_rank)[0]
    # Nothing verified: degrade loudly, but still hand back a best-effort
    # candidate (highest RMS among everything that at least opened) so
    # callers relying on the return value don't crash. verified stays False.
    opened = [m for m in measurements if m.error is None]
    if opened:
        return sorted(opened, key=lambda m: -m.rms)[0]
    return None


def _load_cache() -> dict | None:
    if not PROFILE_PATH.exists():
        return None
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("audio_profile.json cache is unreadable (%s); re-probing", exc)
        return None


def _save_cache(fingerprint: str, profile: DeviceProfile, all_measurements: list[_Measurement]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "probed_at": time.time(),
        "profile": asdict(profile),
        "candidates": [asdict(m) for m in all_measurements],
    }
    PROFILE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _patch_cached_index(cached: dict, new_index: int) -> None:
    """Self-heal the on-disk cache after resolving index drift (no
    re-measurement needed -- same device, just a different position), so a
    human inspecting data/audio_profile.json directly doesn't see a stale
    index that every real caller already ignores."""
    cached["profile"]["index"] = new_index
    try:
        PROFILE_PATH.write_text(json.dumps(cached, indent=2), encoding="utf-8")
    except OSError as exc:
        _log.warning("Could not self-heal audio_profile.json's drifted index: %s", exc)


def probe_input_devices(force: bool = False) -> DeviceProfile:
    """Return the best verified working input device, probing empirically
    if needed. Cached in data/audio_profile.json, keyed by a fingerprint of
    the current device list so plugging in a new headset forces a re-probe.

    The fingerprint's only job is deciding whether to re-measure (expensive:
    ~1s of real audio per candidate device). It is NOT used as proof that a
    cached index still points at the right device -- indices can reorder
    without changing the fingerprint (see _resolve_current_index). Index
    resolution happens fresh on every call, cache hit or miss.
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    fingerprint = _fingerprint_devices(devices)

    if not force:
        cached = _load_cache()
        if cached and cached.get("fingerprint") == fingerprint and cached.get("profile"):
            p = cached["profile"]
            if is_loopback_endpoint(p["name"]):
                # F40 self-heal: a cache written before this fix landed (or
                # by a build that hasn't picked it up yet) can already hold
                # a poisoned loopback winner. A matching fingerprint is not
                # permission to keep trusting it -- treat this like the
                # "cached device not found" case below and re-probe for
                # real, so the fix applies to an existing bad cache, not
                # only to future ones.
                _log.warning(
                    "Cached input device %r on %r matches a known loopback/"
                    "monitor-capture name pattern (F40) -- this cache predates "
                    "the fix or was written by one without it. Re-probing "
                    "instead of trusting it.",
                    p["name"], p["hostapi"],
                )
                # fall through to a full re-probe
            else:
                current_index = _resolve_current_index(p["name"], p["hostapi"], devices, hostapis)
                if current_index is not None:
                    if current_index != p["index"]:
                        _log.info(
                            "Device index drifted from %d to %d for %r on %r "
                            "(same device, different position) -- resolved fresh, "
                            "self-healing the cache.",
                            p["index"], current_index, p["name"], p["hostapi"],
                        )
                        _patch_cached_index(cached, current_index)
                    return DeviceProfile(**{**p, "index": current_index})
                _log.warning(
                    "Cached device %r on %r not found at any index despite a "
                    "matching fingerprint; re-probing instead of trusting a "
                    "stale cached index.",
                    p["name"], p["hostapi"],
                )
                # fall through to a full re-probe

    _log.info("Probing input devices (fingerprint changed, cache miss, or force=True)...")

    # F40, mitigation for the underlying property (not just the known
    # names): RMS ranking is corrupted by definition while anything is
    # playing through this process's own Player -- a loopback/monitor
    # endpoint under a name not in LOOPBACK_ENDPOINT_MARKERS, or even a
    # legitimate mic sitting near a speaker, becomes the loudest candidate
    # purely because something is audibly playing. Markers close the known
    # instances; this closes the underlying vulnerability for the unknown
    # ones -- by refusing to let a probe taken under those conditions
    # poison the persistent cache, not by blocking or delaying the probe
    # itself (a probe that hangs waiting for silence is its own failure
    # mode, and the immediate caller still needs *an* answer now).
    probed_during_playback = is_playback_active()
    if probed_during_playback:
        _log.warning(
            "Input device probe is running while this process's own Player has "
            "an audio stream open (F40) -- RMS-based ranking is not trustworthy "
            "under these conditions. This probe's result will be returned but "
            "NOT cached, so it cannot poison future calls; the next probe that "
            "runs while nothing is playing will get the real answer."
        )

    # winner.index below comes directly from enumerating this exact
    # `devices` snapshot (enumerate_and_measure loops `enumerate(devices)`),
    # the same one just fingerprinted above -- it cannot be stale, no
    # separate resolution step needed on this path.
    measurements = enumerate_and_measure(devices, hostapis)
    winner = _choose_winner(measurements)

    if winner is None:
        _log.warning(
            "No input device could even be opened. Returning an unusable "
            "placeholder profile; NOVA has no microphone."
        )
        profile = DeviceProfile(index=-1, name="<none>", hostapi="<none>", samplerate=16000,
                                 verified=False, rms=0.0)
        if not probed_during_playback:
            _save_cache(fingerprint, profile, measurements)
        return profile

    if not winner.verified:
        _log.warning(
            "No candidate passed verification (real non-silent audio). "
            "Falling back to best-effort device %d (%s) on %s with rms=%.8f "
            "— treat this as unverified and suspicious.",
            winner.index, winner.name, winner.hostapi, winner.rms,
        )

    profile = DeviceProfile(
        index=winner.index,
        name=winner.name,
        hostapi=winner.hostapi,
        samplerate=winner.samplerate,
        verified=winner.verified,
        rms=winner.rms,
    )
    if probed_during_playback:
        _log.warning(
            "Not caching device %d (%s) — selected while playback was active "
            "(F40); treat this result as this call's best effort only.",
            profile.index, profile.name,
        )
    else:
        _save_cache(fingerprint, profile, measurements)
    _log.info(
        "Selected input device %d (%s) hostapi=%s sr=%d verified=%s rms=%.8f%s",
        profile.index, profile.name, profile.hostapi, profile.samplerate,
        profile.verified, profile.rms,
        " [NOT CACHED -- probed during playback, see WARNING above]" if probed_during_playback else "",
    )
    return profile
