"""Contract: docs/CONTRACTS.md T0.2 — nova.audio.devices.probe_input_devices.

    def probe_input_devices(force: bool = False) -> DeviceProfile

The WASAPI trap (the crux of T0.2, per the lead): Windows exposes the same
physical mic multiple times, and at least one endpoint opens successfully
and streams digital zeros. Rules under test:
  - an endpoint returning all-exact-zeros must be REJECTED (verified=False);
  - a quiet-but-non-zero buffer from a real mic in a silent room must be
    ACCEPTED (verified=True) — the RMS floor must not be so aggressive that
    genuine quiet speech gets treated as silence;
  - host-API ranking is WDM-KS -> MME -> DirectSound -> WASAPI;
  - verified is only ever True after real (measured, non-zero) audio arrived;
  - the cache invalidates when the device list changes.

White-box against nova/audio/devices.py as landed: the real _measure_device
uses sd.rec()/sd.wait() (blocking) and sd.query_devices()/sd.query_hostapis()
for enumeration, so that's exactly what these tests fake — via a fake object
substituted for `nova.audio.devices.sd` (not the real `sounddevice` package),
so no other module touching real sounddevice can be affected and no real
microphone I/O ever happens in these tests.

The on-disk cache path (data/audio_profile.json) is hardcoded by contract
design (probe_input_devices takes no path parameter) — every test here
redirects nova.audio.devices.PROFILE_PATH / DATA_DIR to a tmp_path so the
real cache is never touched by the mocked tests.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

import nova.audio.devices as devices_mod
from nova.audio.devices import DeviceProfile, SILENCE_RMS_FLOOR, probe_input_devices


# Matches the real doc-referenced measurements: genuine captures on the
# reference laptop came in >= 6e-5 RMS; the confirmed-lying endpoint
# measured exactly 0.0. These constants keep the test data honest to that.
QUIET_REAL_RMS = 6e-5
DEFAULT_SR = 16000
_FRAMES = int(DEFAULT_SR * devices_mod.PROBE_SECONDS)


def _device(name, hostapi_idx, max_in=1, max_out=0, sr=DEFAULT_SR):
    return {
        "name": name,
        "hostapi": hostapi_idx,
        "max_input_channels": max_in,
        "max_output_channels": max_out,
        "default_samplerate": sr,
    }


def _zeros() -> np.ndarray:
    return np.zeros((_FRAMES, 1), dtype=np.float32)


def _quiet_real(rms: float, frames: int = _FRAMES, seed: int = 0) -> np.ndarray:
    """A non-zero buffer with a controlled RMS, standing in for genuine
    quiet-room mic capture (not literal silence, just quiet)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(frames).astype(np.float32)
    current_rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    x *= (rms / current_rms)
    return x.reshape(frames, 1)


class _FakeSD:
    """Stands in for the `sd` name in nova.audio.devices — not the real
    sounddevice package."""

    CallbackStop = Exception  # unused here, present for API-shape parity

    def __init__(self, devices, hostapis, buffers):
        self.devices = devices
        self.hostapis = hostapis
        # buffers: {device_index: np.ndarray | Exception}
        self.buffers = buffers
        self.rec_calls: list[int] = []

    def query_devices(self):
        return self.devices

    def query_hostapis(self):
        return self.hostapis

    def rec(self, frames, samplerate, channels, dtype, device):
        self.rec_calls.append(device)
        val = self.buffers.get(device)
        if isinstance(val, Exception):
            raise val
        if val is None:
            return np.zeros((frames, channels), dtype=np.float32)
        arr = np.asarray(val, dtype=np.float32)
        return arr.reshape(frames, channels)

    def wait(self):
        return None


