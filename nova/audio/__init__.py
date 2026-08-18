"""Audio layer: device probing (the WASAPI trap), capture, and playback.

See docs/CONTRACTS.md T0.2 for the interface this module implements.
"""
from __future__ import annotations

from nova.audio.capture import Capture
from nova.audio.devices import DeviceProfile, probe_input_devices
from nova.audio.player import Player

__all__ = ["DeviceProfile", "probe_input_devices", "Capture", "Player"]
