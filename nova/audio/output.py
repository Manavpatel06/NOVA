"""Output device selection — the mirror of the input-side trap.

`devices.py` probes microphones empirically because a device that *opens* is
not a device that *works*. Playback inherited no such discipline: `Player`
took whatever Windows called the default output endpoint and trusted it.

That is a real failure on this hardware. The reference laptop exposes four
output endpoints — "Speakers (Realtek(R) Audio)", "Headphones (Realtek(R)
Audio)", and an Insignia television reached over NVIDIA HDMI — and the OS
default moves between them. nova-dev-audio observed the NVIDIA HDMI endpoint
as default while debugging the wake word; minutes later the lead observed
Realtek Headphones. If the default lands on an endpoint nobody is listening
to, NOVA synthesises perfectly and is inaudible, which reads to a human as a
broken assistant — the constitution's "NOVA must never go silently mute",
violated from the output side. Recorded as finding F18.

What this module can and cannot do, stated plainly:

  CAN     detect that the default is a display/HDMI endpoint while a local
          sound card exists, prefer the local one, and say so loudly.
  CAN     verify a chosen endpoint actually opens and accepts a buffer.
  CANNOT  verify anyone can hear it. There is no loopback from a speaker back
          into this process. Audibility needs a human and no probe will change
          that — see docs/EVIDENCE.md.

The narrow rule: trust the OS default *unless* it is a display endpoint and a
local one is available. Windows' jack detection is genuinely better than we are
at choosing between Speakers and Headphones — it knows what is physically
plugged in and we do not. The display-audio case is the one where the default
is reliably wrong for a voice assistant.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np
import sounddevice as sd

from nova import DATA_DIR
from nova.log import get_logger

_log = get_logger(__name__)

PROFILE_PATH = DATA_DIR / "output_profile.json"

#: Substrings identifying an endpoint whose audio leaves for a screen. Matched
#: case-insensitively against the device name. These are display-audio paths:
#: a monitor or a television, not a speaker anyone is sitting near.
#:
#: These are deliberately SHORT because **MME truncates device names to 31
#: characters**. This machine's television reports as
#: ``"NS-39D400NA14 (NVIDIA High Defi"`` — the full string "nvidia high
#: definition audio" is never present to match against, and a marker list
#: written from the full driver name silently matches nothing. Caught by
#: testing the detector against the real enumerated names rather than against
#: the names the driver documentation implies. Do not "tidy" these back to
#: their full forms.
DISPLAY_ENDPOINT_MARKERS: tuple[str, ...] = (
    "nvidia high defi",
    "amd high defi",
    "intel(r) display",
    "hdmi",
    "displayport",
)


@dataclass(frozen=True)
class OutputProfile:
    index: int
    name: str
    hostapi: str
    samplerate: int
    is_display_endpoint: bool
    verified: bool  # True only after the device really opened and accepted a buffer
    reason: str  # why this endpoint, in words, for the log and for a human


def is_display_endpoint(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in DISPLAY_ENDPOINT_MARKERS)


def _verify_opens(index: int, samplerate: int) -> bool:
    """Open the endpoint and hand it a short, effectively-silent buffer.

    Proves the device accepts a stream. It does NOT prove audibility, and
    nothing in this process can. Deliberately near-silent rather than truly
    zero: some drivers optimise a digital-zero write away entirely, which
    would make this check weaker than it looks.
    """
    try:
        frames = int(samplerate * 0.05)
        quiet = (np.random.default_rng(0).standard_normal(frames) * 1e-4).astype(np.float32)
        with sd.OutputStream(samplerate=samplerate, channels=1, dtype="float32", device=index) as stream:
            stream.write(quiet)
        return True
    except Exception as exc:
        _log.warning("Output device %d did not open: %s", index, exc)
        return False


def probe_output_device(force: bool = False) -> OutputProfile:
    """Choose an output endpoint, preferring a local sound card over a screen.

    Cached to data/output_profile.json keyed by the output device list, so
    plugging in a monitor or a headset forces a fresh decision rather than
    silently reusing a stale one. The cached index is re-resolved by
    (name, hostapi) on every call — finding F1's lesson, that an index is not
    an identity, applied to the output side before it can bite here too.
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    fingerprint = "|".join(
        f"{d['name']}:{d['max_output_channels']}"
        for d in devices
        if d["max_output_channels"] > 0
    )

    if not force and PROFILE_PATH.exists():
        try:
            cached = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                p = cached["profile"]
                for i, d in enumerate(devices):
                    if (
                        d["max_output_channels"] > 0
                        and d["name"] == p["name"]
                        and hostapis[d["hostapi"]]["name"] == p["hostapi"]
                    ):
                        return OutputProfile(**{**p, "index": i})
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
            _log.warning("output_profile.json unreadable (%s); re-probing", exc)

    try:
        default = sd.query_devices(kind="output")
        default_index = int(default["index"])
    except Exception as exc:
        _log.warning("No default output device available: %s", exc)
        return OutputProfile(-1, "<none>", "<none>", 16000, False, False, f"no default: {exc}")

    default_name = str(default["name"])
    chosen_index = default_index
    chosen_reason = "OS default endpoint"

    if is_display_endpoint(default_name):
        # Prefer a local sound card, staying within the default's host API
        # where possible so we do not cross into an API with its own problems.
        local = [
            (i, d)
            for i, d in enumerate(devices)
            if d["max_output_channels"] > 0 and not is_display_endpoint(d["name"])
        ]
        same_api = [(i, d) for i, d in local if d["hostapi"] == default["hostapi"]]
        candidates = same_api or local
        if candidates:
            chosen_index = candidates[0][0]
            chosen_reason = (
                f"OS default {default_name!r} is a display/HDMI endpoint; "
                "preferring a local sound card"
            )
            _log.warning(
                "Default output %r is a display/HDMI endpoint. NOVA would be inaudible through "
                "it unless you are listening to that screen. Using %r instead. If that is wrong, "
                "set the output device in Windows sound settings.",
                default_name,
                devices[chosen_index]["name"],
            )
        else:
            chosen_reason = "OS default is a display endpoint and no local endpoint exists"
            _log.warning(
                "Default output %r is a display/HDMI endpoint and this machine exposes no local "
                "sound card. NOVA's voice will go to that screen.",
                default_name,
            )

    chosen = devices[chosen_index]
    samplerate = int(chosen["default_samplerate"])
    verified = _verify_opens(chosen_index, samplerate)

    profile = OutputProfile(
        index=chosen_index,
        name=str(chosen["name"]),
        hostapi=hostapis[chosen["hostapi"]]["name"],
        samplerate=samplerate,
        is_display_endpoint=is_display_endpoint(str(chosen["name"])),
        verified=verified,
        reason=chosen_reason,
    )

    if verified:
        _log.info("Output device: %s (%s) — %s", profile.name, profile.hostapi, profile.reason)
    else:
        _log.warning(
            "Chose output %r but it did not accept a buffer. NOVA may be silent — run "
            "scripts/probe_output.py to see every endpoint.",
            profile.name,
        )

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(
            json.dumps({"fingerprint": fingerprint, "profile": asdict(profile)}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        _log.warning("Could not cache output profile: %s", exc)

    return profile