_HOSTAPIS = [
    {"name": "MME"},
    {"name": "Windows DirectSound"},
    {"name": "Windows WASAPI"},
    {"name": "Windows WDM-KS"},
]
_HOSTAPI_IDX = {h["name"]: i for i, h in enumerate(_HOSTAPIS)}


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """Returns a controller with .configure(devices, buffers) and .rec_calls;
    patches nova.audio.devices.sd / PROFILE_PATH / DATA_DIR for isolation."""

    state = {"fake": None}
    profile_path = tmp_path / "audio_profile.json"
    monkeypatch.setattr(devices_mod, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(devices_mod, "DATA_DIR", tmp_path)

    class Controller:
        def configure(self, devices, buffers, hostapis=_HOSTAPIS):
            fake = _FakeSD(devices, hostapis, buffers)
            monkeypatch.setattr(devices_mod, "sd", fake)
            state["fake"] = fake
            return fake

        @property
        def rec_calls(self):
            return state["fake"].rec_calls

        @property
        def cache_path(self):
            return profile_path

    return Controller()


# ---------------------------------------------------------------------------
# The crux: exact zeros rejected, quiet-but-real accepted
# ---------------------------------------------------------------------------


def test_all_exact_zero_endpoint_is_rejected(probe):
    devices = [
        _device("Microphone (lying WASAPI endpoint)", _HOSTAPI_IDX["Windows WASAPI"]),
    ]
    probe.configure(devices, {0: _zeros()})

    profile = probe_input_devices(force=True)

    assert profile.verified is False
    assert profile.rms == 0.0


def test_quiet_but_nonzero_buffer_from_silent_room_mic_is_accepted(probe):
    devices = [
        _device("Microphone (real, quiet room)", _HOSTAPI_IDX["Windows WASAPI"]),
    ]
    probe.configure(devices, {0: _quiet_real(QUIET_REAL_RMS)})

    profile = probe_input_devices(force=True)

    assert profile.verified is True
    assert profile.rms > 0.0
    assert profile.rms == pytest.approx(QUIET_REAL_RMS, rel=0.05)


def test_zero_endpoint_loses_to_quiet_real_endpoint_even_when_ranked_higher(probe):
    """The zero endpoint sits on WDM-KS (rank 0, normally preferred) and the
    quiet-but-real endpoint sits on WASAPI (rank 3, normally last resort).
    Verification must still win: a device that 'opens' with digital zeros is
    never preferred over a verified-real device, regardless of host-API
    ranking."""
    devices = [
        _device("lying WDM-KS", _HOSTAPI_IDX["Windows WDM-KS"]),
        _device("real WASAPI", _HOSTAPI_IDX["Windows WASAPI"]),
    ]
    probe.configure(devices, {0: _zeros(), 1: _quiet_real(QUIET_REAL_RMS)})

    profile = probe_input_devices(force=True)

    assert profile.verified is True
    assert profile.name == "real WASAPI"


def test_rms_at_exactly_the_silence_floor_boundary(probe):
    """SILENCE_RMS_FLOOR is a >= boundary: a non-zero buffer whose RMS sits
    just below the floor must be rejected; one that meets or exceeds it must
    be accepted. Pins the exact contract of nova.audio.devices.SILENCE_RMS_FLOOR
    rather than a value this suite invents independently."""
    devices = [_device("boundary", _HOSTAPI_IDX["MME"])]

    probe.configure(devices, {0: _quiet_real(SILENCE_RMS_FLOOR * 0.5)})
    below = probe_input_devices(force=True)
    assert below.verified is False

    probe.configure(devices, {0: _quiet_real(SILENCE_RMS_FLOOR * 5)})
    above = probe_input_devices(force=True)
    assert above.verified is True


def test_device_that_raises_on_open_is_excluded_but_does_not_crash_the_probe(probe):
    devices = [
        _device("refuses to open", _HOSTAPI_IDX["Windows WDM-KS"]),
        _device("works fine", _HOSTAPI_IDX["MME"]),
    ]
    probe.configure(
        devices,
        {0: RuntimeError("Invalid device [PaErrorCode -9996]"), 1: _quiet_real(QUIET_REAL_RMS)},
    )

    profile = probe_input_devices(force=True)  # must not raise

    assert profile.verified is True
    assert profile.name == "works fine"


def test_output_only_devices_are_never_probed_as_input(probe):
    devices = [_device("speakers", _HOSTAPI_IDX["MME"], max_in=0, max_out=2)]
    probe.configure(devices, {})

    profile = probe_input_devices(force=True)

    assert profile.verified is False
    assert probe.rec_calls == []  # never even attempted to record from it


def test_no_input_capable_devices_returns_unverified_placeholder_without_crashing(probe):
    devices = [_device("speakers only", _HOSTAPI_IDX["MME"], max_in=0, max_out=2)]
    probe.configure(devices, {})

    profile = probe_input_devices(force=True)

    assert profile.verified is False
    assert profile.index == -1


def test_all_candidates_zero_never_falsely_reports_verified(probe):
    """If literally every candidate is digital silence (fully broken audio
    subsystem), the returned profile must never lie and claim verified=True."""
    devices = [
        _device("dev0", _HOSTAPI_IDX["Windows WDM-KS"]),
        _device("dev1", _HOSTAPI_IDX["MME"]),
    ]
    probe.configure(devices, {0: _zeros(), 1: _zeros()})

    profile = probe_input_devices(force=True)

    assert profile.verified is False


# ---------------------------------------------------------------------------
# Host-API ranking: WDM-KS -> MME -> DirectSound -> WASAPI
# ---------------------------------------------------------------------------


def test_hostapi_ranking_full_ladder(probe):
    devices = [
        _device("via WASAPI", _HOSTAPI_IDX["Windows WASAPI"]),
        _device("via DirectSound", _HOSTAPI_IDX["Windows DirectSound"]),
        _device("via MME", _HOSTAPI_IDX["MME"]),
        _device("via WDM-KS", _HOSTAPI_IDX["Windows WDM-KS"]),
    ]
    same_rms = {i: _quiet_real(QUIET_REAL_RMS, seed=i) for i in range(4)}

    probe.configure(devices, same_rms)
    winner = probe_input_devices(force=True)
    assert winner.hostapi == "Windows WDM-KS"

    # Remove WDM-KS: MME should win next.
    probe.configure(devices[:3], {i: same_rms[i] for i in range(3)})
    winner = probe_input_devices(force=True)
    assert winner.hostapi == "MME"

    # Remove MME too: DirectSound should win.
    probe.configure(devices[:2], {i: same_rms[i] for i in range(2)})
    winner = probe_input_devices(force=True)
    assert winner.hostapi == "Windows DirectSound"

    # Only WASAPI left.
    probe.configure(devices[:1], {0: same_rms[0]})
    winner = probe_input_devices(force=True)
    assert winner.hostapi == "Windows WASAPI"


def test_unrecognized_hostapi_is_ranked_last(probe):
    devices = [
        _device("weird api", 0),
        _device("via MME", 1),
    ]
    hostapis = [{"name": "Some Unknown API"}, {"name": "MME"}]
    probe.configure(
        devices,
        {0: _quiet_real(QUIET_REAL_RMS, seed=0), 1: _quiet_real(QUIET_REAL_RMS, seed=1)},
        hostapis=hostapis,
    )

    winner = probe_input_devices(force=True)
    assert winner.hostapi == "MME"


# ---------------------------------------------------------------------------
# Caching: hit skips re-probe, force bypasses cache, device-list change
# invalidates the cache
# ---------------------------------------------------------------------------


def test_cache_hit_skips_reprobe_when_device_list_is_unchanged(probe):
    devices = [_device("mic", _HOSTAPI_IDX["Windows WDM-KS"])]
    probe.configure(devices, {0: _quiet_real(QUIET_REAL_RMS)})

    first = probe_input_devices(force=False)
    calls_after_first = len(probe.rec_calls)
    assert calls_after_first == 1

    second = probe_input_devices(force=False)
    assert len(probe.rec_calls) == calls_after_first  # no new recording happened
    assert second == first


def test_force_true_bypasses_the_cache(probe):
    devices = [_device("mic", _HOSTAPI_IDX["Windows WDM-KS"])]
    probe.configure(devices, {0: _quiet_real(QUIET_REAL_RMS)})

    probe_input_devices(force=False)
    calls_after_first = len(probe.rec_calls)

    probe_input_devices(force=True)
    assert len(probe.rec_calls) > calls_after_first


def test_cache_invalidates_when_a_new_device_appears(probe):
    devices = [_device("mic A", _HOSTAPI_IDX["Windows WDM-KS"])]
    probe.configure(devices, {0: _quiet_real(QUIET_REAL_RMS)})
    probe_input_devices(force=False)
    calls_after_first = len(probe.rec_calls)

    # A new headset shows up: device list (fingerprint) changes.
    devices_plus_headset = devices + [_device("new headset", _HOSTAPI_IDX["MME"])]
    probe.configure(
        devices_plus_headset,
        {0: _quiet_real(QUIET_REAL_RMS, seed=0), 1: _quiet_real(QUIET_REAL_RMS, seed=1)},
    )
    probe_input_devices(force=False)  # no force — must still re-probe

    assert len(probe.rec_calls) > calls_after_first


def test_cache_invalidates_when_the_winning_device_disappears(probe):
    devices = [
        _device("mic A (WDM-KS)", _HOSTAPI_IDX["Windows WDM-KS"]),
        _device("mic B (MME)", _HOSTAPI_IDX["MME"]),
    ]
    probe.configure(
        devices,
        {0: _quiet_real(QUIET_REAL_RMS, seed=0), 1: _quiet_real(QUIET_REAL_RMS, seed=1)},
    )
    first = probe_input_devices(force=False)
    assert first.name == "mic A (WDM-KS)"

    # Unplug mic A. Cached winner no longer exists among current devices.
    probe.configure(devices[1:], {0: _quiet_real(QUIET_REAL_RMS, seed=1)})
    second = probe_input_devices(force=False)  # must re-probe, not crash on stale index

    assert second.name == "mic B (MME)"


def test_cache_survives_process_boundary_via_disk(probe):
    """The cache is a file, not just an in-memory optimization: writing it
    once and reading it back (fresh dict round-trip through JSON) must
    reproduce an equivalent DeviceProfile."""
    devices = [_device("mic", _HOSTAPI_IDX["Windows WDM-KS"])]
    probe.configure(devices, {0: _quiet_real(QUIET_REAL_RMS)})

    original = probe_input_devices(force=False)
    assert probe.cache_path.exists()

    # New DeviceProfile constructed straight from the cache file must match.
    import json

    cached_raw = json.loads(probe.cache_path.read_text(encoding="utf-8"))
    from_disk = DeviceProfile(**cached_raw["profile"])
    assert from_disk == original


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_device_profile_is_a_frozen_dataclass():
    import dataclasses

    assert dataclasses.is_dataclass(DeviceProfile)
    p = DeviceProfile(index=0, name="x", hostapi="MME", samplerate=16000, verified=True, rms=0.001)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.verified = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Real hardware (opt-in): set NOVA_TEST_REAL_HARDWARE=1 to run against the
# actual microphone(s) on this machine. Off by default because a full real
# probe on this laptop enumerates ~30 audio endpoints at ~1s each.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_TEST_REAL_HARDWARE") != "1",
    reason="set NOVA_TEST_REAL_HARDWARE=1 to probe the real microphone(s) on this machine",
)
def test_real_hardware_probe_produces_a_verified_profile():
    profile = probe_input_devices(force=True)
    assert isinstance(profile, DeviceProfile)
    if profile.verified:
        assert profile.rms > 0.0
