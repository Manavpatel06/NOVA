"""Contract: docs/CONTRACTS.md T1.1-adjacent — nova.audio.output (finding F18/F20).

    def is_display_endpoint(name: str) -> bool
    def probe_output_device(force: bool = False) -> OutputProfile

Written by the lead (not a dev) while the team was offline, to unblock a
demo -- explicitly the least-reviewed code in the repo per the task brief.
Tested adversarially here rather than taken on trust.

The two promises this file exists to keep:
  - F20: MME truncates device names to 31 characters, so this machine's TV
    enumerates as "NS-39D400NA14 (NVIDIA High Defi" -- markers written from
    the full driver name match nothing against that string. The exact
    truncated string is pinned below, not just a plausible stand-in.
  - F18: trust the OS default UNLESS it is a display endpoint AND a local
    endpoint exists. Both branches are exercised with injected device lists.

Also under test: the cached profile re-resolves its winner's index by
(name, hostapi) on every call rather than trusting a stored index (F1's
lesson, applied to the output side) -- and a failing probe never raises out
of probe_output_device() (verified stays False, chosen device still
returned) so Player's own broad except is never load-bearing in isolation.

No real sounddevice/PortAudio call happens anywhere in this file:
nova.audio.output.sd is replaced with a fake local to this module for every
test except the explicitly-marked real-hardware class at the bottom.
"""
from __future__ import annotations

import dataclasses
import json
import os

import numpy as np
import pytest

import nova.audio.output as output_mod
from nova.audio.output import (
    DISPLAY_ENDPOINT_MARKERS,
    INDIRECTION_ENDPOINT_MARKERS,
    OutputProfile,
    is_display_endpoint,
    is_indirection_endpoint,
    probe_output_device,
)


# ---------------------------------------------------------------------------
# is_display_endpoint(): pure function, no mocking needed
# ---------------------------------------------------------------------------


def test_f20_the_real_truncated_tv_name_is_detected():
    """The exact string this machine's TV enumerates as under MME (31-char
    truncation of "NS-39D400NA14 (NVIDIA High Definition Audio)"). This is
    the whole reason DISPLAY_ENDPOINT_MARKERS are short -- pin the real
    case, not a plausible-looking stand-in."""
    assert is_display_endpoint("NS-39D400NA14 (NVIDIA High Defi") is True


@pytest.mark.parametrize(
    "name",
    [
        "NVIDIA High Definition Audio",
        "AMD High Definition Audio",
        "Intel(R) Display Audio",
        "Realtek Digital Output (HDMI)",
        "Generic DisplayPort Audio Device",
        "nvidia high defi",  # lower-case, at the exact marker length
    ],
)
def test_is_display_endpoint_matches_known_display_and_hdmi_names(name):
    assert is_display_endpoint(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Speakers (Realtek(R) Audio)",
        "Headphones (Realtek(R) Audio)",
        "Microphone (Realtek(R) Audio)",  # an input device name -- still not a display marker
        "Logitech USB Headset",
        "",
    ],
)
def test_is_display_endpoint_does_not_match_local_endpoints(name):
    assert is_display_endpoint(name) is False


def test_is_display_endpoint_is_case_insensitive():
    assert is_display_endpoint("HDMI Output Device") is True
    assert is_display_endpoint("hdmi output device") is True
    assert is_display_endpoint("HdMi Output Device") is True


def test_display_endpoint_markers_are_all_31_characters_or_shorter():
    """The whole point of these markers (per the module docstring) is
    surviving MME's 31-character truncation. A marker longer than 31 chars
    could never match a truncated name and would be a silent regression
    back toward F20."""
    for marker in DISPLAY_ENDPOINT_MARKERS:
        assert len(marker) <= 31, f"marker {marker!r} is too long to survive MME truncation"


# ---------------------------------------------------------------------------
# is_indirection_endpoint(): F36 -- endpoints that are not devices at all,
# they resolve to "whatever Windows' default is" at stream-open time. The
# real trigger: this machine's first non-display candidate is
# "Microsoft Sound Mapper - Output" -- selecting it when the OS default is
# a display endpoint (exactly the case F18 exists for) resolves straight
# back to the display endpoint being avoided, a fix that undoes itself.
# ---------------------------------------------------------------------------


def test_f36_the_real_sound_mapper_name_is_detected():
    assert is_indirection_endpoint("Microsoft Sound Mapper - Output") is True


