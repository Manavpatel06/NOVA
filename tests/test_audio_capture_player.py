"""Contract: docs/CONTRACTS.md T0.2 — nova.audio.Capture / nova.audio.Player.

    class Capture:  # 16 kHz mono float32 frames
        def start(self) -> None; def read(self) -> np.ndarray; def stop(self) -> None
    class Player:
        async def play(self, pcm: np.ndarray, samplerate: int) -> None   # cancellable
        def cancel(self) -> None

Only start/read/stop and play/cancel are contractual; both classes accept
extra constructor kwargs (profile injection, frame_ms, ...) purely as the
seam that makes them testable without real hardware — those aren't part of
the T0.2 contract itself, so tests here avoid over-pinning them beyond what's
needed to inject fakes.

No real audio device is opened anywhere in this file: nova.audio.capture.sd
and nova.audio.player.sd are each replaced with fakes local to that module.
"""
from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

import nova.audio.capture as capture_mod
import nova.audio.player as player_mod
from nova.audio.devices import DeviceProfile


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class _FakeInputStream:
    instances: list["_FakeInputStream"] = []

    def __init__(self, device, samplerate, channels, dtype, blocksize, callback):
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.callback = callback
        self.started = False
        self.closed = False
        _FakeInputStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def push(self, native_samples: np.ndarray) -> None:
        """Test helper: simulate PortAudio delivering a block."""
        block = native_samples.reshape(-1, self.channels)
        self.callback(block, block.shape[0], None, None)


class _FakeSDForCapture:
    def __init__(self):
        self.InputStream = _FakeInputStream


@pytest.fixture
def fake_input_stream(monkeypatch):
    _FakeInputStream.instances.clear()
    monkeypatch.setattr(capture_mod, "sd", _FakeSDForCapture())
    return _FakeInputStream


@pytest.fixture
def verified_profile() -> DeviceProfile:
    return DeviceProfile(
        index=7, name="fake mic", hostapi="Windows WDM-KS", samplerate=48000,
        verified=True, rms=0.01,
    )


def test_capture_uses_the_injected_profile_rather_than_probing(verified_profile, fake_input_stream):
    cap = capture_mod.Capture(profile=verified_profile)
    assert cap.profile == verified_profile


def test_capture_read_before_start_raises(verified_profile, fake_input_stream):
    cap = capture_mod.Capture(profile=verified_profile)
    with pytest.raises(RuntimeError):
        cap.read(timeout=0.1)


def test_capture_start_with_no_usable_device_raises_loudly(fake_input_stream):
    unusable = DeviceProfile(index=-1, name="<none>", hostapi="<none>", samplerate=16000,
                              verified=False, rms=0.0)
    cap = capture_mod.Capture(profile=unusable)
    with pytest.raises(RuntimeError):
        cap.start()


def test_capture_start_is_idempotent(verified_profile, fake_input_stream):
    cap = capture_mod.Capture(profile=verified_profile)
    cap.start()
    cap.start()  # must not open a second stream
    assert len(fake_input_stream.instances) == 1


def test_capture_stop_without_start_does_not_raise(verified_profile, fake_input_stream):
    cap = capture_mod.Capture(profile=verified_profile)
    cap.stop()  # must not raise


def test_capture_produces_16khz_mono_float32_frames_from_native_rate_audio(
    verified_profile, fake_input_stream
):
    """End-to-end through the real queue + LinearResampler + framing: feed
    48 kHz native audio, expect 16 kHz mono float32 frames back out."""
    cap = capture_mod.Capture(profile=verified_profile, frame_ms=30)
    cap.start()
    stream = fake_input_stream.instances[-1]
    assert stream.samplerate == verified_profile.samplerate == 48000

    # 3:1 downsample ratio (48kHz -> 16kHz); push several native blocks of a
    # synthetic tone so enough resampled output accumulates for a full frame.
    t = np.linspace(0, 1.0, 48000, endpoint=False, dtype=np.float64)
    tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    for start in range(0, len(tone), 480):
        stream.push(tone[start : start + 480])

    frame = cap.read(timeout=1.0)

    assert frame.dtype == np.float32
    assert frame.ndim == 1
    assert len(frame) == int(cap.target_sr * 30 / 1000)
    assert cap.target_sr == 16000

    cap.stop()
    assert stream.closed is True


