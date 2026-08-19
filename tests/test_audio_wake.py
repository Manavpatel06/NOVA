"""Contract: docs/CONTRACTS.md T1.1 — nova.audio.wake.WakeWord + nova.audio.vad.Vad.

    TOPIC_WAKE_START = "wake.start"   # payload carries preroll PCM
    TOPIC_WAKE_END   = "wake.end"     # mode: "vad_end" | "arm_timeout"

Promises under test (from CONTRACTS.md T1.1 and the module's own docstring):
  - never boots deaf-and-silent: a missing model file -> WARNING naming the
    exact file, backend_available=False, process() a safe no-op;
  - opens no audio device -- fed frames by whoever owns the single Capture;
  - hey_nova.onnx is a pure drop-in: the classifier's embedding-window size
    is read from the model's own input shape at load time, never hardcoded;
  - privacy: the ring buffer is bounded, never grows, never logged, never
    written to disk; raw PCM leaves this module only on wake.start;
  - wake and push-to-talk are interchangeable arming sources (WakeEvent
    mirrors PTTEvent's shape);
  - regression pin: the classifier's embedding window is pre-filled with
    silence so a real ~1-1.5s utterance can be detected at all, instead of
    needing ~2.3s of cold-start accumulation first (found + fixed in T1.1).

No real ONNX model is ever loaded: `nova.audio.wake.ort` is replaced with a
small fake whose melspectrogram/embedding sessions produce shape-plausible
placeholder output (their numeric content is irrelevant to every promise
above) and whose classifier session is fully controllable per test, so
detection timing and scores are deterministic rather than depending on real
acoustic content. `Vad` is injected as a fake too (constructor accepts
`vad=`), since VAD's own contract is covered separately in
test_audio_vad.py. No sounddevice/PortAudio is touched anywhere in this file
-- see the "opens no audio device" section, which asserts that structurally.
"""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import numpy as np
import pytest

import nova.audio.wake as wake_mod
from nova.audio.wake import (
    DEFAULT_ARM_TIMEOUT_MS,
    DEFAULT_VAD_SILENCE_MS,
    EMBED_MEL_FRAMES,
    MEL_STEP_SAMPLES,
    SAMPLERATE,
    SPEECH_GRACE_S,
    TOPIC_WAKE_END,
    TOPIC_WAKE_START,
    WakeEvent,
    WakeWord,
)

DEFAULT_EMBED_DIM = 96


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBus:
    def __init__(self):
        self.published: list[tuple[str, WakeEvent]] = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


class _FakeVad:
    """Stands in for nova.audio.vad.Vad. `probs` is a queue of speech
    probabilities consumed one per process() call; once exhausted, further
    calls return 0.0 (silence)."""

    def __init__(self, probs=None, available=True):
        self.available = available
        self.loaded = False
        self.reset_calls = 0
        self.process_calls: list[np.ndarray] = []
        self._probs = list(probs or [])

    def load(self):
        self.loaded = True

    def reset(self):
        self.reset_calls += 1

    def process(self, frame: np.ndarray) -> float:
        self.process_calls.append(frame)
        if self._probs:
            return self._probs.pop(0)
        return 0.0


class _FakeGate:
    """Stands in for nova.audio.duplex.DuplexGate. `open_` is a plain bool
    the test flips directly, so these tests pin WakeWord's own use of the
    gate (T1.7/F26) without depending on DuplexGate's real hangover
    timing, which is covered separately in test_audio_duplex.py."""

    def __init__(self, open_: bool = True):
        self.open_ = open_
        self.mic_open_calls = 0

    def mic_open(self) -> bool:
        self.mic_open_calls += 1
        return self.open_


class _FakeInput:
    def __init__(self, name, shape):
        self.name = name
        self.shape = shape


class _FakeSession:
    def __init__(self, name, input_name, input_shape, run_fn):
        self.name = name
        self.input_name = input_name
        self.input_shape = input_shape
        self.run_fn = run_fn
        self.run_calls: list[dict] = []

    def get_inputs(self):
        return [_FakeInput(self.input_name, self.input_shape)]

    def run(self, output_names, feeds):
        self.run_calls.append(feeds)
        return self.run_fn(feeds)


def _melspec_run_fn(feeds):
    # Enough mel-frames for at least EMBED_MEL_FRAMES; content is irrelevant
    # to every promise under test here (only shape matters downstream).
    return [np.zeros((1, EMBED_MEL_FRAMES + 20, 32), dtype=np.float32)]


