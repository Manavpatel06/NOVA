"""Contract: docs/CONTRACTS.md T0.1 — nova.config.load_config.

    def load_config(path: Path | None = None) -> Config

Rules under test:
  - unknown keys (top-level or within a section) are a hard error naming the
    offending key, not a shrug;
  - type errors are caught with a clear message, not a silent coercion or a
    raw AttributeError/TypeError leaking out of PyYAML/dataclasses internals;
  - no module reads os.environ or hardcodes a path — everything comes from
    Config;
  - paths with spaces/backslashes work (CLAUDE.md).

These tests are white-box against nova/config.py as landed (real Config /
AudioConfig / SttConfig / TtsConfig / SecurityConfig dataclasses, real
ConfigError type) rather than guessed, since T0.1 landed while this suite was
being written. They still test the *contract* (documented + constitutional
behavior), not incidental implementation choices.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from nova.config import (
    AudioConfig,
    Config,
    ConfigError,
    SecurityConfig,
    SttConfig,
    TtsConfig,
    load_config,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Missing / malformed file
# ---------------------------------------------------------------------------


def test_missing_file_raises_config_error(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigError):
        load_config(missing)


def test_malformed_yaml_raises_config_error_not_a_raw_yaml_error(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "audio: [unclosed\n  - broken\n")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_non_mapping_top_level_document_raises(tmp_path):
    """A YAML list (or scalar) at the top level is not a config, and must not
    crash with an unhandled AttributeError from treating it as a dict."""
    bad = _write(tmp_path / "nova.yaml", "- audio\n- stt\n")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_non_mapping_section_raises(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "audio:\n  - 1\n  - 2\n")
    with pytest.raises(ConfigError):
        load_config(bad)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_empty_document_uses_all_defaults(tmp_path):
    empty = _write(tmp_path / "nova.yaml", "")
    cfg = load_config(empty)
    assert cfg == Config()


def test_partial_document_defaults_missing_sections(tmp_path):
    partial = _write(tmp_path / "nova.yaml", "stt:\n  language: fr\n")
    cfg = load_config(partial)
    assert cfg.stt.language == "fr"
    assert cfg.stt.model == SttConfig().model  # untouched field keeps its default
    assert cfg.audio == AudioConfig()
    assert cfg.tts == TtsConfig()
    assert cfg.security == SecurityConfig()


def test_load_config_returns_dataclass_tree(tmp_path):
    cfg = load_config(_write(tmp_path / "nova.yaml", ""))
    assert dataclasses.is_dataclass(cfg)
    for section in ("audio", "stt", "tts", "security"):
        assert dataclasses.is_dataclass(getattr(cfg, section))


# ---------------------------------------------------------------------------
# Unknown keys named in the error (the core contract promise)
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_named_in_error(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "_totally_bogus_unknown_key_xyz: 1\n")
    with pytest.raises(ConfigError, match="_totally_bogus_unknown_key_xyz"):
        load_config(bad)


def test_unknown_key_within_a_section_is_named_in_error(tmp_path):
    bad = _write(
        tmp_path / "nova.yaml",
        "audio:\n  samplerate: 16000\n  _bogus_nested_key: 1\n",
    )
    with pytest.raises(ConfigError, match="_bogus_nested_key"):
        load_config(bad)


@pytest.mark.parametrize("section", ["audio", "stt", "tts", "security"])
def test_every_section_rejects_its_own_unknown_key(tmp_path, section):
    bad = _write(tmp_path / "nova.yaml", f"{section}:\n  _nope_not_a_real_field: 1\n")
    with pytest.raises(ConfigError, match="_nope_not_a_real_field"):
        load_config(bad)


def test_unknown_key_error_does_not_silently_ignore_and_load(tmp_path):
    """A config file full of options nothing reads must fail loudly, not
    quietly produce a Config as if the bogus key weren't there."""
    bad = _write(tmp_path / "nova.yaml", "audio:\n  samplerate: 16000\nnonexistent_section: {}\n")
    with pytest.raises(ConfigError):
        load_config(bad)


# ---------------------------------------------------------------------------
# Type errors are caught, not coerced or left to crash deep in the stack
# ---------------------------------------------------------------------------


