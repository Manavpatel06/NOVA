"""The duplex gate (T1.7, finding F26) — mutes wake-word detection while
NOVA is speaking, so the detector never gets a clean shot at NOVA's own
voice coming back through the mic.

**Why this exists at all.** The retired build hit this exact bug, wrote it
up as *"a loop, not a glitch"* (NOVA hears its own reply, treats part of it
as a fresh wake word or command, and can re-trigger itself), and shipped a
half-duplex mute to close it. This tree has no equivalent: `Player.play()`
and `WakeWord.process()` don't know about each other at all. It has been
masked so far only as a side effect of push-to-talk's shape — PTT closes
`Capture` before `NovaApp._speak()` ever runs, so there has been no window
for the loop to occur, not a control that prevents it. Wiring the wake word
into the turn loop without this gate reproduces a solved bug on purpose,
which is why this is a precondition for that wiring, not a follow-up to it.

**Half-duplex, not AEC, and that is a deliberate scope decision, not a
shortcut.** Full acoustic echo cancellation is explicitly out of scope:
neither of the two common Windows AEC backends has a reliable wheel for
this platform, and the retired build reached the same conclusion after
trying. Mute-while-speaking-plus-a-hangover is the entire mechanism here.
It cannot support real barge-in (interrupting NOVA mid-sentence by voice)
-- push-to-talk remains the deliberate barge-in path
(`Ctrl+Alt+Space` cancels playback inline, `nova.interfaces.hotkey`) and is
untouched by this module.

**Bus-driven, not a direct call into `Player`.** `TOPIC_SPEAK_START`/
`TOPIC_SPEAK_END` already exist (owned by `nova.interfaces.tray`, published
by `NovaApp._speak()` — see that module's docstring for why the topic is
owned there rather than in `nova.app`) and the tray already subscribes to
them for its own purposes. `DuplexGate` is a third subscriber, not a new
coupling between `nova.audio.player` and `nova.audio.wake` — those two
modules still don't know about each other.

**Fail closed, stated precisely**: a `DuplexGate` that was never subscribed
to a real bus (no bus passed at construction) starts, and stays, muted --
`mic_open()` returns False forever, never True. Making the gate itself
require positive proof of a live event channel before it will ever say
"open" is the whole point: a gate that silently defaults to *open* when its
wiring is broken is indistinguishable from no gate, and it would fail open
exactly when something has already gone wrong. Once a `DuplexGate` *is*
subscribed, its initial state genuinely is open (`mic_open() -> True`) --
that is not the failure case, it is the correct answer for "NOVA hasn't
spoken since boot, nothing to protect against yet." The failure case this
guards is a broken subscription, not a quiet boot.

**Once speaking starts, only a real end event reopens the mic.** There is
no independent timeout that force-reopens `mic_open()` if `on_speak_end()`
is late or never arrives (e.g. a crash mid-utterance). Assuming "it's
probably done by now" is exactly the optimistic, fail-open behavior this
gate exists to refuse.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from nova.interfaces.tray import TOPIC_SPEAK_END, TOPIC_SPEAK_START  # owned there; see module docstring
from nova.log import get_logger

if TYPE_CHECKING:
    from nova.bus import EventBus

_log = get_logger(__name__)

# The retired build's number (180ms), inherited rather than re-derived: its
# own reasoning was speaker decay plus WASAPI output buffering meaning the
# last few tens of milliseconds of audio can still be arriving/ringing in
# the room slightly after Player.play() itself returns. That reasoning
# still applies here (same OS-level output stack) but was never actually
# measured against this machine's speakers, this room, or this Player
# implementation -- it is a documented, revisable starting point, the same
# honesty standard as nova.interfaces.hotkey's DEFAULT_TAP_THRESHOLD_MS,
# not a finding. Rounded up slightly (180 -> 200) for a little extra margin
# given it is unmeasured here specifically.
DEFAULT_HANGOVER_MS = 200


def _default_duplex_config():
    """DuplexConfig may not have landed in nova.config yet, or config/
    nova.yaml may be missing/malformed -- degrade loudly rather than
    silently using the built-in default, same pattern as
    nova.interfaces.hotkey._default_hotkey_config and
    nova.audio.wake._default_wake_config."""
    try:
        from nova.config import load_config

        cfg = load_config()
        duplex_cfg = getattr(cfg, "duplex", None)
        if duplex_cfg is not None:
            return duplex_cfg
        _log.warning(
            "nova.config.Config has no 'duplex' section yet; using the "
            "built-in hangover default (%dms).",
            DEFAULT_HANGOVER_MS,
        )
    except Exception as exc:
        _log.warning(
            "Could not load config/nova.yaml (%s); using the built-in "
            "hangover default (%dms).",
            exc, DEFAULT_HANGOVER_MS,
        )
    return None


class DuplexGate:
    """Half-duplex mute-while-speaking gate. One instance, constructed at
    boot and subscribed to the real `EventBus`, shared by every consumer
    that needs to know "is it safe to listen for the wake word right now"
    (today: `WakeWord.process()`).
    """

    def __init__(self, bus: "EventBus | None" = None, *, hangover_ms: int | None = None) -> None:
        self._lock = threading.Lock()
        self._speaking = False
        self._hangover_until: float | None = None  # monotonic timestamp, or None
        if hangover_ms is not None:
            resolved_hangover_ms = hangover_ms
        else:
            cfg = _default_duplex_config()
            resolved_hangover_ms = cfg.hangover_ms if cfg is not None else DEFAULT_HANGOVER_MS
        self._hangover_s = resolved_hangover_ms / 1000.0
        # See module docstring's "fail closed" section: no bus means no way
        # to ever learn that speaking has started or ended, so this gate
        # must never claim to be open. `_subscribed` is the single fact
        # mic_open() checks first, before anything else.
        self._subscribed = False

        if bus is not None:
            bus.subscribe(TOPIC_SPEAK_START, self._on_speak_start_event)
            bus.subscribe(TOPIC_SPEAK_END, self._on_speak_end_event)
            self._subscribed = True
        else:
            _log.warning(
                "DuplexGate constructed with no bus -- it cannot learn when NOVA "
                "starts or stops speaking, so mic_open() will return False "
                "permanently (fail closed, not a silent always-open gate)."
            )

    def mic_open(self) -> bool:
        """True only when it is safe to feed audio to the wake-word
        detector: a live bus subscription exists, NOVA is not currently
        speaking, and the post-speech hangover has elapsed."""
        with self._lock:
            if not self._subscribed:
                return False
            if self._speaking:
                return False
            if self._hangover_until is not None and time.monotonic() < self._hangover_until:
                return False
            return True

    def on_speak_start(self) -> None:
        """Called (directly, or via the bus subscription below) the instant
        NOVA begins speaking. Clears any pending hangover from a prior
        utterance -- a new utterance starting supersedes it; there is
        nothing left to "cool down" from once speech has resumed."""
        with self._lock:
            self._speaking = True
            self._hangover_until = None

    def on_speak_end(self) -> None:
        """Called the instant NOVA finishes speaking. Starts the hangover
        window; mic_open() stays False until it elapses."""
        with self._lock:
            self._speaking = False
            self._hangover_until = time.monotonic() + self._hangover_s

    # -- bus handlers (must be async: nova.bus.EventBus.Handler) -----------

    async def _on_speak_start_event(self, _payload: Any) -> None:
        self.on_speak_start()

    async def _on_speak_end_event(self, _payload: Any) -> None:
        self.on_speak_end()