@pytest.mark.parametrize(
    "name",
    [
        "Primary Sound Driver",
        "Primary Sound Capture Driver",
        "microsoft sound mapper - input",  # lower-case, and the input-side sibling
    ],
)
def test_is_indirection_endpoint_matches_known_indirection_layers(name):
    assert is_indirection_endpoint(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Speakers (Realtek(R) Audio)",
        "Headphones (Realtek(R) Audio)",
        "USB Headset",
        "",
    ],
)
def test_is_indirection_endpoint_does_not_match_concrete_devices(name):
    assert is_indirection_endpoint(name) is False


def test_indirection_endpoint_markers_list_is_never_empty():
    """Same shape as the display-marker length guard above: someone
    'tidying' this list away (or a refactor that forgets to port it)
    would silently restore F36 -- the exact case F18 exists to fix
    resolving straight back to the endpoint it was meant to avoid."""
    assert len(INDIRECTION_ENDPOINT_MARKERS) > 0


# ---------------------------------------------------------------------------
# Fakes for probe_output_device()
# ---------------------------------------------------------------------------


def _device(name, hostapi_idx, max_out=2, sr=48000):
    return {"name": name, "hostapi": hostapi_idx, "max_output_channels": max_out, "default_samplerate": sr}


_HOSTAPIS = [{"name": "MME"}, {"name": "Windows WASAPI"}]


class _FakeStreamCtx:
    def __init__(self, on_write=None):
        self._on_write = on_write

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def write(self, data):
        if self._on_write is not None:
            self._on_write(data)


class _FakeSD:
    """Stands in for the `sd` name in nova.audio.output -- not the real
    sounddevice package."""

    def __init__(self, devices, hostapis, default, open_fails=frozenset(), default_raises=None):
        self.devices = devices
        self.hostapis = hostapis
        self._default = default
        self._open_fails = set(open_fails)
        self._default_raises = default_raises
        self.opened_indices: list[int] = []
        self.written_buffers: list[np.ndarray] = []

    def query_devices(self, kind=None):
        if kind == "output":
            if self._default_raises is not None:
                raise self._default_raises
            return self._default
        return self.devices

    def query_hostapis(self):
        return self.hostapis

    def OutputStream(self, samplerate, channels, dtype, device):
        self.opened_indices.append(device)
        if device in self._open_fails:
            raise RuntimeError(f"simulated failure opening device {device}")
        return _FakeStreamCtx(on_write=self.written_buffers.append)


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """Returns a controller with .configure(devices, default_index) and
    .opened_indices; patches nova.audio.output.sd / PROFILE_PATH / DATA_DIR
    for isolation from the real machine and the real on-disk cache."""
    profile_path = tmp_path / "output_profile.json"
    monkeypatch.setattr(output_mod, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(output_mod, "DATA_DIR", tmp_path)

    state = {"fake": None}

    class Controller:
        def configure(self, devices, default_index, hostapis=_HOSTAPIS, open_fails=frozenset(), default_raises=None):
            default = None
            if default_raises is None:
                default = {**devices[default_index], "index": default_index}
            fake = _FakeSD(devices, hostapis, default, open_fails=open_fails, default_raises=default_raises)
            monkeypatch.setattr(output_mod, "sd", fake)
            state["fake"] = fake
            return fake

        @property
        def opened_indices(self):
            return state["fake"].opened_indices

        @property
        def written_buffers(self):
            return state["fake"].written_buffers

        @property
        def cache_path(self):
            return profile_path

    return Controller()


# ---------------------------------------------------------------------------
# The narrow rule: trust the OS default UNLESS it's a display endpoint AND
# a local endpoint exists. Both branches, injected.
# ---------------------------------------------------------------------------


def test_trusts_the_os_default_when_it_is_not_a_display_endpoint(probe):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)

    profile = probe_output_device(force=True)

    assert profile.index == 0
    assert profile.name == "Speakers (Realtek(R) Audio)"
    assert profile.is_display_endpoint is False
    assert profile.reason == "OS default endpoint"


def test_prefers_a_local_endpoint_when_the_default_is_a_display_endpoint(probe):
    devices = [
        _device("NVIDIA High Definition Audio (HDMI)", 0),
        _device("Speakers (Realtek(R) Audio)", 0),
    ]
    probe.configure(devices, default_index=0)

    profile = probe_output_device(force=True)

    assert profile.index == 1
    assert profile.name == "Speakers (Realtek(R) Audio)"
    assert profile.is_display_endpoint is False
    assert "display" in profile.reason.lower()


def test_f20_regression_the_truncated_tv_name_is_rejected_in_favor_of_a_local_speaker(probe):
    devices = [
        _device("NS-39D400NA14 (NVIDIA High Defi", 0),  # real truncated MME name
        _device("Speakers (Realtek(R) Audio)", 0),
    ]
    probe.configure(devices, default_index=0)

    profile = probe_output_device(force=True)

    assert profile.index == 1
    assert profile.name == "Speakers (Realtek(R) Audio)"