def _embedding_run_fn(feeds):
    return [np.zeros((1, DEFAULT_EMBED_DIM), dtype=np.float32)]


class _FakeSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = None
        self.inter_op_num_threads = None


class _FakeORT:
    """Stands in for onnxruntime inside nova.audio.wake. melspectrogram.onnx
    and embedding_model.onnx get fixed, shape-plausible placeholder sessions
    (their content never matters to what this module decides). The
    classifier session is fully controllable per test via `classifier_name`
    / `classifier_shape` / `classifier_run_fn`, so detection timing and
    scores are deterministic instead of depending on real acoustic content.
    `raise_for` simulates a corrupt/unreadable model file by filename.
    """

    def __init__(
        self,
        classifier_name="clf.onnx",
        classifier_shape=(1, 16, DEFAULT_EMBED_DIM),
        classifier_run_fn=None,
        raise_for=(),
    ):
        self.SessionOptions = _FakeSessionOptions
        self._classifier_name = classifier_name
        self._classifier_shape = classifier_shape
        self._classifier_run_fn = classifier_run_fn or (
            lambda feeds: [np.array(0.0, dtype=np.float32)]
        )
        self._raise_for = set(raise_for)
        self.sessions: dict[str, _FakeSession] = {}
        self.created_paths: list[str] = []

    def InferenceSession(self, path, sess_options=None, providers=None):
        self.created_paths.append(str(path))
        fname = Path(path).name
        if fname in self._raise_for:
            raise RuntimeError(f"simulated failure loading {fname}")
        if fname == "melspectrogram.onnx":
            sess = _FakeSession(fname, "input", (1, -1), _melspec_run_fn)
        elif fname == "embedding_model.onnx":
            sess = _FakeSession(fname, "input_1", (1, EMBED_MEL_FRAMES, 32, 1), _embedding_run_fn)
        elif fname == self._classifier_name:
            sess = _FakeSession(fname, "x.1", self._classifier_shape, self._classifier_run_fn)
        else:
            raise AssertionError(f"unexpected model path opened by WakeWord: {path}")
        self.sessions[fname] = sess
        return sess


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def wake_model_dir(tmp_path, monkeypatch) -> Path:
    """A directory with a space in its name (Windows path stressor, per
    CLAUDE.md) substituted for nova.audio.wake.WAKE_MODEL_DIR, so no test in
    this file ever touches the real models/wake/ directory."""
    d = tmp_path / "wake models (fake)"
    d.mkdir()
    monkeypatch.setattr(wake_mod, "WAKE_MODEL_DIR", d)
    return d


def _touch(d: Path, name: str) -> Path:
    p = d / name
    p.write_bytes(b"placeholder -- ort is faked in this module")
    return p


@pytest.fixture
def fake_ort(monkeypatch):
    def _install(**kw):
        fake = _FakeORT(**kw)
        monkeypatch.setattr(wake_mod, "ort", fake)
        return fake

    return _install


def _make_wake(
    wake_model_dir: Path,
    bus=None,
    loop=None,
    classifier_name="clf.onnx",
    vad=None,
    **kw,
) -> WakeWord:
    return WakeWord(
        bus=bus if bus is not None else _FakeBus(),
        model_path=classifier_name,
        loop=loop,
        vad=vad if vad is not None else _FakeVad(),
        **kw,
    )


def _frame(n: int, value: float = 0.0) -> np.ndarray:
    return np.full(n, value, dtype=np.float32)


async def _wait_until(predicate, timeout=2.0):
    import time

    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Never boots deaf-and-silent
# ---------------------------------------------------------------------------


def test_start_with_all_model_files_missing_warns_naming_each_and_disables_backend(
    wake_model_dir, caplog
):
    wake = _make_wake(wake_model_dir, classifier_name="hey_nova.onnx", loop=asyncio.new_event_loop())
    with caplog.at_level("WARNING"):
        wake.start()
    assert wake.backend_available is False
    joined = " ".join(r.message for r in caplog.records)
    assert "hey_nova.onnx" in joined
    assert "melspectrogram.onnx" in joined
    assert "embedding_model.onnx" in joined


