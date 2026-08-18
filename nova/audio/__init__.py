"""Audio layer: device probing (the WASAPI trap), capture, playback, voice
activity detection, and the wake word.

See docs/CONTRACTS.md T0.2 for the T0.2 interface (DeviceProfile, Capture,
Player). Vad and WakeWord are T1.1.
"""
from __future__ import annotations

from nova.audio.capture import Capture
from nova.audio.devices import DeviceProfile, probe_input_devices
from nova.audio.player import Player
from nova.audio.vad import Vad
from nova.audio.wake import WakeEvent, WakeWord

__all__ = [
    "DeviceProfile", "probe_input_devices", "Capture", "Player",
    "Vad", "WakeWord", "WakeEvent",
]