def test_capture_read_after_stop_raises(verified_profile, fake_input_stream):
    cap = capture_mod.Capture(profile=verified_profile)
    cap.start()
    cap.stop()
    with pytest.raises(RuntimeError):
        cap.read(timeout=0.1)


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------


class _CallbackStop(Exception):
    pass


class _FakeOutputStream:
    instances: list["_FakeOutputStream"] = []

    def __init__(self, samplerate, channels, dtype, callback, finished_callback, device=None):
        self.samplerate = samplerate
        self.channels = channels
        self.device = device  # finding F18: Player now selects an endpoint explicitly
        self.callback = callback
        self.finished_callback = finished_callback
        self.closed = False
        self.chunks_delivered = 0
        self.first_chunk_delivered = threading.Event()
        self._chunk_frames = 64
        _FakeOutputStream.instances.append(self)

    def start(self):
        def run():
            outdata = np.zeros((self._chunk_frames, self.channels), dtype=np.float32)
            try:
                while True:
                    self.callback(outdata, self._chunk_frames, None, None)
                    self.chunks_delivered += 1
                    self.first_chunk_delivered.set()
            except _CallbackStop:
                pass
            finally:
                self.finished_callback()

        threading.Thread(target=run, daemon=True).start()

    def close(self):
        self.closed = True


class _FakeSDForPlayer:
    def __init__(self):
        self.OutputStream = _FakeOutputStream
        self.CallbackStop = _CallbackStop


@pytest.fixture
def fake_output_stream(monkeypatch):
    _FakeOutputStream.instances.clear()
    monkeypatch.setattr(player_mod, "sd", _FakeSDForPlayer())
    return _FakeOutputStream


async def test_cancel_before_any_playback_does_not_raise(fake_output_stream):
    player = player_mod.Player()
    player.cancel()  # must be safe with nothing in flight


async def test_play_completes_normally_and_cleans_up_the_stream(fake_output_stream):
    player = player_mod.Player()
    pcm = np.ones(128, dtype=np.float32)  # 2 chunks of 64 at the fake's chunk size

    await asyncio.wait_for(player.play(pcm, samplerate=16000), timeout=5)

    stream = fake_output_stream.instances[-1]
    assert stream.closed is True
    assert stream.channels == 1


async def test_cancel_stops_playback_within_one_callback_block(fake_output_stream):
    """Barge-in depends on this: cancel() must cut audio well before the
    full buffer would have played out, not merely at the natural end."""
    player = player_mod.Player()
    # A buffer large enough that "play to completion" would take many
    # callback chunks if cancel() did nothing.
    pcm = np.ones(64 * 100_000, dtype=np.float32)

    task = asyncio.create_task(player.play(pcm, samplerate=16000))

    stream = None
    for _ in range(200):
        if fake_output_stream.instances:
            stream = fake_output_stream.instances[-1]
            break
        await asyncio.sleep(0.01)
    assert stream is not None, "Player never opened an output stream"

    # Deterministic sync point: wait for the first real chunk, not a sleep.
    await asyncio.get_event_loop().run_in_executor(
        None, stream.first_chunk_delivered.wait, 5
    )
    player.cancel()

    await asyncio.wait_for(task, timeout=5)

    assert stream.chunks_delivered < 100_000, (
        "cancel() did not stop playback promptly — "
        f"delivered {stream.chunks_delivered} of ~100000 possible chunks"
    )
    assert stream.closed is True


async def test_cancel_is_safe_to_call_from_a_different_thread(fake_output_stream):
    player = player_mod.Player()
    pcm = np.ones(64 * 50_000, dtype=np.float32)

    task = asyncio.create_task(player.play(pcm, samplerate=16000))
    for _ in range(200):
        if fake_output_stream.instances:
            break
        await asyncio.sleep(0.01)
    stream = fake_output_stream.instances[-1]
    await asyncio.get_event_loop().run_in_executor(None, stream.first_chunk_delivered.wait, 5)

    def cancel_from_other_thread():
        player.cancel()

    t = threading.Thread(target=cancel_from_other_thread)
    t.start()
    t.join(timeout=5)

    await asyncio.wait_for(task, timeout=5)
    assert stream.chunks_delivered < 50_000