def test_start_with_only_the_classifier_missing_warns_naming_exactly_that_file(
    wake_model_dir, caplog
):
    """A config pointing at a nonexistent hey_nova.onnx must never look
    healthy, even when the shared melspec/embedding models are fine."""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    wake = _make_wake(wake_model_dir, classifier_name="hey_nova.onnx", loop=asyncio.new_event_loop())

    with caplog.at_level("WARNING"):
        wake.start()

    assert wake.backend_available is False
    joined = " ".join(r.message for r in caplog.records)
    assert "hey_nova.onnx" in joined
    assert "melspectrogram.onnx" not in joined.replace("hey_nova.onnx", "")


def test_missing_model_leaves_process_a_safe_noop(wake_model_dir, caplog):
    bus = _FakeBus()
    wake = _make_wake(wake_model_dir, bus=bus, classifier_name="hey_nova.onnx", loop=asyncio.new_event_loop())
    wake.start()
    assert wake.backend_available is False

    wake.process(_frame(480))  # must not raise

    assert bus.published == []


def test_session_load_failure_disables_backend_and_does_not_raise(wake_model_dir, fake_ort, caplog):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(raise_for={"clf.onnx"})

    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    with caplog.at_level("WARNING"):
        wake.start()  # must not raise

    assert wake.backend_available is False


def test_start_with_no_running_loop_and_none_provided_degrades_without_crashing(
    wake_model_dir, fake_ort, caplog
):
    """Plain (non-async) test: deliberately no running asyncio loop and none
    passed to start()."""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()

    wake = _make_wake(wake_model_dir)  # no loop
    with caplog.at_level("WARNING"):
        wake.start()

    assert wake.backend_available is False


# ---------------------------------------------------------------------------
# Opens no audio device
# ---------------------------------------------------------------------------


def test_module_never_imports_sounddevice_or_touches_portaudio():
    """WakeWord.process() is fed frames by whoever owns the single shared
    Capture -- it must not be able to open a competing InputStream. Checked
    structurally against the source, not just "no test happened to call it",
    the same discipline test_no_network_or_process_spawn.py applies to
    egress."""
    source = Path(wake_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "sounddevice", "wake.py must not import sounddevice"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "sounddevice", "wake.py must not import from sounddevice"
    assert "sounddevice" not in source


def test_wake_word_has_no_device_or_stream_attribute(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()
    for attr in vars(wake):
        assert "stream" not in attr.lower() and "device" not in attr.lower(), (
            f"unexpected device/stream-shaped attribute: {attr}"
        )


# ---------------------------------------------------------------------------
# hey_nova.onnx is a pure drop-in: window size read from the model's own
# input shape, never hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", [4, 16, 23])
def test_classifier_window_size_is_read_from_the_models_own_input_shape(
    wake_model_dir, fake_ort, window
):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_shape=(1, window, DEFAULT_EMBED_DIM))

    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()

    assert wake.backend_available is True
    assert wake._embeddings.maxlen == window


def test_classifier_reporting_a_non_fixed_window_disables_backend_without_crashing(
    wake_model_dir, fake_ort, caplog
):
    """A dynamic-shape model (batch/seq dim reported as a string, not an
    int) must degrade loudly rather than crash trying to build a deque with
    a non-integer maxlen."""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_shape=(1, "N", DEFAULT_EMBED_DIM))

    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    with caplog.at_level("WARNING"):
        wake.start()  # must not raise

    assert wake.backend_available is False


# ---------------------------------------------------------------------------
# Regression pin: the classifier window is pre-filled with silence at
# start(), so a real ~1-1.5s utterance can be detected instead of needing a
# ~2.3s cold-start accumulation first.
# ---------------------------------------------------------------------------


