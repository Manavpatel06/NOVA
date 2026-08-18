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
    speed: float = 1.0


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
    threshold: float = 0.55  # the one knob Manav will actually tune
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


def _check_value(section: str, key: str, value: Any, expected: Any) -> Any:
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
        return result

    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                f"nova.yaml: '{section}.{key}' must be an int, got "
                f"{type(value).__name__} ({value!r})"
            )
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
    known = {f.name for f in dataclasses.fields(section_type)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            f"nova.yaml: unknown key(s) in '{section_name}': {sorted(unknown)}. "
            f"Expected one of: {sorted(known)}"
        )

    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        kwargs[key] = _check_value(section_name, key, value, hints[key])
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
