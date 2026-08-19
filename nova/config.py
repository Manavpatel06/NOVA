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
    #: Bounds enforced (F27, T1.5): `> 0.0` and `<= 1.0`. `<= 1.0` because
    #: the classifier's score is a probability-like value bounded in [0, 1]
    #: (standard for this architecture's sigmoid output head; the one
    #: measured true-positive, 0.997, is consistent with that but this
    #: wasn't independently re-derived here) -- above 1.0 the gate can never
    #: be satisfied, silently disabling hands-free wake entirely. `> 0.0`
    #: because at or below zero the gate is trivially always-open: every
    #: non-negative score satisfies `score >= threshold`, which is not a
    #: "low but valid" setting, it's no gate at all.
    #:
    #: Deliberately NOT setting a real security floor above that. The
    #: measured false-accept numbers (0.997 true-positive, 0.00005 on one
    #: control phrase, zero detections across ~15s of noise) describe
    #: behavior *at the current default*, not the shape of the false-accept
    #: curve between 0.00005 and 0.55 -- there is no hard-negative/near-miss
    #: data (phonetically similar phrases, other wake words, other voices)
    #: anywhere in that huge unmeasured range. nova-security's own threat
    #: model reached the same conclusion and declined to invent a number for
    #: the same reason (docs/THREAT_MODEL_ALWAYS_ON.md §4: "not something to
    #: invent in a threat model... a measurement task for whoever owns the
    #: false-accept curve"). Picking any specific value in that gap would be
    #: false precision dressed as derived-from-data. Tracked as F27, open,
    #: pending a real false-accept measurement across hard negatives.
    threshold: float = field(default=0.55, metadata={"bounds": Bounds(
        lo=0.0, hi=1.0, lo_exclusive=True,
        lo_note=(
            "this is the access-control gate for hands-free arming, not a comfort "
            "setting -- a value at or below zero cannot reject anything, so every "
            "sound would satisfy it (no higher floor is enforced yet: see F27)"
        ),
        hi_note="the detector's score is bounded in [0, 1]; above that it can never trigger, silently disabling hands-free wake",
    )})
    preroll_ms: int = 1000  # audio kept before detection so the first syllables of the command aren't lost
    arm_timeout_ms: int = 15000  # hard ceiling if VAD never detects end-of-speech
    vad_silence_ms: int = 800  # consecutive trailing silence (VAD) that ends an armed turn once speech has started


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
    brain: BrainConfig = field(default_factory=BrainConfig)
    governor: GovernorConfig = field(default_factory=GovernorConfig)


_SECTIONS: dict[str, type] = {
    "audio": AudioConfig,
    "stt": SttConfig,
    "tts": TtsConfig,
    "security": SecurityConfig,
    "hotkey": HotkeyConfig,
    "wake": WakeConfig,
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