def test_embeddings_window_is_already_full_immediately_after_start(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    window = 4
    fake_ort(classifier_shape=(1, window, DEFAULT_EMBED_DIM))

    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()

    assert len(wake._embeddings) == wake._embeddings.maxlen == window


async def test_a_single_mel_step_after_start_can_already_score_and_arm(wake_model_dir, fake_ort):
    """Without the pre-fill fix, the classifier needs `window` more
    real detection steps (each needing ~80ms of new audio on top of the ~1s
    of ring context) before it ever produces a non-placeholder score --
    which a real ~1-1.5s "hey jarvis" utterance cannot outlast. With the
    fix, the very first real detection step after start() can already
    score, because the window was pre-filled with silence embeddings."""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    window = 4
    fake_ort(
        classifier_shape=(1, window, DEFAULT_EMBED_DIM),
        classifier_run_fn=lambda feeds: [np.array(0.99, dtype=np.float32)],
    )

    bus = _FakeBus()
    loop = asyncio.get_running_loop()
    wake = _make_wake(wake_model_dir, bus=bus, loop=loop, threshold=0.5)
    wake.start()

    # Exactly one MEL_STEP_SAMPLES worth of new audio -- one detection step.
    wake.process(_frame(MEL_STEP_SAMPLES))

    await _wait_until(lambda: len(bus.published) == 1)
    assert bus.published[0][0] == TOPIC_WAKE_START


# ---------------------------------------------------------------------------
# Privacy: the ring buffer is bounded, never grows, never logged, never
# written to disk; raw PCM leaves this module only on wake.start.
# ---------------------------------------------------------------------------


def _find_disk_write_calls(source: str) -> list[str]:
    """F42: AST-based replacement for the original substring scan. Flags
    two shapes: any reference to the builtin name `open` (as a Name node in
    Load context -- catches both a direct `open(...)` call and an alias
    like `o = open; o(...)`, which a substring scan cannot see at all) and
    any `.write_text`/`.write_bytes` attribute access (catches a Path
    write call regardless of what the receiver expression looks like).
    Returns a list of "line N: description" strings, one per hit.

    Deliberately does NOT match on the attribute name `mic_open` (or any
    other identifier that merely *contains* "open") -- an ast.Attribute
    node's `.attr` is compared for exact equality against "write_text"/
    "write_bytes", and a builtin-`open` reference is an ast.Name node,
    which is a completely different node shape from an ast.Attribute named
    "mic_open". A substring scan over raw text cannot make that
    distinction; an AST walk does so for free. This is what let
    self._gate.mic_open() go back to a plain dotted call in wake.py (F42)
    instead of routing through getattr() to dodge the old scan.
    """
    hits: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "open" and isinstance(node.ctx, ast.Load):
            hits.append(f"line {node.lineno}: reference to builtin open")
        if isinstance(node, ast.Attribute) and node.attr in ("write_text", "write_bytes"):
            hits.append(f"line {node.lineno}: .{node.attr}() call")
    return hits


def test_wake_module_source_contains_no_file_write_calls():
    """The ring buffer's whole justification (module docstring PRIVACY
    section) is that it is never persisted. Structural guard: this file
    must contain no filesystem-write call at all -- AST-based (F42), not a
    substring scan, specifically so a legitimately-named method like
    self._gate.mic_open() doesn't collide with it by coincidence of
    spelling (it did, under the old substring scan, and wake.py had to
    route that call through getattr() to dodge it)."""
    source = Path(wake_mod.__file__).read_text(encoding="utf-8")
    hits = _find_disk_write_calls(source)
    assert not hits, f"wake.py must never write to disk: {hits}"


def test_the_disk_write_scanner_actually_detects_a_planted_violation():
    """Guards the guard, same shape as test_source_hygiene.py's own
    zero-files check: a scanner that silently found nothing because it was
    broken (wrong node type, typo'd attr name, etc.) would 'pass' having
    checked nothing. Feeds the same scanning function a synthetic source
    string with a real violation and confirms it's caught."""
    planted = "def f():\n    open('secret_ring_buffer.raw', 'wb')\n"
    hits = _find_disk_write_calls(planted)
    assert hits, "the scanner did not detect a planted call to builtin open()"

    planted_alias = "def f():\n    o = open\n    o('x', 'wb')\n"
    hits_alias = _find_disk_write_calls(planted_alias)
    assert hits_alias, "the scanner did not detect an aliased reference to builtin open"

    planted_write_text = "def f(p):\n    p.write_text('data')\n"
    hits_write_text = _find_disk_write_calls(planted_write_text)
    assert hits_write_text, "the scanner did not detect a planted .write_text() call"


def test_the_disk_write_scanner_does_not_false_positive_on_mic_open():
    """The exact collision that forced wake.py's getattr() workaround under
    the old substring scan: 'mic_open(' contains 'open(' as a substring.
    An AST walk comparing exact node shapes (Attribute.attr == 'mic_open'
    is not Name.id == 'open') doesn't have that problem -- this is what
    makes reverting the getattr() workaround back to a plain dotted call
    safe."""
    source = "def f(self):\n    if self._gate.mic_open():\n        pass\n"
    hits = _find_disk_write_calls(source)
    assert hits == [], f"false positive on a mic_open() call: {hits}"


def test_no_log_call_anywhere_in_the_module_references_an_audio_holding_variable():
    """Structural backstop (same technique as
    test_interfaces_hotkey.py's AST guard on _extract_combo_event): a log
    call anywhere in this module must never be handed a variable that could
    hold raw or derived audio -- so a future edit adding a stray debug log
    of the ring/window/embedding for "temporary debugging" fails the build,
    not just a code review."""
    banned_names = {
        "frame", "audio", "x", "window", "mel", "mel_out", "embedding",
        "emb_out", "clf_input", "preroll", "ring",
    }
    source = Path(wake_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("debug", "info", "warning", "error", "exception", "critical", "log"):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name) and sub.id in banned_names:
                    pytest.fail(
                        f"log call at line {node.lineno} references {sub.id!r}, "
                        "a variable that can hold raw/derived audio"
                    )
                if isinstance(sub, ast.Attribute) and sub.attr in banned_names:
                    pytest.fail(
                        f"log call at line {node.lineno} references .{sub.attr}, "
                        "a variable that can hold raw/derived audio"
                    )


def test_ring_buffer_never_exceeds_its_configured_maximum(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()

    for _ in range(50):
        wake.process(_frame(1000))
        assert len(wake._ring) <= wake._ring_max_samples


async def test_preroll_pcm_is_only_carried_on_the_wake_start_event(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    window = 4
    fake_ort(
        classifier_shape=(1, window, DEFAULT_EMBED_DIM),
        classifier_run_fn=lambda feeds: [np.array(0.99, dtype=np.float32)],
    )

    bus = _FakeBus()
    loop = asyncio.get_running_loop()
    fake_vad = _FakeVad(probs=[0.0] * 200)  # immediate silence -> ends the turn quickly
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=loop, threshold=0.5, vad=fake_vad,
        vad_silence_ms=10, arm_timeout_ms=60_000,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) >= 1)

    start_event = bus.published[0][1]
    assert start_event.preroll is not None
    assert isinstance(start_event.preroll, np.ndarray)

    # Drive past the grace period and the configured silence duration so the
    # turn ends via VAD.
    for _ in range(5):
        wake.process(_frame(int(SAMPLERATE * SPEECH_GRACE_S / 4)))
    await _wait_until(lambda: len(bus.published) >= 2)

    end_event = bus.published[1][1]
    assert end_event.preroll is None, "raw PCM must not ride on wake.end"


async def test_preroll_length_never_exceeds_the_configured_preroll_samples(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()
    bus = _FakeBus()
    wake = _make_wake(wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), preroll_ms=500)
    wake.start()

    for _ in range(10):
        wake.process(_frame(2000))

    with wake._state_lock:
        wake._arm_locked(score=0.99)
    await _wait_until(lambda: len(bus.published) == 1)

    preroll = bus.published[0][1].preroll
    assert len(preroll) <= wake._preroll_samples


# ---------------------------------------------------------------------------
# Wake and push-to-talk are interchangeable arming sources
# ---------------------------------------------------------------------------


def test_wake_event_field_names_are_a_superset_of_ptt_events():
    """The turn loop must not care which arming source fired. WakeEvent
    carries everything PTTEvent does (ts, mode) plus the one field wake
    genuinely needs that PTT doesn't (preroll)."""
    import dataclasses

    from nova.interfaces.hotkey import PTTEvent

    ptt_fields = {f.name for f in dataclasses.fields(PTTEvent)}
    wake_fields = {f.name for f in dataclasses.fields(WakeEvent)}
    assert ptt_fields <= wake_fields


def test_wake_topics_follow_the_same_domain_dot_start_end_naming_as_ptt():
    from nova.interfaces.hotkey import TOPIC_PTT_END, TOPIC_PTT_START

    assert TOPIC_WAKE_START.endswith(".start")
    assert TOPIC_WAKE_END.endswith(".end")
    assert TOPIC_PTT_START.endswith(".start")
    assert TOPIC_PTT_END.endswith(".end")


# ---------------------------------------------------------------------------
# State machine: IDLE detection cadence, arming, VAD-driven end of turn
# ---------------------------------------------------------------------------


async def test_detection_only_runs_once_per_mel_step_samples_of_new_audio(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()  # classifier always scores 0.0, well under threshold
    wake = _make_wake(wake_model_dir, loop=asyncio.get_running_loop())
    wake.start()

    classifier_calls_after_warmup = wake._classifier_session.run_calls.__len__()

    wake.process(_frame(MEL_STEP_SAMPLES - 1))  # not quite enough for a step yet
    assert len(wake._classifier_session.run_calls) == classifier_calls_after_warmup

    wake.process(_frame(1))  # crosses the threshold -> exactly one more step
    assert len(wake._classifier_session.run_calls) == classifier_calls_after_warmup + 1


async def test_score_above_threshold_arms_and_publishes_wake_start(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    bus = _FakeBus()
    wake = _make_wake(wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55)
    wake.start()

    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)

    assert bus.published[0][0] == TOPIC_WAKE_START
    assert wake._state.value == "armed"


async def test_score_below_threshold_never_arms(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.1, dtype=np.float32)])
    bus = _FakeBus()
    wake = _make_wake(wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55)
    wake.start()

    for _ in range(5):
        wake.process(_frame(MEL_STEP_SAMPLES))
    await asyncio.sleep(0.05)

    assert bus.published == []
    assert wake._state.value == "idle"


