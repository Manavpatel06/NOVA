"""Contract: docs/CONTRACTS.md T0.4 — nova.tts.Synthesizer.

    class Synthesizer:
        def load(self) -> None; def unload(self) -> None
        def say(self, text: str) -> tuple[np.ndarray, int]   # (pcm, samplerate)

"Kokoro ONNX via onnxruntime... If Kokoro fails, fall back to Windows SAPI
and log a WARNING naming Kokoro — NOVA must never go silently mute."

The interesting behavior isn't the happy path, it's the fallback discipline:
  - a load()-time Kokoro failure degrades the whole backend to SAPI, once;
  - a say()-time Kokoro failure falls through to SAPI for THAT CALL ONLY —
    the backend is not permanently downgraded, so the next call tries
    Kokoro again. Verified directly against the real code before writing
    the assertion (not assumed from the docstring).

`kokoro_onnx.Kokoro` is imported lazily inside load() (`from kokoro_onnx
import Kokoro`), so the fast/mocked tests patch the attribute on the real
`kokoro_onnx` module — the only patch target a deferred `from X import Y`
will actually observe (same pattern as faster_whisper in
test_stt_transcriber.py). `_say_sapi` is a free function in this module and
is patched directly where the fallback-routing tests don't want to touch
real COM.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

import nova.tts.synthesizer as synth_mod
from nova.tts.synthesizer import Synthesizer, _say_sapi


class _FakeKokoro:
    instances: list["_FakeKokoro"] = []

    def __init__(self, model_path, voices_path, *, create_raises=None, create_return=None):
        self.model_path = model_path
        self.voices_path = voices_path
        self.create_calls: list[tuple] = []
        self._create_raises = create_raises
        self._create_return = create_return if create_return is not None else (
            [0.0, 0.1, -0.1, 0.2], 24000
        )
        _FakeKokoro.instances.append(self)

    def create(self, text, voice, lang):
        self.create_calls.append((text, voice, lang))
        if self._create_raises is not None:
            raise self._create_raises
        return self._create_return


@pytest.fixture
def fake_kokoro_module(monkeypatch):
    """Patches kokoro_onnx.Kokoro so `from kokoro_onnx import Kokoro` inside
    Synthesizer.load() picks it up. Configure per-test via
    fake_kokoro_module.configure(...)."""
    import kokoro_onnx

    _FakeKokoro.instances.clear()
    state = {"create_raises": None, "create_return": None}

    def factory(model_path, voices_path):
        return _FakeKokoro(
            model_path, voices_path,
            create_raises=state["create_raises"], create_return=state["create_return"],
        )

    monkeypatch.setattr(kokoro_onnx, "Kokoro", factory)

    class Controller:
        def configure(self, *, create_raises=None, create_return=None):
            state["create_raises"] = create_raises
            state["create_return"] = create_return

        @property
        def instances(self):
            return _FakeKokoro.instances

    return Controller()


@pytest.fixture
def kokoro_import_broken(monkeypatch):
    """Forces `from kokoro_onnx import Kokoro` to raise ImportError."""
    monkeypatch.setitem(__import__("sys").modules, "kokoro_onnx", None)


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_uses_kokoro_when_it_constructs_successfully(fake_kokoro_module):
    synth = Synthesizer()
    synth.load()
    assert synth.backend == "kokoro"
    assert len(fake_kokoro_module.instances) == 1


def test_load_falls_back_to_sapi_when_kokoro_cannot_be_imported(kokoro_import_broken, caplog):
    synth = Synthesizer()
    with caplog.at_level("WARNING"):
        synth.load()
    assert synth.backend == "sapi"
    assert any("kokoro" in r.message.lower() for r in caplog.records)


def test_load_falls_back_to_sapi_when_kokoro_construction_raises(monkeypatch, caplog):
    import kokoro_onnx

    def broken_factory(model_path, voices_path):
        raise RuntimeError("onnxruntime session creation failed")

    monkeypatch.setattr(kokoro_onnx, "Kokoro", broken_factory)
    synth = Synthesizer()
    with caplog.at_level("WARNING"):
        synth.load()

    assert synth.backend == "sapi"
    assert any("kokoro" in r.message.lower() for r in caplog.records)


def test_load_is_idempotent(fake_kokoro_module):
    synth = Synthesizer()
    synth.load()
    synth.load()
    assert len(fake_kokoro_module.instances) == 1


def test_backend_is_none_before_load():
    synth = Synthesizer()
    assert synth.backend is None


# ---------------------------------------------------------------------------
# unload()
# ---------------------------------------------------------------------------


def test_unload_resets_backend_to_none(fake_kokoro_module):
    synth = Synthesizer()
    synth.load()
    synth.unload()
    assert synth.backend is None


def test_unload_without_load_does_not_raise():
    Synthesizer().unload()  # must not raise


def test_load_after_unload_creates_a_fresh_backend(fake_kokoro_module):
    synth = Synthesizer()
    synth.load()
    synth.unload()
    synth.load()
    assert len(fake_kokoro_module.instances) == 2


# ---------------------------------------------------------------------------
# say(): the happy path, and the type contract
# ---------------------------------------------------------------------------


def test_say_auto_loads_when_not_yet_loaded(fake_kokoro_module):
    synth = Synthesizer()
    synth.say("hello")
    assert synth.backend == "kokoro"


def test_say_kokoro_success_returns_float32_ndarray_and_int_samplerate(fake_kokoro_module):
    fake_kokoro_module.configure(create_return=([0.0, 0.25, -0.25], 22050))
    synth = Synthesizer()
    pcm, samplerate = synth.say("hello there")

    assert isinstance(pcm, np.ndarray)
    assert pcm.dtype == np.float32
    assert samplerate == 22050
    assert isinstance(samplerate, int)
    np.testing.assert_allclose(pcm, [0.0, 0.25, -0.25], atol=1e-6)


def test_say_passes_text_through_to_kokoro_unchanged(fake_kokoro_module):
    synth = Synthesizer()
    synth.say("open notepad please")
    calls = fake_kokoro_module.instances[0].create_calls
    assert calls[-1][0] == "open notepad please"


# ---------------------------------------------------------------------------
# The fallback discipline: say()-time failure is per-call, not permanent
# ---------------------------------------------------------------------------


def test_say_kokoro_failure_falls_through_to_sapi_for_that_call_only(
    fake_kokoro_module, monkeypatch, caplog
):
    fake_kokoro_module.configure(create_raises=RuntimeError("onnxruntime inference error"))
    sapi_calls = []

    def fake_sapi(text):
        sapi_calls.append(text)
        return np.zeros(10, dtype=np.float32), 16000

    monkeypatch.setattr(synth_mod, "_say_sapi", fake_sapi)

    synth = Synthesizer()
    synth.load()
    assert synth.backend == "kokoro"

    with caplog.at_level("WARNING"):
        pcm, sr = synth.say("this utterance breaks kokoro")

    assert sapi_calls == ["this utterance breaks kokoro"]
    assert sr == 16000
    assert synth.backend == "kokoro", (
        "a synthesis-time failure must not permanently downgrade the backend"
    )
    assert any("kokoro" in r.message.lower() for r in caplog.records)


def test_say_retries_kokoro_on_the_next_call_after_a_transient_failure(
    fake_kokoro_module, monkeypatch
):
    """Directly proves 'not permanently downgraded': after one failed call
    falls through to SAPI, the very next call must attempt Kokoro again."""
    monkeypatch.setattr(synth_mod, "_say_sapi", lambda text: (np.zeros(1, dtype=np.float32), 16000))

    synth = Synthesizer()
    synth.load()
    kokoro = fake_kokoro_module.instances[0]

    kokoro._create_raises = RuntimeError("transient failure")
    synth.say("first utterance")
    assert len(kokoro.create_calls) == 1  # Kokoro was tried and failed

    kokoro._create_raises = None  # "transient" — recovers
    synth.say("second utterance")
    assert len(kokoro.create_calls) == 2  # tried again, not skipped


def test_say_never_raises_even_when_kokoro_was_never_available(kokoro_import_broken, monkeypatch):
    """The core 'never go silently mute' promise: no Kokoro at all, still
    produces audio via SAPI rather than raising."""
    monkeypatch.setattr(synth_mod, "_say_sapi", lambda text: (np.ones(5, dtype=np.float32), 16000))
    synth = Synthesizer()
    pcm, sr = synth.say("hello")  # must not raise
    assert len(pcm) == 5
    assert sr == 16000


# ---------------------------------------------------------------------------
# _say_sapi: real Windows COM, no subprocess (stays outside the T0.5
# no-spawn allowlist by design — verified in test_no_network_or_process_spawn.py
# that this module never appears there). Fast and produces no audible
# output (redirected to a file stream), so these run by default rather than
# behind the NOVA_TEST_REAL_HARDWARE opt-in.
# ---------------------------------------------------------------------------


def test_real_sapi_fallback_produces_float32_pcm():
    pcm, samplerate = _say_sapi("Testing the SAPI fallback path.")
    assert isinstance(pcm, np.ndarray)
    assert pcm.dtype == np.float32
    assert len(pcm) > 0
    assert samplerate > 0
    assert np.abs(pcm).max() <= 1.0 + 1e-6


def test_real_sapi_cleans_up_its_temp_file():
    tmpdir = Path(tempfile.gettempdir())
    before = set(tmpdir.glob(f"nova_sapi_{os.getpid()}_*.wav"))

    _say_sapi("Cleanup check.")

    after = set(tmpdir.glob(f"nova_sapi_{os.getpid()}_*.wav"))
    assert after == before  # no leftover temp file from this call


def test_real_sapi_handles_empty_text_without_crashing():
    pcm, samplerate = _say_sapi("")
    assert isinstance(pcm, np.ndarray)
    assert samplerate > 0


# ---------------------------------------------------------------------------
# Real hardware (opt-in): loads the actual Kokoro ONNX model already on
# disk. Off by default for consistency with the rest of the suite's
# hardware-touching tests, even though this one is fast (~1-2s measured).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_TEST_REAL_HARDWARE") != "1",
    reason="set NOVA_TEST_REAL_HARDWARE=1 to load the real Kokoro ONNX model",
)
def test_real_synthesizer_produces_audio_via_kokoro():
    synth = Synthesizer()
    try:
        synth.load()
        assert synth.backend == "kokoro"
        pcm, samplerate = synth.say("Hello from NOVA.")
        assert isinstance(pcm, np.ndarray)
        assert pcm.dtype == np.float32
        assert len(pcm) > 0
        assert samplerate > 0
        assert np.abs(pcm).max() <= 1.0 + 1e-6
    finally:
        synth.unload()
    assert synth.backend is None