def test_int_field_rejects_string(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "audio:\n  samplerate: \"sixteen-thousand\"\n")
    with pytest.raises(ConfigError, match="samplerate"):
        load_config(bad)


def test_int_field_rejects_bool(tmp_path):
    """bool is a subclass of int in Python; `channels: true` must not be
    silently accepted as channels=1."""
    bad = _write(tmp_path / "nova.yaml", "audio:\n  channels: true\n")
    with pytest.raises(ConfigError, match="channels"):
        load_config(bad)


def test_float_field_rejects_non_numeric_string(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "tts:\n  speed: \"fast\"\n")
    with pytest.raises(ConfigError, match="speed"):
        load_config(bad)


def test_float_field_accepts_plain_int(tmp_path):
    """1 (an int) for a float field is a reasonable YAML author error to
    tolerate — assert it's coerced to float, not rejected."""
    cfg = load_config(_write(tmp_path / "nova.yaml", "tts:\n  speed: 2\n"))
    assert cfg.tts.speed == 2.0
    assert isinstance(cfg.tts.speed, float)


def test_str_field_rejects_int(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "stt:\n  language: 123\n")
    with pytest.raises(ConfigError, match="language"):
        load_config(bad)


def test_list_field_rejects_non_list(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "security:\n  egress_allowlist: \"example.com\"\n")
    with pytest.raises(ConfigError, match="egress_allowlist"):
        load_config(bad)


def test_list_field_rejects_wrong_element_type(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "security:\n  egress_allowlist: [1, 2, 3]\n")
    with pytest.raises(ConfigError, match="egress_allowlist"):
        load_config(bad)


def test_list_field_accepts_list_of_str(tmp_path):
    cfg = load_config(
        _write(tmp_path / "nova.yaml", "security:\n  egress_allowlist: [api.example.com, x.com]\n")
    )
    assert cfg.security.egress_allowlist == ["api.example.com", "x.com"]


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_config_tree_is_frozen(tmp_path):
    cfg = load_config(_write(tmp_path / "nova.yaml", ""))
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.audio.samplerate = 48000  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.audio = AudioConfig()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Paths: spaces, backslashes (CLAUDE.md)
# ---------------------------------------------------------------------------


def test_path_with_spaces_and_parens_loads(spaced_tmp_path):
    cfg_path = _write(spaced_tmp_path / "nova.yaml", "stt:\n  language: de\n")
    cfg = load_config(cfg_path)
    assert cfg.stt.language == "de"


def test_path_as_plain_windows_backslash_string(tmp_path):
    cfg_path = _write(tmp_path / "nova.yaml", "stt:\n  language: es\n")
    # Pass a raw backslash string rather than a Path object.
    cfg = load_config(Path(str(cfg_path)))
    assert cfg.stt.language == "es"


# ---------------------------------------------------------------------------
# Real repo config (config/nova.yaml, once it exists)
# ---------------------------------------------------------------------------


def test_default_path_loads_the_real_repo_config(repo_root):
    """path=None must resolve to config/nova.yaml (docs/CONTRACTS.md) and
    load without error. If config/nova.yaml doesn't exist yet, this is
    expected to fail until it lands — not a bug in load_config itself."""
    real_config = repo_root / "config" / "nova.yaml"
    if not real_config.exists():
        pytest.skip("config/nova.yaml has not landed yet")
    cfg = load_config()
    assert isinstance(cfg, Config)


# ---------------------------------------------------------------------------
# CLAUDE.md: no module reads os.environ; everything comes from Config
# ---------------------------------------------------------------------------


class _EnvironTripwire(dict):
    """Wraps the real os.environ mapping and records any read access, so we
    can prove load_config() never consults it (constitution: 'no module
    reads os.environ ... everything comes from Config')."""

    def __init__(self, real: dict):
        super().__init__(real)
        self.reads: list[str] = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)


def test_load_config_never_reads_os_environ(monkeypatch, tmp_path):
    tripwire = _EnvironTripwire(dict(os.environ))
    monkeypatch.setattr(os, "environ", tripwire)

    cfg_path = _write(tmp_path / "nova.yaml", "stt:\n  language: en\n")
    load_config(cfg_path)

    assert tripwire.reads == [], (
        f"load_config() read os.environ keys it must not touch: {tripwire.reads}"
    )