async def test_armed_state_polls_vad_instead_of_the_classifier(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    fake_vad = _FakeVad(probs=[1.0] * 50)
    wake = _make_wake(
        wake_model_dir, loop=asyncio.get_running_loop(), threshold=0.55, vad=fake_vad,
    )
    wake.start()

    wake.process(_frame(MEL_STEP_SAMPLES))  # arms
    calls_at_arm = len(wake._classifier_session.run_calls)

    wake.process(_frame(480))  # one ARMED-state frame
    assert len(fake_vad.process_calls) == 1
    assert len(wake._classifier_session.run_calls) == calls_at_arm  # classifier untouched while armed


async def test_vad_reports_speech_then_silence_ends_the_turn_after_the_configured_duration(
    wake_model_dir, fake_ort
):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    # Speech immediately, then silence -- past the grace period.
    fake_vad = _FakeVad(probs=[1.0, 1.0] + [0.0] * 200)
    bus = _FakeBus()
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55, vad=fake_vad,
        vad_silence_ms=100, arm_timeout_ms=60_000,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)

    frame_n = int(SAMPLERATE * 0.03)  # 30ms frames
    for _ in range(30):
        wake.process(_frame(frame_n))
    await _wait_until(lambda: len(bus.published) == 2)

    assert bus.published[1][0] == TOPIC_WAKE_END
    assert bus.published[1][1].mode == "vad_end"
    assert wake._state.value == "idle"