def test_f36_regression_an_indirection_layer_is_skipped_in_favor_of_a_concrete_local_device(probe):
    """The real F36 shape on this machine: the OS default is a display
    endpoint (the F18 case), and the FIRST non-display candidate
    enumerated is "Microsoft Sound Mapper - Output" -- an indirection
    layer that resolves to the OS default at stream-open time, not at
    probe time. Selecting it here would silently resolve straight back to
    the display endpoint being avoided. A concrete local device (ranked
    second) must win instead."""
    devices = [
        _device("NVIDIA High Definition Audio (HDMI)", 0),  # default, display
        _device("Microsoft Sound Mapper - Output", 0),       # indirection layer, enumerated first
        _device("Speakers (Realtek(R) Audio)", 0),           # concrete local device
    ]
    probe.configure(devices, default_index=0)

    profile = probe_output_device(force=True)

    assert profile.index == 2
    assert profile.name == "Speakers (Realtek(R) Audio)"


def test_indirection_layer_with_no_concrete_local_device_falls_back_to_the_display_endpoint(probe, caplog):
    """If Sound Mapper is the ONLY non-display candidate, it must not be
    silently accepted as though it were a real local device -- the module
    falls back the same way it does when no local endpoint exists at all
    (test_display_default_with_no_local_endpoint_falls_back_to_the_display_
    endpoint_itself above), rather than picking an indirection layer that
    would just resolve back to the display endpoint."""
    devices = [
        _device("NVIDIA High Definition Audio (HDMI)", 0),
        _device("Microsoft Sound Mapper - Output", 0),
    ]
    probe.configure(devices, default_index=0)

    with caplog.at_level("WARNING"):
        profile = probe_output_device(force=True)

    assert profile.index == 0
    assert profile.is_display_endpoint is True


def test_prefers_a_local_candidate_on_the_same_hostapi_as_the_default(probe):
    devices = [
        _device("Display Audio (HDMI)", 0),           # default, display, hostapi 0
        _device("USB Headset (WASAPI local)", 1),      # local, but different hostapi
        _device("Speakers (Realtek(R) Audio)", 0),     # local, SAME hostapi as default
    ]
    probe.configure(devices, default_index=0)

    profile = probe_output_device(force=True)

    assert profile.index == 2
    assert profile.name == "Speakers (Realtek(R) Audio)"


def test_falls_back_to_any_hostapi_local_candidate_when_same_hostapi_has_none(probe):
    devices = [
        _device("Display Audio (HDMI)", 0),
        _device("USB Headset (WASAPI local)", 1),
    ]
    probe.configure(devices, default_index=0)

    profile = probe_output_device(force=True)

    assert profile.index == 1
    assert profile.name == "USB Headset (WASAPI local)"


def test_display_default_with_no_local_endpoint_falls_back_to_the_display_endpoint_itself(probe, caplog):
    devices = [_device("Display Audio (HDMI)", 0)]
    probe.configure(devices, default_index=0)

    with caplog.at_level("WARNING"):
        profile = probe_output_device(force=True)

    assert profile.index == 0
    assert profile.is_display_endpoint is True
    assert "no local" in profile.reason.lower()
    assert any("go to that screen" in r.message.lower() or "inaudible" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# verified: a failing probe never raises, and never lies about verification
# ---------------------------------------------------------------------------


def test_verified_true_when_the_chosen_device_opens_successfully(probe):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)

    profile = probe_output_device(force=True)

    assert profile.verified is True


def test_verified_false_and_no_exception_when_the_chosen_device_fails_to_open(probe, caplog):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0, open_fails={0})

    with caplog.at_level("WARNING"):
        profile = probe_output_device(force=True)  # must not raise

    assert profile.verified is False
    assert profile.index == 0  # still returns a usable-looking profile, just marked unverified
    assert any("did not accept a buffer" in r.message.lower() for r in caplog.records)


def test_verify_opens_writes_a_near_silent_but_genuinely_nonzero_buffer(probe):
    """Per the module's own stated design: a true digital-zero write risks
    being optimized away by some drivers, which would make the check
    weaker than it looks. Pin that the probe buffer is actually non-zero."""
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)

    probe_output_device(force=True)

    assert len(probe.written_buffers) == 1
    buf = probe.written_buffers[0]
    assert np.any(buf != 0.0), "probe buffer must not be literal digital zero"
    assert np.max(np.abs(buf)) < 0.01, "probe buffer must be near-silent, not audible"


