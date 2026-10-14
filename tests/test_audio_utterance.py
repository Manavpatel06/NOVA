"""Contract: nova/audio/utterance.py -- UtteranceSegmenter.

Driven by a scripted fake VAD (process(frame)->float, reset()). Frames are
480-sample float32 = 30 ms at 16 kHz. Every frame is filled with a unique
marker value so the emitted utterance's exact composition (pre-roll, speech,
trailing silence) can be checked, not just its length.
"""
from __future__ import annotations

import numpy as np
import pytest

from nova.audio.utterance import UtteranceSegmenter

FRAME = 480
FRAME_MS = 30


class ScriptedVad:
    """Returns the probability encoded in the frame: frames built by
    `_speech()` carry a positive marker -> 0.9, `_silence()` a negative
    marker -> 0.0. Counts calls and resets."""

    def __init__(self):
        self.calls = 0
        self.resets = 0

    def process(self, frame):
        self.calls += 1
        return 0.9 if float(frame[0]) > 0 else 0.0

    def reset(self):
        self.resets += 1


class _Counter:
    def __init__(self):
        self.n = 0

    def next(self):
        self.n += 1
        return self.n


_ids = _Counter()


def _speech() -> np.ndarray:
    return np.full(FRAME, float(_ids.next()), dtype=np.float32)


def _silence() -> np.ndarray:
    return np.full(FRAME, -float(_ids.next()), dtype=np.float32)


def _markers(utt: np.ndarray) -> list[float]:
    assert len(utt) % FRAME == 0
    return [float(utt[i]) for i in range(0, len(utt), FRAME)]


def _feed(seg, frames):
    out = []
    for f in frames:
        r = seg.process(f)
        if r is not None:
            out.append(r)
    return out


def _seg(vad=None, **kw):
    defaults = dict(silence_ms=300, max_ms=3000, min_speech_ms=150, preroll_ms=90, threshold=0.5, start_frames=3)
    defaults.update(kw)
    return UtteranceSegmenter(vad or ScriptedVad(), **defaults)


def test_no_speech_never_emits():
    vad = ScriptedVad()
    seg = _seg(vad)
    out = _feed(seg, [_silence() for _ in range(500)])
    assert out == []
    assert vad.calls == 500  # positive control: the VAD really was consulted
    assert seg.in_speech is False


def test_speech_then_silence_emits_one_utterance_with_preroll():
    seg = _seg()  # preroll 90 ms = 3 frames, silence 300 ms = 10 frames
    lead = [_silence() for _ in range(8)]
    speech = [_speech() for _ in range(10)]
    trail = [_silence() for _ in range(10)]

    out = _feed(seg, lead + speech)
    assert out == []
    assert seg.in_speech is True
    # 9 frames of silence (270 ms) is not yet enough
    out = _feed(seg, trail[:9])
    assert out == []
    out = _feed(seg, trail[9:])
    assert len(out) == 1
    utt = out[0]
    assert utt.dtype == np.float32
    expected = lead[-3:] + speech + trail
    assert _markers(utt) == [float(f[0]) for f in expected]
    assert seg.in_speech is False


def test_utterance_emitted_exactly_once_and_more_silence_emits_nothing():
    seg = _seg()
    out = _feed(seg, [_speech() for _ in range(10)] + [_silence() for _ in range(100)])
    assert len(out) == 1


def test_two_separate_utterances_emit_twice():
    seg = _seg()
    block = lambda: [_speech() for _ in range(10)] + [_silence() for _ in range(12)]  # noqa: E731
    out = _feed(seg, block() + block())
    assert len(out) == 2


def test_blips_shorter_than_start_frames_never_start_speech():
    vad = ScriptedVad()
    seg = _seg(vad, start_frames=3)
    frames = []
    for _ in range(50):
        frames += [_speech(), _speech(), _silence()]  # 2-frame blips
    out = _feed(seg, frames)
    assert out == []
    assert seg.in_speech is False
    assert vad.calls == len(frames)


def test_blip_frames_become_preroll_for_the_next_real_speech():
    seg = _seg(preroll_ms=90)
    blip = [_speech(), _silence()]
    speech = [_speech() for _ in range(10)]
    trail = [_silence() for _ in range(10)]
    out = _feed(seg, blip + speech + trail)
    assert len(out) == 1
    m = _markers(out[0])
    # pre-roll = last 3 non-started frames: blip speech + blip silence ... only 2 exist
    assert m[:2] == [float(blip[0][0]), float(blip[1][0])]