async def test_silence_within_the_speech_grace_period_does_not_end_the_turn(wake_model_dir, fake_ort):
    """A natural pre-speech pause (the beat between the wake word and the
    command) must not end the turn instantly."""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    fake_vad = _FakeVad(probs=[0.0] * 200)  # silence throughout
    bus = _FakeBus()
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55, vad=fake_vad,
        vad_silence_ms=50, arm_timeout_ms=60_000,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)

    # Feed less audio than SPEECH_GRACE_S -- still inside the grace window.
    grace_frame = int(SAMPLERATE * SPEECH_GRACE_S * 0.5)
    wake.process(_frame(grace_frame))
    await asyncio.sleep(0.02)

    assert len(bus.published) == 1  # still armed, no premature end
    assert wake._state.value == "armed"


async def test_a_speech_frame_resets_the_silence_run(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    # past grace, then silence, then speech again, then final silence run
    # that alone is not long enough to have ended the turn without the reset.
    fake_vad = _FakeVad(probs=[1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
    bus = _FakeBus()
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55, vad=fake_vad,
        vad_silence_ms=200, arm_timeout_ms=60_000,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)

    frame_n = int(SAMPLERATE * 0.05)  # 50ms frames
    for _ in range(7):
        wake.process(_frame(frame_n))

    assert len(bus.published) == 1  # not ended: the mid-stream speech frame reset the silence run
    assert wake._state.value == "armed"


# ---------------------------------------------------------------------------
# Arm-timeout failsafe: ARMED must never be able to last forever
# ---------------------------------------------------------------------------


class _FakeTimer:
    instances: list["_FakeTimer"] = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False
        _FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def fake_timer(monkeypatch):
    _FakeTimer.instances.clear()
    monkeypatch.setattr(threading, "Timer", _FakeTimer)
    return _FakeTimer


async def test_arm_timer_is_scheduled_with_the_configured_duration(wake_model_dir, fake_ort, fake_timer):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    wake = _make_wake(
        wake_model_dir, loop=asyncio.get_running_loop(), threshold=0.55, arm_timeout_ms=12345,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: wake._state.value == "armed")

    assert len(fake_timer.instances) == 1
    timer = fake_timer.instances[-1]
    assert timer.interval == pytest.approx(12.345)
    assert timer.started is True


async def test_arm_timeout_firing_ends_the_turn_with_arm_timeout_mode(wake_model_dir, fake_ort, fake_timer):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    bus = _FakeBus()
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55, arm_timeout_ms=30_000,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)

    timer = fake_timer.instances[-1]
    timer.function()  # fire the timeout callback directly, no real waiting

    await _wait_until(lambda: len(bus.published) == 2)
    assert bus.published[1][0] == TOPIC_WAKE_END
    assert bus.published[1][1].mode == "arm_timeout"
    assert wake._state.value == "idle"


async def test_arm_timeout_does_not_fire_if_backend_became_unavailable_meanwhile(
    wake_model_dir, fake_ort, fake_timer
):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    bus = _FakeBus()
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55, arm_timeout_ms=30_000,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)

    wake.backend_available = False  # e.g. stop() ran before the timer fired
    timer = fake_timer.instances[-1]
    timer.function()

    await asyncio.sleep(0.05)
    assert len(bus.published) == 1  # no spurious wake.end