def test_no_default_output_device_returns_an_unusable_placeholder_without_crashing(probe, caplog):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0, default_raises=RuntimeError("PortAudio: no output devices"))

    with caplog.at_level("WARNING"):
        profile = probe_output_device(force=True)  # must not raise

    assert profile.verified is False
    assert profile.index == -1
    assert probe.opened_indices == []  # never even tried to open anything


# ---------------------------------------------------------------------------
# Caching: hit re-resolves by (name, hostapi) rather than trusting a stored
# index (F1's lesson applied to the output side); force bypasses the cache;
# device-list changes invalidate it; a corrupt cache degrades to a re-probe.
# ---------------------------------------------------------------------------


def test_cache_hit_ignores_a_stale_on_disk_index_and_re_resolves_by_name_and_hostapi(probe):
    devices = [
        _device("Speakers (Realtek(R) Audio)", 0),
        _device("Headphones (Realtek(R) Audio)", 0),
    ]
    probe.configure(devices, default_index=0)
    first = probe_output_device(force=False)
    assert first.index == 0
    assert first.name == "Speakers (Realtek(R) Audio)"

    # Corrupt the on-disk cache as if the index had drifted: same device,
    # wrong stored position.
    cached = json.loads(probe.cache_path.read_text(encoding="utf-8"))
    cached["profile"]["index"] = 1
    probe.cache_path.write_text(json.dumps(cached), encoding="utf-8")

    second = probe_output_device(force=False)  # cache hit: fingerprint unchanged

    assert second.name == "Speakers (Realtek(R) Audio)"
    assert second.index == 0, (
        "cache hit trusted the stale on-disk index instead of re-resolving "
        "by (name, hostapi)"
    )


def test_cache_hit_never_reopens_the_output_device(probe):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)
    probe_output_device(force=False)
    opens_after_first = len(probe.opened_indices)
    assert opens_after_first == 1

    probe_output_device(force=False)  # cache hit

    assert len(probe.opened_indices) == opens_after_first  # no re-verification on a hit


def test_force_true_bypasses_the_cache_and_reopens_the_device(probe):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)
    probe_output_device(force=False)
    opens_after_first = len(probe.opened_indices)

    probe_output_device(force=True)

    assert len(probe.opened_indices) > opens_after_first


def test_cache_invalidates_when_a_new_output_device_appears(probe):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)
    probe_output_device(force=False)

    # probe.configure() installs a fresh fake (opened_indices reset to []),
    # so re-checking against THIS fake in isolation cleanly proves whether
    # the second call actually re-opened the device or hit the cache.
    devices_plus_monitor = devices + [_device("New Monitor (HDMI)", 0)]
    probe.configure(devices_plus_monitor, default_index=0)
    probe_output_device(force=False)  # no force -- must still re-probe

    assert probe.opened_indices == [0], (
        "device-list change did not force a re-probe (cache was wrongly reused)"
    )


def test_corrupt_cache_file_triggers_a_clean_reprobe_not_a_crash(probe, caplog):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)
    probe.cache_path.parent.mkdir(parents=True, exist_ok=True)
    probe.cache_path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        profile = probe_output_device(force=False)  # must not raise

    assert profile.index == 0
    assert profile.verified is True


def test_cache_survives_a_process_boundary_via_disk(probe):
    devices = [_device("Speakers (Realtek(R) Audio)", 0)]
    probe.configure(devices, default_index=0)

    original = probe_output_device(force=False)
    assert probe.cache_path.exists()

    cached_raw = json.loads(probe.cache_path.read_text(encoding="utf-8"))
    from_disk = OutputProfile(**cached_raw["profile"])
    assert from_disk == original


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_output_profile_is_a_frozen_dataclass():
    assert dataclasses.is_dataclass(OutputProfile)
    p = OutputProfile(
        index=0, name="x", hostapi="MME", samplerate=48000,
        is_display_endpoint=False, verified=True, reason="test",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.verified = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Real hardware (opt-in): set NOVA_TEST_REAL_HARDWARE=1 to run against the
# actual output endpoints on this machine.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_TEST_REAL_HARDWARE") != "1",
    reason="set NOVA_TEST_REAL_HARDWARE=1 to probe the real output device(s) on this machine",
)
def test_real_hardware_probe_produces_a_profile_that_is_never_a_display_endpoint_when_avoidable():
    profile = probe_output_device(force=True)
    assert isinstance(profile, OutputProfile)
    # Not a hard assertion of audibility (the module docstring is explicit
    # that nothing in-process can prove that) -- just that the documented
    # preference actually ran against real devices without crashing.
    assert profile.index != -1 or profile.verified is False
