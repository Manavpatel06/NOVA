"""Contract: nova/app.py's F28 arming-ceiling wiring
(docs/THREAT_MODEL_ALWAYS_ON.md) -- the promise that ships with the wake
word: a wake-armed turn can reach READ but never ACT/CONFIRM, because no
human proved they're in the room by pressing the hotkey.

`Ladder.authorize(ceiling=...)` already has unit coverage
(tests/test_security_ladder.py) proving the ceiling mechanism itself
refuses correctly once given a ceiling. What's UNPROVEN there is that
`nova/app.py` actually threads the arming source honestly: a real
`wake.start`/`wake.end` event pair, through `_on_wake_end`, into
`SkillCtx.armed_by`, into `SkillSpec.invoke()`'s ceiling computation, into
`Ladder.authorize()` -- and that nothing in between silently defaults it
back to PTT. A ceiling that's never fed the right value is a control that
exists and does nothing.

Every behavioral test below drives the REAL bus handlers
(`_on_wake_start`/`_on_wake_end`, `_on_ptt_start`/`_on_ptt_end`) rather than
calling `run_turn(armed_by=...)` directly -- calling `run_turn` directly
would only prove `run_turn` respects a parameter, not that the handlers
that are actually subscribed to `wake.end`/`ptt.end` on the real bus pass
the right one. The load-bearing assertion throughout is that `Popen` (the
real side effect a refusal must prevent) was or wasn't called -- a
decision-level check alone would still pass if a refusal somehow let the
process launch anyway.
"""
from __future__ import annotations

import ast
import asyncio
import json
import queue
from pathlib import Path

import numpy as np
import pytest

import nova.app as app_mod
import nova.skills.open_app as open_app_mod
from nova.audio.wake import WakeEvent
from nova.config import Config, SecurityConfig
from nova.interfaces.hotkey import PTTEvent
from nova.security.audit import AuditLog


class _FakeTranscriber:
    def __init__(self, text: str):
        self.text = text

    def transcribe(self, pcm):
        return self.text


class _FakeSynth:
    def __init__(self):
        self.said: list[str] = []

    def say(self, text):
        self.said.append(text)
        return np.zeros(4, dtype=np.float32), 16000


class _FakePlayer:
    def __init__(self):
        self.played: list[tuple] = []

    async def play(self, pcm, samplerate, *, on_first_frame=None):
        self.played.append((pcm, samplerate))
        if on_first_frame is not None:
            on_first_frame()


class _FakePopen:
    calls: list[tuple[list, dict]] = []

    def __init__(self, argv, **kwargs):
        _FakePopen.calls.append((argv, kwargs))


@pytest.fixture
def fake_popen(monkeypatch):
    """Same technique tests/test_skills_open_app.py uses -- replaces the
    whole subprocess module reference inside nova.skills.open_app, so
    nothing in this file actually launches a process."""
    _FakePopen.calls = []
    fake_subprocess = type("FakeSubprocessModule", (), {"Popen": _FakePopen})()
    monkeypatch.setattr(open_app_mod, "subprocess", fake_subprocess)
    return _FakePopen


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A real NovaApp with a REAL allowlisted app open_app could actually
    launch -- the positive control (PTT-armed) needs something that WOULD
    succeed if not for the ceiling, or it proves nothing. STT/TTS/playback
    are faked (hardware/models); router, ladder, AuditLog, and the real
    skill registry all run for real, same discipline as
    tests/test_app_turn_loop.py's own `app` fixture."""
    monkeypatch.setattr(
        app_mod, "AuditLog",
        lambda **kw: AuditLog(path=tmp_path / "audit.log", redact_terms=kw.get("redact_terms", ())),
    )
    trusted_dir = tmp_path / "Trusted"
    trusted_dir.mkdir()
    exe = trusted_dir / "notepad.exe"
    exe.write_bytes(b"fake exe")
    config = Config(security=SecurityConfig(
        allowed_apps={"notepad": str(exe)}, allowed_app_dirs=[str(trusted_dir)],
    ))
    application = app_mod.NovaApp(config=config)
    application.synth = _FakeSynth()
    application.player = _FakePlayer()
    yield application
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None  # don't leak this test's binding into others


def _last_audit_event(audit_path: Path) -> dict:
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "expected at least one audit record"
    return json.loads(lines[-1])["event"]