async def test_vad_ending_the_turn_cancels_the_pending_arm_timer(wake_model_dir, fake_ort, fake_timer):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    fake_vad = _FakeVad(probs=[1.0, 1.0] + [0.0] * 200)
    bus = _FakeBus()
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55, vad=fake_vad,
        vad_silence_ms=50, arm_timeout_ms=30_000,
    )
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)
    armed_timer = fake_timer.instances[-1]
    assert armed_timer.cancelled is False

    frame_n = int(SAMPLERATE * 0.05)
    for _ in range(20):
        wake.process(_frame(frame_n))
    await _wait_until(lambda: len(bus.published) == 2)

    assert armed_timer.cancelled is True
    assert bus.published[1][1].mode == "vad_end"


# ---------------------------------------------------------------------------
# process(): must never let an exception escape
# ---------------------------------------------------------------------------


def test_process_never_propagates_an_internal_exception(wake_model_dir, fake_ort, monkeypatch):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()

    def boom(*a, **kw):
        raise RuntimeError("simulated internal failure deep in the DSP pipeline")

    monkeypatch.setattr(wake, "_run_detection_step", boom)
    wake.process(_frame(MEL_STEP_SAMPLES))  # must not raise


def test_process_exception_log_never_contains_the_exception_message(wake_model_dir, fake_ort, monkeypatch, caplog):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()

    secret = "SUPER_SECRET_ACOUSTIC_DERIVED_TEXT"

    def boom(*a, **kw):
        raise RuntimeError(secret)

    monkeypatch.setattr(wake, "_run_detection_step", boom)
    with caplog.at_level("WARNING"):
        wake.process(_frame(MEL_STEP_SAMPLES))

    joined = " ".join(r.message for r in caplog.records)
    assert secret not in joined
    assert "RuntimeError" in joined


def test_empty_frame_is_a_safe_noop(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()
    wake.process(np.zeros(0, dtype=np.float32))  # must not raise


# ---------------------------------------------------------------------------
# start()/stop(): idempotency and clean reset
# ---------------------------------------------------------------------------


def test_start_is_idempotent_and_does_not_reload_sessions(wake_model_dir, fake_ort):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake = fake_ort()
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.start()
    paths_after_first = len(fake.created_paths)

    wake.start()  # second call must not re-load

    assert len(fake.created_paths) == paths_after_first


def test_stop_before_start_does_not_raise(wake_model_dir):
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())
    wake.stop()  # must not raise


async def test_stop_resets_ring_backend_and_cancels_any_pending_timer(wake_model_dir, fake_ort, fake_timer):
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    bus = _FakeBus()
    wake = _make_wake(wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55)
    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: wake._state.value == "armed")
    armed_timer = fake_timer.instances[-1]

    wake.stop()

    assert wake.backend_available is False
    assert len(wake._ring) == 0
    assert armed_timer.cancelled is True
    assert wake._state.value == "idle"


# ---------------------------------------------------------------------------
# T1.7 (F26): DuplexGate integration -- process() must be genuinely inert
# while muted, not just "doesn't publish a wake event." nova.audio.duplex
# has its own dedicated test file (test_audio_duplex.py) for the gate's own
# hangover/fail-closed logic; these tests pin WakeWord's *use* of a gate.
# ---------------------------------------------------------------------------