def test_exactly_start_frames_of_speech_starts_speech():
    seg = _seg(start_frames=3)
    _feed(seg, [_speech(), _speech()])
    assert seg.in_speech is False
    _feed(seg, [_speech()])
    assert seg.in_speech is True


def test_speech_shorter_than_min_speech_ms_is_dropped():
    seg = _seg(min_speech_ms=300, start_frames=3)  # 300 ms = 10 frames
    out = _feed(seg, [_speech() for _ in range(5)] + [_silence() for _ in range(20)])
    assert out == []
    assert seg.in_speech is False  # it did start and finish, just dropped


def test_speech_at_min_speech_ms_is_kept():
    seg = _seg(min_speech_ms=300, start_frames=3)
    out = _feed(seg, [_speech() for _ in range(10)] + [_silence() for _ in range(20)])
    assert len(out) == 1


def test_max_ms_cuts_a_never_ending_utterance():
    seg = _seg(max_ms=900, preroll_ms=0, start_frames=3)  # 30 frames
    frames = [_speech() for _ in range(200)]
    outs = []
    first_emit_at = None
    for i, f in enumerate(frames):
        r = seg.process(f)
        if r is not None:
            outs.append(r)
            if first_emit_at is None:
                first_emit_at = i
    assert outs, "max_ms never cut the utterance"
    assert first_emit_at == 29  # the 30th frame reaches 900 ms
    assert len(outs[0]) == 30 * FRAME
    # the stream keeps being cut into bounded pieces, never one growing buffer
    assert len(outs) >= 5
    assert all(len(o) <= 30 * FRAME for o in outs)


def test_reset_clears_in_progress_utterance_and_resets_vad():
    vad = ScriptedVad()
    seg = _seg(vad)
    _feed(seg, [_silence() for _ in range(5)] + [_speech() for _ in range(8)])
    assert seg.in_speech is True
    seg.reset()
    assert seg.in_speech is False
    assert vad.resets >= 1
    # the old speech is gone: silence alone never produces an utterance now
    assert _feed(seg, [_silence() for _ in range(50)]) == []


def test_reset_clears_preroll_and_pending():
    seg = _seg(preroll_ms=90)
    pre = [_silence() for _ in range(3)]
    _feed(seg, pre + [_speech(), _speech()])  # pending candidates
    seg.reset()
    speech = [_speech() for _ in range(10)]
    trail = [_silence() for _ in range(10)]
    out = _feed(seg, speech + trail)
    assert len(out) == 1
    assert _markers(out[0]) == [float(f[0]) for f in speech + trail]


def test_reset_tolerates_a_vad_whose_reset_raises():
    class BadVad(ScriptedVad):
        def reset(self):
            raise RuntimeError("boom")

    seg = _seg(BadVad())
    seg.reset()  # must not raise


def test_vad_exception_is_treated_as_silence():
    class BoomVad:
        def process(self, frame):
            raise RuntimeError("boom")

        def reset(self):
            pass

    seg = _seg(BoomVad())
    assert _feed(seg, [_speech() for _ in range(50)]) == []
    assert seg.in_speech is False


@pytest.mark.parametrize("frame", [None, np.zeros(0, dtype=np.float32)])
def test_empty_frame_is_ignored(frame):
    vad = ScriptedVad()
    seg = _seg(vad)
    assert seg.process(frame) is None
    assert vad.calls == 0


def test_default_parameters_with_30ms_frames():
    """Defaults (silence 800 ms, preroll 300 ms, min 300 ms): 27 silent
    30 ms frames (810 ms) end the utterance, 26 (780 ms) do not."""
    seg = UtteranceSegmenter(ScriptedVad())
    lead = [_silence() for _ in range(20)]
    speech = [_speech() for _ in range(20)]
    _feed(seg, lead + speech)
    assert _feed(seg, [_silence() for _ in range(26)]) == []
    out = _feed(seg, [_silence()])
    assert len(out) == 1
    m = _markers(out[0])
    assert m[:10] == [float(f[0]) for f in lead[-10:]]  # 300 ms pre-roll
    assert len(m) == 10 + 20 + 27
