"""Load and validate config/nova.yaml — the one HUMAN-authored config file
(docs/CONTRACTS.md).

Design rules this module enforces:
  - Unknown keys anywhere in the file are a hard error naming the offending
    key and what was expected, not a silent ignore.
  - Every field below has a real consumer in another M0 module; do not add a
    key here unless you can point at the code that reads it.
  - No module reads os.environ or hardcodes a path — this is the only place
    nova.yaml is parsed, and nova/__init__.py is the only place paths live.

**T2.3a's second, machine-written source**: `nova.LEARNED_APPS_PATH`
(`data/learned_apps.json`), merged into `Config.security.allowed_apps` at
the end of `load_config()`. Deliberately a separate file rather than a
runtime rewrite of nova.yaml itself — nova.yaml is hand-authored and its
comments are load-bearing (the wake threshold's whole derivation, F27/F41,
lives in one; see WakeConfig.threshold below). A dump-and-rewrite of the
parsed dict back to YAML would destroy every one of them the first time
`nova.skills.add_app` wrote an entry, and a naive comment-preserving-YAML
library is one more dependency and one more way a machine write could
subtly corrupt a file a human also hand-edits. Machine-written state and
human-authored config have different lifecycles (this file's own git
history changes on a `git commit`; `learned_apps.json`'s changes on
whatever Manav approves by voice) — the same reasoning `data/`'s existing
contents (audit.log, the two profile caches) already follow, applied here
for the first time to something that feeds a *security* boundary rather
than a cache.

The merge rule, load-bearing and simple on purpose: nova.yaml's own
`security.allowed_apps` always wins on a name conflict. The learned file
can only ADD a name nova.yaml doesn't already configure — it can never
override or shadow a human-configured entry, and it can never add or widen
`allowed_app_dirs` (the actual trust boundary) at all; that stays
exclusively human-controlled, on purpose (nova.skills.add_app's own module
docstring explains what this costs in practice).

**F62/F63 (nova-security): reconciliation against the audit log, not
unconditional trust.** Planting `learned_apps.json` directly is strictly
easier than the voice path it was built to protect — it skips Start Menu
resolution, the CONFIRM prompt, `add_app`'s three-layer TOCTOU check, AND
the audit trail, all in the one step of editing a file. `open_app`'s own
containment check still bounds the damage (a planted entry can't point
outside `allowed_app_dirs`), but a planted entry aimed at a legitimate
in-boundary binary would otherwise load and launch under any friendly name
with zero record of how it got there — precisely the silent-grant failure
`add_app` exists to prevent, reached by skipping the mechanism rather than
breaking it. So every entry in the learned file is checked against
`nova.AUDIT_LOG_PATH` for a matching `skill.add_app.learned` record
(`nova.security.ladder.Ladder.record_event()`'s own write) before it is
trusted; an orphan — present in the file, absent from the log — is logged
loudly and dropped from the merge, never silently trusted by default. This
is a plain content scan, not a cryptographic replay of the audit chain's
hash links: per nova-security's own framing, planting is already inside
the attacker model for every file under `data/`, audit.log included, so
verifying hash-chain integrity here would not raise the bar against that
same attacker, only cost more at every boot for no real gain. This makes
an inconsistent state DETECTABLE — the honest ceiling nova-security named
for this fix — not cryptographically prevented.

**F65 (nova-security, HIGH): a grant can be reversed, so "matching
record" means the LATEST one, not just any one.** `add_app` now records
the grant before its own round-trip verification (verifying first
deadlocked every call — every fresh write looked like an orphan to this
exact reconciliation, since its confirming record didn't exist yet; see
`nova.skills.add_app`'s module docstring). A write that then fails
verification appends a compensating `skill.add_app.rolled_back` event for
the same `(app, resolved_path)` pair rather than trying to un-record the
grant — the audit log is append-only and hash-chained, so there is no
un-recording, only correcting. `_confirmed_learned_apps()` below walks the
whole log and keeps the LATEST status per pair; a pair is trusted only if
that latest status is `skill.add_app.learned`, not `skill.add_app.
rolled_back`. A ledger that records both a grant and its own reversal, in
order, is honest in a way silently discarding either one would not be.

**F66 (nova-security): reconciliation compares `learned_app_path_key()`
(a sha256 digest), not the literal resolved path.** `AuditLog.record()`
runs every string through `nova.security.redact.redact()` (F14) before
writing, which rewrites the user-home segment of any path under
`C:\\Users\\<name>\\...`. Real installed apps overwhelmingly live there, so
the audit record's own `resolved_path` field is NOT byte-identical to
`learned_apps.json`'s literal one (which must stay literal — it's what
`subprocess.Popen` actually launches) for exactly the common case, not an
edge case: a first verification of F62/F63/F65 against a System32 path
(never redacted, since it isn't under a user's home directory) missed this
entirely, and only surfaced once tested against a path that looks like
what a real `add_app` call actually produces. A sha256 hex digest doesn't
match `redact()`'s home-path, secret, or email patterns, so it survives
unchanged on both sides — see `learned_app_path_key()`'s own docstring for
the verified claim, not an assumed one.

**Measured cost, so "walking the log at every boot" isn't a guess**:
scanning a synthetic 1,000,000-line audit.log (≈284 MB — the volume of
roughly 5-10 years of heavy daily use extrapolated from this project's
real per-entry size) took ~2.4s on this machine. That is material at that
scale and not something to pretend otherwise. It is NOT material at
today's or the next few years' real scale: this project's actual
audit.log is 59 lines as of T2.3a; even 100,000 lines (a very heavy few
years) scans in well under half a second. No bounded/windowed scan is
implemented here — a "most recent N lines" cutoff was considered and
rejected: unlike routine audit growth, `add_app` writes are rare (a human
voice-approving a new app, not a daily event), so a legitimately-approved
entry's confirming record could scroll past any fixed recency window
purely from OTHER, unrelated audit activity accumulating — silently
un-trusting a working app for a reason that has nothing to do with it,
which is a worse failure mode than the boot-time cost it would save. The
honest bounded fix, if this ever becomes real (revisit past roughly
200,000-300,000 lines, per the measurement above), is a write-time pointer
— `add_app` recording the exact audit-log position of its own write
alongside the entry, verified by direct lookup instead of a scan — not a
sliding window. Not built now because nothing at today's scale asks for
it, and building it speculatively would be new, untested infrastructure
for a cost that does not yet exist.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nova import AUDIT_LOG_PATH, DEFAULT_CONFIG_PATH, LEARNED_APPS_PATH
from nova.log import get_logger

logger = get_logger("config")


class ConfigError(ValueError):
    """Raised for a missing, malformed, or unknown-key config/nova.yaml."""


@dataclass(frozen=True)
class Bounds:
    """Declarative numeric range for one config field (T1.5, F7/F27).
    Attached via `dataclasses.field(metadata={"bounds": Bounds(...)})` on a
    `float`/`int` field; `_build_section` reads it and `_check_bounds`
    enforces it after the existing type check.

    `lo`/`hi` are None for "no bound on this side" — most numeric fields
    only need one side constrained, not both. `lo_exclusive`/`hi_exclusive`
    default to inclusive (`>=`/`<=`), matching the rest of this module's
    comparison style; a field where the boundary value itself is degenerate
    (see `WakeConfig.threshold` below) sets the relevant one to True.

    `lo_note`/`hi_note` are an optional extra clause appended to the
    `ConfigError` for that specific side — for a field whose bound is a
    security or product decision rather than a pure sanity check, this is
    where that gets said, at the point someone actually hits it, not only
    in a comment above the field a casual editor may not read.
    """

    lo: float | None = None
    hi: float | None = None
    lo_exclusive: bool = False
    hi_exclusive: bool = False
    lo_note: str | None = None
    hi_note: str | None = None


def _check_bounds(section: str, key: str, value: float, bounds: Bounds) -> None:
    """Reject `value` outside `bounds`, including NaN, by construction.

    Written as a chained comparison (`effective_lo <= value <= effective_hi`,
    inverted) rather than `value < lo or value > hi`: NaN fails *every*
    ordinary comparison, so the chained form evaluates to False for a NaN
    operand on either side (rejected, correctly) while the split-OR form
    would pass a NaN straight through both branches (each individually
    False) and accept it. `float` fields already reject NaN/Inf earlier in
    `_check_value`'s own finite-number check, so this is defense in depth
    for the float case specifically — but this function doesn't assume a
    caller already filtered them, and int fields never even hit that
    earlier check, so the shape has to be correct standing alone.
    """
    effective_lo = bounds.lo if bounds.lo is not None else float("-inf")
    effective_hi = bounds.hi if bounds.hi is not None else float("inf")
    lo_ok = (value > effective_lo) if bounds.lo_exclusive else (value >= effective_lo)
    hi_ok = (value < effective_hi) if bounds.hi_exclusive else (value <= effective_hi)
    if lo_ok and hi_ok:
        return

    if bounds.lo is not None and bounds.hi is not None:
        lo_sym = ">" if bounds.lo_exclusive else ">="
        hi_sym = "<" if bounds.hi_exclusive else "<="
        range_desc = f"{lo_sym} {bounds.lo} and {hi_sym} {bounds.hi}"
    elif bounds.lo is not None:
        range_desc = f"{'>' if bounds.lo_exclusive else '>='} {bounds.lo}"
    elif bounds.hi is not None:
        range_desc = f"{'<' if bounds.hi_exclusive else '<='} {bounds.hi}"
    else:
        return  # a Bounds() with neither side set constrains nothing

    msg = f"nova.yaml: '{section}.{key}' must be {range_desc}, got {value!r}"
    note = (bounds.lo_note if not lo_ok else None) or (bounds.hi_note if not hi_ok else None)
    if note:
        msg += f" -- {note}"
    raise ConfigError(msg)


# ---------------------------------------------------------------------------
# Sections — one per consuming module.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioConfig:
    """Consumed by nova.audio (Capture/Player, probe_input_devices)."""

    samplerate: int = 16000  # Hz — fixed by the STT/VAD models downstream
    channels: int = 1  # mono
    frame_ms: int = 30  # capture frame size handed to Capture.read()


@dataclass(frozen=True)
class SttConfig:
    """Consumed by nova.stt (Transcriber, probe_gpu)."""

    model: str = "large-v3-turbo"  # faster-whisper model name in cache/hf/hub
    language: str = "en"


@dataclass(frozen=True)
class TtsConfig:
    """Consumed by nova.tts (Synthesizer)."""

    #: Set false to make NOVA print replies instead of speaking them.
    #: Deliberate muting, NOT the "never go silently mute" failure case --
    #: nova/app.py's _speak() returns early at INFO and prints, never logging
    #: a warning and never reaching the SAPI fallback, because nothing has
    #: gone wrong. Useful when Manav is working at the same desk NOVA is
    #: talking at.
    speak: bool = True
    voice: str = "af_heart"  # Kokoro voice id
    #: F7: bounded 0.25-4.0, matching the speaking-rate range public TTS APIs
    #: (e.g. Google Cloud TTS's speakingRate) document as their own sane
    #: multiplier range -- not independently tuned against Kokoro's own
    #: intelligibility limits here, borrowed as a reasonable off-the-shelf
    #: sanity bound rather than an unbounded float. 0 and negative are
    #: physically meaningless for a rate multiplier, which is the actual
    #: bug F7 reported (`.inf`/`.nan` also loaded successfully before this;
    #: those are now caught earlier too, by _check_value's own finite-number
    #: check for every float field, not just this one).
    speed: float = field(default=1.0, metadata={"bounds": Bounds(lo=0.25, hi=4.0)})


@dataclass(frozen=True)
class SecurityConfig:
    """Consumed by nova.security (Egress) and nova.skills.open_app."""

    egress_allowlist: list[str] = field(default_factory=list)

    #: closed allowlist for open_app: symbolic name -> absolute executable
    #: path. docs/THREAT_MODEL.md §1.1 — open_app resolves only through this
    #: map, never through a free-text path or a shell.
    allowed_apps: dict[str, str] = field(default_factory=dict)

    #: directories a resolved allowed_apps entry must realpath-land inside at
    #: call time (docs/THREAT_MODEL.md §1.1). Checked with path containment,
    #: not string prefix — a junction pointed out of one of these dirs must
    #: fail the check even though its lexical path starts with the prefix.
    allowed_app_dirs: list[str] = field(default_factory=list)

    #: Manav's own private terms (name, employer, etc.), matched case-
    #: insensitively and redacted by nova.security.redact.redact(). Empty by
    #: default — this is opt-in content he provides, never inferred
    #: (docs/SECURITY_SPEC.md §2.3).
    redact_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HotkeyConfig:
    """Consumed by nova.interfaces.hotkey (Hotkey)."""

    combo: str = "ctrl+alt+space"  # `keyboard` package hotkey string
    tap_threshold_ms: int = 300  # <= this held duration = tap (latch toggle); > this = hold (classic PTT)
    latch_timeout_ms: int = 30000  # LATCHED auto-expires after this long with no closing tap


@dataclass(frozen=True)
class WakeConfig:
    """Consumed by nova.audio.wake (WakeWord)."""

    #: filename resolved against models/wake/ (nova.WAKE_MODEL_DIR) -- swap
    #: in hey_nova.onnx later with zero code changes.
    model_path: str = "hey_jarvis_v0.1.onnx"
    #: The one knob Manav will actually tune -- and, until acoustic arming
    #: is tier-capped (F28), the entire access-control surface for hands-
    #: free arming (docs/THREAT_MODEL_ALWAYS_ON.md §4). Lowering it to catch
    #: a missed detection also widens who/what can arm NOVA; that tradeoff
    #: belongs in this comment, not just in the threat model, because this
    #: is the file Manav actually edits.
    #:
    #: F27 (T1.6, scripts/measure_wake_false_accept.py,
    #: docs/WAKE_FALSE_ACCEPT_MEASUREMENT.md): NOT closed by this value.
    #: 0.75/0.70 are defence in depth, explicitly insufficient -- read the
    #: rest of this comment before treating this constant as "the fix".
    #:
    #: "hey jazz" -- an ordinary two-word English phrase, not an engineered
    #: nonsense syllable -- was measured false-accepting three separate
    #: times, after F36/F40's audio-path bugs were already fixed, and its
    #: observed peak rose every single time anyone sampled more:
    #:
    #:     session 1 (T1.6, first clean run): peak 0.5125
    #:     session 2 (lead's run):    [0.4377, 0.6449, 0.4759]  peak 0.6449
    #:     session 3 (nova-dev-2, same day): [0.4920, 0.4071, 0.9321]  peak 0.9321
    #:     session 3, 6 more trials of "hey jazz" alone:
    #:         [0.4663, 0.4842, 0.7414, 0.9242, 0.8240, 0.4885]  peak 0.9242
    #:
    #: Three sessions, three ceilings, each refuting the last -- the
    #: signature of a right tail this project cannot afford to bound by
    #: sampling harder. A floor derived from an observed maximum will keep
    #: being exceeded by the next run, and will look like a reasonable
    #: number right up until it doesn't. Raising this constant further was
    #: considered and rejected for that reason, not attempted and abandoned.
    #:
    #: A higher floor was also rejected for a second, independent reason
    #: (F41): every true-positive score on this project, including every
    #: number above, is Kokoro-synthetic -- a TTS voice through speakers
    #: into a mic. No human has ever spoken to this detector. A floor
    #: pushed toward the TP's measured 0.996-0.999 to try to clear "hey
    #: jazz" risks a threshold Manav's own real voice can't reliably clear
    #: either, and there is currently no way to measure that risk before he
    #: tries -- raising a threshold aggressively against synthetic
    #: true-positive data is a trade this project cannot currently price.
    #:
    #: So: 0.75/0.70 halve the exposure at the current default (of 6 fresh
    #: "hey jazz" trials, 3 fire at 0.55, 2 still fire at 0.75) without
    #: claiming to close it, and F28 -- capping acoustic-only arming below
    #: ACT tier -- is the control this measurement is actually the strongest
    #: argument for. If wake-armed actions can't reach ACT, a false accept
    #: costs a spurious listening state, not a launched process: a
    #: containable failure, unlike "a number that holds until it doesn't."
    #: Put to Manav in those terms; not this dev's call to make alone.
    #:
    #: Bounds enforced: `>= 0.70` and `<= 1.0`. `<= 1.0` because the
    #: classifier's score is a probability-like value bounded in [0, 1]
    #: (standard for this architecture's sigmoid output head; the true
    #: positive measurements above are consistent with that) -- above 1.0
    #: the gate can never be satisfied, silently disabling hands-free wake
    #: entirely.
    threshold: float = field(default=0.75, metadata={"bounds": Bounds(
        lo=0.70, hi=1.0,
        lo_note=(
            "this is the access-control gate for hands-free arming, not a comfort "
            "setting -- 0.70 is defence in depth (F27, T1.6), explicitly NOT a "
            "closed floor: \"hey jazz\" has been measured false-accepting up to "
            "0.9321 across repeat trials, and the observed ceiling has risen every "
            "time anyone sampled more. Lowering below 0.70 widens an already-"
            "measured exposure further; raising it is not a fix either -- see "
            "docs/WAKE_FALSE_ACCEPT_MEASUREMENT.md and F28/F41."
        ),
        hi_note="the detector's score is bounded in [0, 1]; above that it can never trigger, silently disabling hands-free wake",
    )})
    preroll_ms: int = 1000  # audio kept before detection so the first syllables of the command aren't lost
    arm_timeout_ms: int = 15000  # hard ceiling if VAD never detects end-of-speech
    vad_silence_ms: int = 800  # consecutive trailing silence (VAD) that ends an armed turn once speech has started
    #: How NOVA decides speech is meant for it (Manav, 2026-10-14):
    #:   "prefix" -- NOVA listens only while its window is open, transcribes
    #:               each utterance locally, and acts ONLY when the sentence
    #:               starts with one of `names` ("Nova, open Spotify").
    #:               Everything else is discarded unread and never logged.
    #:   "model"  -- the original always-on wake-word detector (model_path).
    mode: str = "model"  # config/nova.yaml sets "prefix" -- the shipped behaviour
    #: Names NOVA answers to in prefix mode, matched case-insensitively at the
    #: start of a sentence, optionally after "hey"/"hi"/"ok"/"okay".
    names: list[str] = field(default_factory=lambda: ["nova"])
    #: After a bare "Nova" (nothing else said), how long NOVA keeps listening
    #: for the command without needing its name again.
    follow_up_ms: int = field(default=8000, metadata={"bounds": Bounds(lo=0, hi=60000)})
    #: Longest single utterance NOVA will buffer in prefix mode.
    max_utterance_ms: int = field(default=15000, metadata={"bounds": Bounds(lo=1000, hi=60000)})
    #: Speech shorter than this is treated as noise (coughs, clicks) and dropped.
    min_speech_ms: int = field(default=300, metadata={"bounds": Bounds(lo=0, hi=5000)})
    #: Calibration aid. When true, for speech NOT addressed to NOVA the log
    #: records only its FIRST WORD (e.g. 'noah'), so a mis-heard name can be
    #: diagnosed. Off by default: normally nothing about such speech is kept.
    calibrate: bool = False


@dataclass(frozen=True)
class UiConfig:
    """Consumed by nova.app / nova.interfaces.window (Manav, 2026-10-14)."""

    #: True: NOVA is a normal windowed app -- opening it shows the NOVA
    #: window and starts listening; closing the window quits NOVA and turns
    #: the microphone off. False: the older background tray-icon mode.
    window: bool = False  # config/nova.yaml sets true -- the shipped behaviour
    #: Speak a short "ready" line once the window has finished loading, so
    #: it is obvious at a glance (and by ear) that voice output works.
    greet: bool = False


@dataclass(frozen=True)
class DuplexConfig:
    """Consumed by nova.audio.duplex.DuplexGate (T1.7, finding F26)."""

    #: How long after NOVA stops speaking the wake-word detector stays
    #: muted, on top of the mute-while-speaking itself. Inherited from the
    #: retired build's 180ms (speaker decay + WASAPI output buffering
    #: reasoning), rounded up slightly for margin -- NOT independently
    #: measured on this machine's speakers/room. An untuned starting point,
    #: labelled as one; see nova/audio/duplex.py's module docstring.
    #: Bounded `>= 0` only (a negative hangover is nonsensical, not a
    #: security concern like WakeConfig.threshold's floor -- no upper bound
    #: needed for the same reason none of the other timing knobs in this
    #: file have one).
    hangover_ms: int = field(default=500, metadata={"bounds": Bounds(lo=0)})


@dataclass(frozen=True)
class BrainConfig:
    """Consumed by nova.brain.router.Router. See its module docstring for how
    these two numbers were chosen (calibrated against a labelled phrase set,
    not picked decoratively)."""

    #: below this score, route() asks for clarification instead of acting.
    confidence_floor: float = 0.42

    #: if the top-2 skill scores are closer than this, treat the match as
    #: cross-domain-ambiguous and clarify even if the top score clears the
    #: floor.
    ambiguity_margin: float = 0.08

    # --- the agent brain (nova.brain.agent, 2026-10-15) -------------------
    #: "max": every addressed sentence goes to Claude through the signed-in
    #: `claude` program on Manav's Max plan (nova.brain.max_brain) -- no API
    #: key. "api": same, through the Anthropic API with a key
    #: (nova.brain.agent). "router": the old deterministic router first,
    #: Claude only when it misses. In every mode the router is the fallback.
    mode: str = "max"
    #: Claude model (a full id, or "haiku"/"sonnet" for the max mode).
    #: Haiku is the fastest current model.
    model: str = "claude-haiku-5-5"
    max_tokens: int = field(default=600, metadata={"bounds": Bounds(lo=64, hi=4096)})
    #: tool-use rounds per sentence ("open YouTube in Brave and play lofi" = 2-3)
    max_steps: int = field(default=6, metadata={"bounds": Bounds(lo=1, hi=12)})
    #: conversation memory: last N exchanges, forgotten after this much silence
    history_turns: int = field(default=8, metadata={"bounds": Bounds(lo=0, hi=40)})
    history_idle_s: float = field(default=600.0, metadata={"bounds": Bounds(lo=0.0, hi=86400.0)})
    #: let Claude search the web for questions it can't answer from memory
    #: ($10 per 1,000 searches on top of tokens; results are untrusted input)
    web_search: bool = True
    request_timeout_s: float = field(default=20.0, metadata={"bounds": Bounds(lo=2.0, hi=120.0)})
    #: NOVA's journal (nova/memory): append each answered, addressed sentence
    #: to data/memory/journal/<day>.md. Never wake-armed turns.
    journal: bool = True
    #: Two-speed brain (Manav, 2026-10-16): a short sentence that exactly
    #: matches one of these skills' command patterns runs instantly, without
    #: a round trip to Claude. Only skills whose commands can't be misheard
    #: into something else belong here -- nothing that takes a free-text
    #: name (apps, sites, searches): those go to Claude, which also catches
    #: mishearings. Empty list = everything goes to Claude.
    instant_skills: list[str] = field(default_factory=lambda: [
        "time", "date", "battery",
        "mute", "unmute", "volume_up", "volume_down", "set_volume", "get_volume",
        "brightness_up", "brightness_down", "set_brightness", "get_brightness",
        "media_play_pause", "media_next", "media_previous", "media_stop",
        "screenshot", "lock_computer", "show_desktop", "minimize_window", "maximize_window",
        "cancel_shutdown", "set_radio",
    ])


@dataclass(frozen=True)
class GovernorConfig:
    """Consumed by nova.governor.Governor. Defaults are measured, not
    modelled — see docs/GOVERNOR_MEASUREMENTS.md for the real numbers and
    the command that produced each one."""

    #: how often the background poll checks RSS against the ceiling.
    poll_interval_s: float = 5.0

    #: above this, with no turn in flight, unload the largest registered
    #: idle component (re-measuring and continuing until back under the
    #: ceiling or nothing left to unload). 900 MB sits just above the
    #: measured Transcriber-alone-after-real-use floor (~740-780 MB host
    #: RSS on this machine -- Transcriber.unload() frees VRAM but not host
    #: RSS once it has actually transcribed something, a real finding, not
    #: a bug -- see docs/GOVERNOR_MEASUREMENTS.md) and comfortably below
    #: "both resident" (~1150-1350 MB), so the realistic job this ceiling
    #: does is shedding Kokoro when both are loaded and idle, not returning
    #: all the way to cold-idle once Whisper has ever run.
    rss_ceiling_mb: float = 900.0


@dataclass(frozen=True)
class WeatherConfig:
    """Consumed by nova.skills.weather (2026-10-15). The default place a
    bare "what's the weather" is about -- Manav's home, not a guess from
    his IP. Fetched from Open-Meteo through nova.security.egress (host
    api.open-meteo.com must be on security.egress_allowlist)."""

    location_name: str = "Tempe"  # spoken in replies: "... in Tempe"
    latitude: float = field(default=33.4255, metadata={"bounds": Bounds(lo=-90.0, hi=90.0)})
    longitude: float = field(default=-111.9400, metadata={"bounds": Bounds(lo=-180.0, hi=180.0)})
    timezone: str = "America/Phoenix"  # IANA name; decides what "today"/"tomorrow" mean
    #: "fahrenheit" or "celsius" (checked by the weather skill, which says so
    #: out loud rather than silently picking one).
    temperature_unit: str = "fahrenheit"


@dataclass(frozen=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    duplex: DuplexConfig = field(default_factory=DuplexConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    governor: GovernorConfig = field(default_factory=GovernorConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)


_SECTIONS: dict[str, type] = {
    "audio": AudioConfig,
    "stt": SttConfig,
    "tts": TtsConfig,
    "security": SecurityConfig,
    "hotkey": HotkeyConfig,
    "wake": WakeConfig,
    "duplex": DuplexConfig,
    "brain": BrainConfig,
    "governor": GovernorConfig,
    "ui": UiConfig,
    "weather": WeatherConfig,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check_value(section: str, key: str, value: Any, expected: Any, *, bounds: "Bounds | None" = None) -> Any:
    origin = typing.get_origin(expected)
    if origin is list:
        (elem_type,) = typing.get_args(expected)
        if isinstance(value, bool) or not isinstance(value, list) or not all(
            isinstance(item, elem_type) and not isinstance(item, bool)
            for item in value
        ):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be a list of {elem_type.__name__}, "
                f"got {value!r}"
            )
        return list(value)

    if origin is dict:
        key_type, val_type = typing.get_args(expected)
        if isinstance(value, bool) or not isinstance(value, dict) or not all(
            isinstance(k, key_type) and isinstance(v, val_type) and not isinstance(v, bool)
            for k, v in value.items()
        ):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be a mapping of "
                f"{key_type.__name__} to {val_type.__name__}, got {value!r}"
            )
        return dict(value)

    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be a float, got "
                f"{type(value).__name__} ({value!r})"
            )
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be a finite number, got {value!r}"
            )
        if bounds is not None:
            _check_bounds(section, key, result, bounds)
        return result

    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be an int, got "
                f"{type(value).__name__} ({value!r})"
            )
        if bounds is not None:
            _check_bounds(section, key, value, bounds)
        return value

    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be a str, got "
                f"{type(value).__name__} ({value!r})"
            )
        return value

    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be a bool, got "
                f"{type(value).__name__} ({value!r})"
            )
        return value

    raise ConfigError(f"nova.yaml: unsupported field type {expected!r} for '{section}.{key}'")


def _build_section(section_name: str, section_type: type, raw: Any) -> Any:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"nova.yaml: section '{section_name}' must be a mapping, got "
            f"{type(raw).__name__}"
        )

    hints = typing.get_type_hints(section_type)
    fields_by_name = {f.name: f for f in dataclasses.fields(section_type)}
    unknown = set(raw) - set(fields_by_name)
    if unknown:
        raise ConfigError(
            f"nova.yaml: unknown key(s) in '{section_name}': {sorted(unknown)}. "
            f"Expected one of: {sorted(fields_by_name)}"
        )

    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        bounds = fields_by_name[key].metadata.get("bounds")
        kwargs[key] = _check_value(section_name, key, value, hints[key], bounds=bounds)
    return section_type(**kwargs)


def _load_learned_apps(path: Path) -> dict[str, str]:
    """Read T2.3a's machine-written allowlist additions. A missing file is
    normal (nothing learned yet on this machine) -> {}. A PRESENT but
    malformed file is a loud ConfigError, same treatment as nova.yaml
    itself -- this file is written only by nova.skills.add_app's own
    atomic writer, so a shape this function doesn't recognize means
    corruption or tampering, not an ordinary missing-file case, and a
    security-relevant allowlist silently reverting to empty because its
    file got corrupted is exactly the kind of quiet failure this module
    exists to refuse.
    """
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: could not read learned apps: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {"allowed_apps"}:
        raise ConfigError(
            f"{path}: must be a mapping with only an 'allowed_apps' key, got {raw!r}"
        )
    apps = raw.get("allowed_apps", {})
    if not isinstance(apps, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in apps.items()
    ):
        raise ConfigError(f"{path}: 'allowed_apps' must be a mapping of str to str")
    return dict(apps)


_LEARNED_EVENT_KINDS = {"skill.add_app.learned", "skill.add_app.rolled_back"}


def learned_app_path_key(resolved_path: str) -> str:
    """F66 (nova-security): the machine-comparable reconciliation key for a
    resolved app path — NOT the literal path.

    `AuditLog.record()` runs every string value through `nova.security.
    redact.redact()` (F14) before writing, which rewrites the user-home
    segment of any path shaped like `C:\\Users\\<name>\\...` (or the POSIX
    equivalent) to `[USER]`. Real installed apps overwhelmingly live there
    — Discord, VS Code, Spotify; `nova.skills.add_app`'s own module
    docstring already found this true for most of what a human would
    actually ask to add. So comparing `learned_apps.json`'s literal,
    unredacted path (it has to stay literal — it's what `subprocess.Popen`
    launches) against the REDACTED string that landed in the audit record
    would silently fail reconciliation for exactly that common case. A
    verification built only against a System32 path (outside the home
    directory, never redacted) would not catch this — the failure needs a
    path under the user's profile to appear at all.

    A sha256 hex digest of the literal path sidesteps this rather than
    fighting it: a hex digest doesn't match `redact()`'s home-path,
    secret, or email patterns, so `redact(digest) == digest` always —
    verified directly, not assumed. Both sides compute this same digest
    from the same literal string (`nova.skills.add_app` when it writes the
    audit event, this module when it reconciles), so they agree regardless
    of what redaction did to the path text sitting next to it in the log.
    The audit record still carries the (redacted) `resolved_path` in plain
    form for a human reading the log — this key exists purely for the
    machine comparison, never in place of the readable field.
    """
    return hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()


def _confirmed_learned_apps(audit_path: Path) -> frozenset[tuple[str, str]]:
    """F62/F63/F65/F66: the set of (name, path_key) pairs — `path_key`
    being `learned_app_path_key()`'s hash, not the literal path (F66) —
    whose MOST RECENT matching audit event is 'skill.add_app.learned', not
    merely "has one somewhere". See this module's own docstring for why
    this is a plain content scan, not a hash-chain replay, and what that
    ceiling honestly buys.

    F65: a grant can be reversed. `nova.skills.add_app` records the grant
    BEFORE its own round-trip verification (it has to — see that module's
    docstring for why verifying first deadlocked every call), so a write
    that fails verification after the grant was already, durably recorded
    appends a compensating 'skill.add_app.rolled_back' event for the same
    pair rather than trying to un-record it (the audit log is append-only
    and hash-chained; there is no un-recording). This function has to
    honor that: it walks the WHOLE log in order and keeps only the LATEST
    status per (name, path_key) pair, so a 'rolled_back' event correctly
    un-trusts a pair its own earlier 'learned' event granted, and a later,
    fresh 'learned' event (a legitimate re-add) correctly re-trusts a pair
    an older 'rolled_back' had revoked. Two different kinds sharing one
    status map, last-write-wins by file position — an append-only log's
    position IS its timestamp.

    A missing audit.log means "nothing confirmed yet" (empty set), not an
    error — a fresh machine with no audit history and no learned apps yet
    is the ordinary starting state. A line that isn't valid JSON is
    audit.log's OWN integrity problem (AuditLog.verify() is what diagnoses
    that); silently skipped here rather than raised, so one corrupt line
    can't turn "nothing confirmed" into "config won't load at all".
    """
    if not audit_path.exists():
        return frozenset()
    status: dict[tuple[str, str], str] = {}
    with audit_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = entry.get("event") if isinstance(entry, dict) else None
            if not isinstance(event, dict) or event.get("kind") not in _LEARNED_EVENT_KINDS:
                continue
            name, path_key = event.get("app"), event.get("resolved_path_key")
            if isinstance(name, str) and isinstance(path_key, str):
                status[(name, path_key)] = event["kind"]  # later line overwrites earlier -- file order is chronological
    return frozenset(pair for pair, kind in status.items() if kind == "skill.add_app.learned")


def orphaned_app_names(
    *, learned_apps_path: Path | None = None, audit_log_path: Path | None = None
) -> frozenset[str]:
    """The names in the learned-apps file with NO matching audit record —
    exactly the entries `load_config()` just refused to trust (F62/F63).

    Exposed standalone rather than through `Config`/`SecurityConfig` on
    purpose: `_build_section()` treats every declared dataclass field as an
    ALLOWED nova.yaml key (unknown keys are a hard error), so a purely-
    computed field would look like a human-settable knob it structurally
    isn't. A skill wanting a more informative refusal than "I don't know
    how to open X" for a name it recognizes but doesn't trust
    (nova.skills.open_app, F62's UX follow-up) calls this directly
    instead — it does its own fresh read of both files, deliberately not
    cached, since it only runs on an already-slow-path refusal, never on
    every boot.
    """
    learned_path = Path(learned_apps_path) if learned_apps_path is not None else LEARNED_APPS_PATH
    audit_path = Path(audit_log_path) if audit_log_path is not None else AUDIT_LOG_PATH
    try:
        learned = _load_learned_apps(learned_path)
    except ConfigError:
        # A malformed learned_apps.json is load_config()'s own error to
        # raise loudly on the next boot -- this helper only ever runs
        # mid-session, on a refusal path, and must not turn "give a nicer
        # message" into a second way to crash a turn.
        return frozenset()
    if not learned:
        return frozenset()
    confirmed = _confirmed_learned_apps(audit_path)
    return frozenset(
        name for name, resolved_path in learned.items()
        if (name, learned_app_path_key(resolved_path)) not in confirmed
    )


def load_config(
    path: Path | None = None,
    *,
    learned_apps_path: Path | None = None,
    audit_log_path: Path | None = None,
) -> Config:
    """Parse, validate, and return config/nova.yaml as a typed Config tree,
    merged with `nova.LEARNED_APPS_PATH`'s machine-written entries (T2.3a;
    see this module's own docstring for the merge rule and why it's a
    separate file) and reconciled against `nova.AUDIT_LOG_PATH` (F62/F63;
    see this module's own docstring for why and its measured cost).
    `learned_apps_path`/`audit_log_path` override where those two files are
    read from — for tests only; every real caller gets the defaults.

    Raises ConfigError if nova.yaml or the learned-apps file is malformed,
    contains an unknown key at any level, or a value of the wrong type. A
    missing learned-apps file or audit log is normal and not an error; a
    missing nova.yaml still is.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"nova.yaml: could not parse {config_path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"nova.yaml: top level must be a mapping, got {type(raw).__name__}"
        )

    unknown = set(raw) - set(_SECTIONS)
    if unknown:
        raise ConfigError(
            f"nova.yaml: unknown top-level key(s): {sorted(unknown)}. "
            f"Expected one of: {sorted(_SECTIONS)}"
        )

    sections = {
        name: _build_section(name, section_type, raw.get(name))
        for name, section_type in _SECTIONS.items()
    }

    # T2.3a: merge machine-learned allowlist entries UNDER nova.yaml's own —
    # a name nova.yaml already configures always wins; the learned file can
    # only ADD names nova.yaml doesn't mention, never override or remove
    # one. allowed_app_dirs (the actual trust boundary) is never touched by
    # this merge — the learned file has no key for it at all, enforced by
    # _load_learned_apps' own "only an allowed_apps key" check above.
    learned_path = Path(learned_apps_path) if learned_apps_path is not None else LEARNED_APPS_PATH
    learned = _load_learned_apps(learned_path)
    if learned:
        # F62/F63: reconcile before merging — an entry with no matching
        # audit record was never approved through add_app's CONFIRM path,
        # whatever put it there, and is dropped rather than trusted by
        # default (see this module's own docstring for the full reasoning
        # and the measured cost of the scan this performs).
        audit_path = Path(audit_log_path) if audit_log_path is not None else AUDIT_LOG_PATH
        confirmed = _confirmed_learned_apps(audit_path)
        reconciled: dict[str, str] = {}
        for name, resolved_path in learned.items():
            # F66: compare by learned_app_path_key(), not the literal path
            # — the audit record's own resolved_path may have been
            # redacted (F14) if it sits under the user's home directory,
            # which most real installed apps do.
            if (name, learned_app_path_key(resolved_path)) in confirmed:
                reconciled[name] = resolved_path
            else:
                logger.warning(
                    "%s: '%s' -> %r has no matching 'skill.add_app.learned' audit record in %s — "
                    "refusing to trust it (F63: this bypasses Start Menu resolution, the CONFIRM "
                    "prompt, and the audit trail in one step; not merged into allowed_apps)",
                    learned_path, name, resolved_path, audit_path,
                )
        if reconciled:
            merged_apps = {**reconciled, **sections["security"].allowed_apps}
            sections["security"] = dataclasses.replace(sections["security"], allowed_apps=merged_apps)

    return Config(**sections)