async def test_process_is_genuinely_inert_while_the_gate_reports_mic_closed(wake_model_dir, fake_ort):
    """Ring untouched, embeddings untouched, classifier untouched -- as if
    process() were never called at all. Checked by array/state equality,
    not just "no wake event published", per the module's own process()
    docstring: 'the frame is not pushed to the ring, not scored, not fed
    to the VAD.'"""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    fake_vad = _FakeVad()
    closed_gate = _FakeGate(open_=False)
    wake = _make_wake(
        wake_model_dir, loop=asyncio.get_running_loop(), vad=fake_vad, gate=closed_gate,
    )
    wake.start()

    ring_before = wake._ring.copy()
    embeddings_before = list(wake._embeddings)
    classifier_calls_before = len(wake._classifier_session.run_calls)

    wake.process(_frame(MEL_STEP_SAMPLES, value=0.3))  # real, non-trivial audio content

    assert closed_gate.mic_open_calls >= 1, "process() never even consulted the gate"
    assert np.array_equal(wake._ring, ring_before), "the ring was mutated while the mic should be closed"
    assert list(wake._embeddings) == embeddings_before
    assert len(wake._classifier_session.run_calls) == classifier_calls_before
    assert len(fake_vad.process_calls) == 0, "VAD was fed a frame while the mic should be closed"


async def test_process_behaves_normally_when_the_gate_reports_mic_open(wake_model_dir, fake_ort):
    """Positive control: a gate that exists and says 'open' must not
    silently break ordinary detection -- distinct from both 'no gate at
    all' and 'gate present but closed'."""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort(classifier_run_fn=lambda feeds: [np.array(0.9, dtype=np.float32)])
    open_gate = _FakeGate(open_=True)
    bus = _FakeBus()
    wake = _make_wake(
        wake_model_dir, bus=bus, loop=asyncio.get_running_loop(), threshold=0.55, gate=open_gate,
    )
    wake.start()

    wake.process(_frame(MEL_STEP_SAMPLES))
    await _wait_until(lambda: len(bus.published) == 1)

    assert open_gate.mic_open_calls >= 1
    assert bus.published[0][0] == TOPIC_WAKE_START


def test_no_gate_at_all_is_a_distinct_case_from_a_gate_that_is_closed(wake_model_dir, fake_ort):
    """__init__'s own docstring: omitting a gate entirely is a deliberate,
    distinct case from a gate that exists but fails closed -- an offline
    scoring harness with no live Player/speaking-state concept to gate
    against still needs to run undisturbed. Default construction (no
    gate= kwarg) must leave _gate as None, and process() must not attempt
    to consult anything when it is."""
    _touch(wake_model_dir, "melspectrogram.onnx")
    _touch(wake_model_dir, "embedding_model.onnx")
    _touch(wake_model_dir, "clf.onnx")
    fake_ort()
    wake = _make_wake(wake_model_dir, loop=asyncio.new_event_loop())

    assert wake._gate is None

    wake.start()
    wake.process(_frame(MEL_STEP_SAMPLES))  # must not raise, must not touch a gate that doesn't exist


# ---------------------------------------------------------------------------
# _publish(): no event loop -> warn and drop, never raise
# ---------------------------------------------------------------------------


def test_publish_with_no_event_loop_warns_and_drops_the_event(wake_model_dir, caplog):
    wake = _make_wake(wake_model_dir)  # no loop ever provided
    with caplog.at_level("WARNING"):
        wake._publish(TOPIC_WAKE_START, WakeEvent(ts=0.0))  # must not raise
    assert any("no event loop" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Config precedence: explicit constructor args win, missing config degrades
# ---------------------------------------------------------------------------


def test_explicit_threshold_overrides_config(wake_model_dir, monkeypatch):
    import nova.config as config_mod

    def fake_load_config():
        raise config_mod.ConfigError("no config in this test")

    monkeypatch.setattr(config_mod, "load_config", fake_load_config)
    wake = _make_wake(wake_model_dir, threshold=0.91)
    assert wake._threshold == 0.91


def test_missing_config_degrades_to_defaults_not_a_crash(wake_model_dir, monkeypatch, caplog):
    import nova.config as config_mod

    def fake_load_config():
        raise config_mod.ConfigError("no config in this test")

    monkeypatch.setattr(config_mod, "load_config", fake_load_config)
    with caplog.at_level("WARNING"):
        wake = _make_wake(wake_model_dir)
    assert wake._threshold == wake_mod.DEFAULT_THRESHOLD