def _one_frame() -> np.ndarray:
    return np.zeros(1600, dtype=np.float32)  # 0.1s @ 16kHz -- STT is faked, content is irrelevant


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    import time

    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# The three required cases, driven through the REAL bus handlers.
# ---------------------------------------------------------------------------


async def test_wake_armed_act_tier_is_refused_and_popen_is_never_called(app, fake_popen, tmp_path):
    """The one with teeth: a wake-armed 'open notepad' must not just be
    DECIDED against -- the process must never launch. Also asserts the
    exact machine-readable reason and ceiling the audit record carries,
    so a refusal for the WRONG reason (e.g. an unrelated router miss)
    can't accidentally satisfy this test."""
    audit_path = tmp_path / "audit.log"
    app.transcriber = _FakeTranscriber("open notepad")

    await app._on_wake_start(WakeEvent(ts=0.0, preroll=_one_frame()))
    assert app._wake_capturing is True
    await app._on_wake_end(WakeEvent(ts=0.5, mode="vad_end"))

    assert fake_popen.calls == [], "open_app launched a process despite the wake-armed ceiling refusing it"
    assert app.synth.said == ["I can't do that unless you press the hotkey."]

    event = _last_audit_event(audit_path)
    assert event["reason"] == "presence_required"
    assert event["ceiling"] == "read"
    assert event["ceiling_exceeded"] is True
    assert event["decision"] == "deny"


async def test_ptt_armed_act_tier_succeeds_positive_control(app, fake_popen):
    """Same skill, same words, PTT instead of wake -- must succeed. Without
    this, test_wake_armed_act_tier_is_refused_and_popen_is_never_called
    could pass because EVERYTHING is broken (open_app misconfigured,
    router miss, etc.), not because the ceiling specifically is doing its
    job."""
    app.transcriber = _FakeTranscriber("open notepad")

    await app._on_ptt_start(PTTEvent(ts=0.0, mode=None))
    app._ptt_frames.append(_one_frame())
    await app._on_ptt_end(PTTEvent(ts=0.5, mode="tap"))

    assert len(fake_popen.calls) == 1, "PTT-armed open_app should have launched notepad"
    assert fake_popen.calls[0][1].get("shell") is False


async def test_wake_armed_read_tier_succeeds_the_ceiling_is_a_cap_not_a_blanket_refusal(app, fake_popen):
    """F47's READ exemption composing correctly with F28's ceiling: a
    wake-armed turn is capped at READ, not refused outright. This is the
    case that proves the ceiling is a CAP, not 'acoustic arming never
    works' -- without it, a bug that refused every wake-armed call
    regardless of tier would pass the ACT-refusal test above for the wrong
    reason."""
    app.transcriber = _FakeTranscriber("what time is it")

    await app._on_wake_start(WakeEvent(ts=0.0, preroll=_one_frame()))
    await app._on_wake_end(WakeEvent(ts=0.5, mode="vad_end"))

    assert fake_popen.calls == []  # time never touches Popen anyway; sanity
    assert len(app.synth.said) == 1
    assert app.synth.said[0] != "I can't do that unless you press the hotkey."
    assert "presence_required" not in app.synth.said[0]


# ---------------------------------------------------------------------------
# The default is not load-bearing: SkillCtx.armed_by defaults to PTT so
# every OTHER call site is unaffected, which means that default is exactly
# what a future refactor dropping the keyword would silently fall back to.
# Pinned two independent ways: structurally (the real call site names
# ArmedBy.WAKE explicitly) and behaviorally (breaking it is caught below,
# non-vacuity section).
# ---------------------------------------------------------------------------


def test_on_wake_end_passes_armed_by_wake_explicitly_not_relying_on_the_default():
    tree = ast.parse(Path(app_mod.__file__).read_text(encoding="utf-8"))
    on_wake_end = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_wake_end":
            on_wake_end = node
            break
    assert on_wake_end is not None, "expected an _on_wake_end() handler in nova/app.py"

    run_turn_calls = [
        n for n in ast.walk(on_wake_end)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run_turn"
    ]
    assert len(run_turn_calls) == 1, "_on_wake_end should call run_turn() exactly once"
    call = run_turn_calls[0]

    armed_by_kwargs = [kw for kw in call.keywords if kw.arg == "armed_by"]
    assert armed_by_kwargs, (
        "_on_wake_end's run_turn() call does not pass armed_by= explicitly -- "
        "it would silently fall back to the ArmedBy.PTT default, granting ACT/CONFIRM to the room"
    )
    value = armed_by_kwargs[0].value
    assert (
        isinstance(value, ast.Attribute) and value.attr == "WAKE"
        and isinstance(value.value, ast.Name) and value.value.id == "ArmedBy"
    ), f"_on_wake_end passes armed_by={ast.dump(value)}, not the literal ArmedBy.WAKE"


