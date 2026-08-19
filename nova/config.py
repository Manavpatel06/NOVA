"""Load and validate config/nova.yaml — the one config file (docs/CONTRACTS.md).

Design rules this module enforces:
  - Unknown keys anywhere in the file are a hard error naming the offending
    key and what was expected, not a silent ignore.
  - Every field below has a real consumer in another M0 module; do not add a
    key here unless you can point at the code that reads it.
  - No module reads os.environ or hardcodes a path — this is the only place
    nova.yaml is parsed, and nova/__init__.py is the only place paths live.
"""

from __future__ import annotations

import dataclasses
import math
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nova import DEFAULT_CONFIG_PATH


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
    hangover_ms: int = field(default=200, metadata={"bounds": Bounds(lo=0)})


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


def load_config(path: Path | None = None) -> Config:
    """Parse, validate, and return config/nova.yaml as a typed Config tree.

    Raises ConfigError if the file is missing, malformed, contains an
    unknown key at any level, or a value of the wrong type.
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
    return Config(**sections)