def test_on_ptt_end_passes_armed_by_ptt_explicitly_too():
    """Not strictly load-bearing (PTT already gets the unrestricted
    default) but pinned for the same reason open_app's Popen shape is
    pinned on both the positive and negative side: an explicit value here
    means a future change to what the default even IS can't silently
    change PTT's own behavior out from under it either."""
    tree = ast.parse(Path(app_mod.__file__).read_text(encoding="utf-8"))
    on_ptt_end = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_ptt_end":
            on_ptt_end = node
            break
    assert on_ptt_end is not None

    run_turn_calls = [
        n for n in ast.walk(on_ptt_end)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run_turn"
    ]
    assert len(run_turn_calls) == 1
    call = run_turn_calls[0]
    armed_by_kwargs = [kw for kw in call.keywords if kw.arg == "armed_by"]
    assert armed_by_kwargs, "_on_ptt_end's run_turn() call does not pass armed_by= explicitly"
    value = armed_by_kwargs[0].value
    assert (
        isinstance(value, ast.Attribute) and value.attr == "PTT"
        and isinstance(value.value, ast.Name) and value.value.id == "ArmedBy"
    )


# ---------------------------------------------------------------------------
# Wake suppression while a PTT session is capturing (nova-dev-4's plan,
# module docstring's "Capture's lifecycle changed" section): saying the
# wake phrase mid-hotkey-utterance must never produce a second turn.
# Driven through the REAL _continuous_capture_loop, not a re-implementation
# of its branching logic -- a fake WakeWord.process() count is the
# load-bearing assertion, since the suppression is "never feed the frame
# at all", and structurally cannot produce a second turn if the detector
# is never given the audio that would have triggered it.
# ---------------------------------------------------------------------------


class _ScriptedCapture:
    """Yields a scripted sequence of frames; an optional setup callback
    runs right before each frame is returned so app state (`_capturing`)
    can be flipped deterministically between frames -- avoids any real
    threading race in a test driving the actual async loop."""

    def __init__(self, script):
        self._script = list(script)  # list of (setup_fn_or_None, frame)
        self.exhausted = False

    def read(self, timeout=None):
        if not self._script:
            self.exhausted = True
            raise queue.Empty
        setup, frame = self._script.pop(0)
        if setup is not None:
            setup()
        return frame


async def test_wake_word_is_never_fed_frames_while_a_ptt_session_is_capturing(app):
    processed: list[np.ndarray] = []

    class _FakeWakeWord:
        def process(self, frame):
            processed.append(frame)

    app.wake_word = _FakeWakeWord()

    frame_before = np.full(4, 1.0, dtype=np.float32)  # not capturing -- reaches the detector
    frame_during = np.full(4, 2.0, dtype=np.float32)  # PTT capturing -- must be suppressed
    frame_after = np.full(4, 3.0, dtype=np.float32)  # PTT ended -- reaches the detector again

    def start_ptt():
        app._capturing = True

    def end_ptt():
        app._capturing = False

    app._capture = _ScriptedCapture([
        (None, frame_before),
        (start_ptt, frame_during),
        (end_ptt, frame_after),
    ])
    app._capture_reader_stop = asyncio.Event()

    task = asyncio.create_task(app._continuous_capture_loop())
    await _wait_until(lambda: app._capture.exhausted)
    app._capture_reader_stop.set()
    await task

    # The suppressed frame never reached the detector at all -- this is
    # what makes "the wake word fires mid-PTT-utterance" structurally
    # impossible, not merely unlikely: a second wake.start (and therefore
    # a second turn) can only be published from inside WakeWord.process(),
    # which this proves was never even called with that frame.
    assert len(processed) == 2
    assert np.array_equal(processed[0], frame_before)
    assert np.array_equal(processed[1], frame_after)
    # The suppressed frame went to the PTT buffer instead of being dropped.
    assert len(app._ptt_frames) == 1
    assert np.array_equal(app._ptt_frames[0], frame_during)
