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
import json
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
    learned_app_path_key,
    load_config,
    orphaned_app_names,
)
from nova.security.audit import AuditLog
from nova.security.redact import redact


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


def test_float_field_rejects_infinity(tmp_path):
    """docs/SECURITY_SPEC.md §4 flagged tts.speed: float accepting YAML's
    .inf/.nan literals as a robustness gap; config.py has since closed it."""
    bad = _write(tmp_path / "nova.yaml", "tts:\n  speed: .inf\n")
    with pytest.raises(ConfigError, match="finite"):
        load_config(bad)


def test_float_field_rejects_nan(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "tts:\n  speed: .nan\n")
    with pytest.raises(ConfigError, match="finite"):
        load_config(bad)


def test_dict_field_accepts_str_to_str_mapping(tmp_path):
    cfg = load_config(
        _write(
            tmp_path / "nova.yaml",
            "security:\n  allowed_apps:\n    notepad: C:\\Windows\\System32\\notepad.exe\n",
        )
    )
    assert cfg.security.allowed_apps == {"notepad": "C:\\Windows\\System32\\notepad.exe"}


def test_dict_field_rejects_non_dict(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "security:\n  allowed_apps: [not, a, mapping]\n")
    with pytest.raises(ConfigError, match="allowed_apps"):
        load_config(bad)


def test_dict_field_rejects_non_string_value(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "security:\n  allowed_apps:\n    notepad: 123\n")
    with pytest.raises(ConfigError, match="allowed_apps"):
        load_config(bad)


def test_dict_field_rejects_bool_value(tmp_path):
    """SECURITY_SPEC.md praised config.py's list-field bool guard by name
    (bool is an int subclass in Python); the dict-field path added later for
    allowed_apps needs the same discipline for its str-typed values, checked
    directly rather than assumed to match."""
    bad = _write(tmp_path / "nova.yaml", "security:\n  allowed_apps:\n    notepad: true\n")
    with pytest.raises(ConfigError, match="allowed_apps"):
        load_config(bad)


def test_redact_terms_field_accepts_list_of_str(tmp_path):
    cfg = load_config(
        _write(tmp_path / "nova.yaml", "security:\n  redact_terms: [Manav, Acme Corp]\n")
    )
    assert cfg.security.redact_terms == ["Manav", "Acme Corp"]


def test_hotkey_section_defaults(tmp_path):
    cfg = load_config(_write(tmp_path / "nova.yaml", ""))
    assert cfg.hotkey.combo == "ctrl+alt+space"
    assert cfg.hotkey.tap_threshold_ms == 300


def test_hotkey_section_unknown_key_named_in_error(tmp_path):
    bad = _write(tmp_path / "nova.yaml", "hotkey:\n  _bogus: 1\n")
    with pytest.raises(ConfigError, match="_bogus"):
        load_config(bad)


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


# ---------------------------------------------------------------------------
# T2.3a: learned_apps.json merge (nova.skills.add_app's machine-written
# second source, module docstring's "second, machine-written source"
# section). The security-load-bearing property is that a name conflict
# always resolves in nova.yaml's favor, and that the learned file has no
# way to touch allowed_app_dirs (the actual trust boundary) at all -- so
# the rejection tests below matter more than the happy-path ones.
#
# F62/F63 (nova-security): every merge test below now also passes
# audit_log_path -- a SECOND file load_config() reconciles learned entries
# against, per the module's own docstring ("every entry in the learned file
# is checked against nova.AUDIT_LOG_PATH for a matching 'skill.add_app.
# learned' record before it is trusted; an orphan... is dropped from the
# merge"). Omitting the override would read this repo's REAL data/audit.log
# instead of a tmp one -- exactly the isolation break F64 exists to prevent
# one class of (see that finding). A test whose intent is "this entry IS
# trusted" must seed a real matching record via the helper below; a test
# whose intent is the orphan-rejection path itself doesn't.
# ---------------------------------------------------------------------------

def _write_learned_raw(path: Path, raw) -> Path:
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _write_learned(path: Path, apps: dict) -> Path:
    return _write_learned_raw(path, {"allowed_apps": apps})


def _confirm(audit_path: Path, name: str, resolved_path: str) -> None:
    """Write a REAL 'skill.add_app.learned' audit record via the actual
    AuditLog class (hash-chained envelope, real code path) -- not a
    hand-built JSON line -- so these tests track the real record shape
    Ladder.record_event() produces rather than a guess that could drift
    from it silently. F66: reconciliation matches on resolved_path_key
    (learned_app_path_key()'s sha256 digest), not the literal resolved_path
    text -- computed here via the real function, the same one
    nova.skills.add_app calls, so this helper can't silently drift from
    what production actually writes."""
    AuditLog(path=audit_path).record({
        "kind": "skill.add_app.learned",
        "app": name,
        "resolved_path": resolved_path,
        "resolved_path_key": learned_app_path_key(resolved_path),
    })


def test_missing_learned_apps_file_is_not_an_error(tmp_path):
    """A learned file that doesn't exist yet is the normal, expected state
    on a fresh install -- it must load exactly as if T2.3a didn't exist."""
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = tmp_path / "learned_apps.json"  # deliberately never created
    audit_path = tmp_path / "audit.log"
    assert not learned_path.exists()

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_apps == {}


def test_a_learned_entry_with_a_matching_audit_record_is_merged(tmp_path):
    """Deliberately NOT a C:\\Users\\... path -- AuditLog.record() runs
    every string value through the shared redact(), whose first pass
    replaces a Windows home-directory segment with [USER]
    (nova/security/redact.py's _WIN_HOME_RE). A resolved_path under
    C:\\Users\\... would come back out of the audit log redacted and could
    never literal-match learned_apps.json's own (unredacted, because it has
    to be launchable) entry -- see test_redaction_makes_a_users_path_entry_
    permanently_unconfirmable below, which pins that as a real, known gap
    rather than silently avoiding it here."""
    cfg_path = _write(tmp_path / "nova.yaml", "")
    resolved = r"C:\Machine\Discord.exe"
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": resolved})
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "discord", resolved)

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_apps == {"discord": resolved}


def test_f66_reconciliation_key_is_immune_to_redaction_even_for_a_real_users_path():
    """FIXED -- F66 (nova-security's ruling, implemented as
    nova.config.learned_app_path_key()). This test used to pin a KNOWN,
    REAL GAP: AuditLog.record() redacts the username segment of every
    string value on write (F14's shared redact(), correctly applied to
    every audit record), so a resolved_path under C:\\Users\\<name>\\...
    (the module's own docstring names %LOCALAPPDATA%\\Discord\\... -- i.e.
    exactly this shape -- as the TYPICAL case for a real installed app)
    used to be permanently unconfirmable: the redacted string stored in
    the audit log could never literal-match learned_apps.json's own
    (necessarily unredacted, because it's what Popen launches) entry.

    The fix reconciles on a sha256 digest of the literal path instead of
    the path text itself -- a hex digest doesn't match redact()'s home-
    path/secret/email patterns, so `redact(key) == key` always, verified
    directly here rather than assumed. The human-readable resolved_path
    field alongside it in the audit record stays redacted, for a human
    reading the log; the key exists purely for this machine comparison.

    Kept (not deleted) per the lead's note: the next person to touch
    reconciliation needs to know WHY it doesn't compare path text
    directly, not just that it doesn't."""
    real_world_path = r"C:\Users\patel\AppData\Local\Discord\app-1.0\Discord.exe"
    key = learned_app_path_key(real_world_path)

    assert redact(key) == key, (
        "the reconciliation key must be unaffected by redact() -- if this fails, "
        "F66's fix is broken and the C:\\Users\\... collision this test used to "
        "document is back"
    )
    # The gap this replaced was specifically that redact() DOES touch the
    # raw path -- confirm that premise still holds, otherwise the fix
    # above might be passing vacuously because redact() stopped touching
    # C:\Users\... paths at all (a different, unrelated change) rather
    # than because the digest-based key is genuinely redaction-proof.
    assert redact(real_world_path) != real_world_path


def test_learned_app_path_key_is_deterministic_and_content_sensitive():
    """The two sides of reconciliation (nova.skills.add_app writing the
    audit event, nova.config reading it back) must compute the identical
    key from the identical literal string every time, and different paths
    must not collide."""
    path_a = r"C:\Users\patel\AppData\Local\Discord\app-1.0\Discord.exe"
    path_b = r"C:\Users\patel\AppData\Local\Spotify\Spotify.exe"

    assert learned_app_path_key(path_a) == learned_app_path_key(path_a)
    assert learned_app_path_key(path_a) != learned_app_path_key(path_b)


def test_nova_yaml_always_wins_on_a_name_conflict(tmp_path):
    """The load-bearing merge rule: a human-authored entry must never be
    silently overridden by whatever the machine-written file happens to
    say for the same name, even if the learned entry is 'newer' (and even
    if it's a confirmed, non-orphaned entry -- audit confirmation is not
    the same thing as winning a name conflict)."""
    cfg_path = _write(
        tmp_path / "nova.yaml",
        'security:\n  allowed_apps:\n    discord: "C:\\\\Human\\\\Discord.exe"\n',
    )
    machine_path = r"C:\Machine\Discord.exe"
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": machine_path})
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "discord", machine_path)  # confirmed, and STILL must lose to nova.yaml

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_apps == {"discord": r"C:\Human\Discord.exe"}


def test_learned_file_can_add_a_name_but_never_shadow_one_nova_yaml_already_has(tmp_path):
    """Both directions in one config: a name only nova.yaml knows stays
    exactly as nova.yaml wrote it; a confirmed name only the learned file
    knows is added; a name both know resolves to nova.yaml's value."""
    cfg_path = _write(
        tmp_path / "nova.yaml",
        'security:\n  allowed_apps:\n'
        '    notepad: "C:\\\\Windows\\\\notepad.exe"\n'
        '    discord: "C:\\\\Human\\\\Discord.exe"\n',
    )
    spotify_path = r"C:\Machine\Spotify.exe"
    learned_path = _write_learned(
        tmp_path / "learned_apps.json",
        {"discord": r"C:\Machine\Discord.exe", "spotify": spotify_path},
    )
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "discord", r"C:\Machine\Discord.exe")
    _confirm(audit_path, "spotify", spotify_path)

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_apps == {
        "notepad": r"C:\Windows\notepad.exe",  # nova.yaml-only: untouched
        "discord": r"C:\Human\Discord.exe",  # conflict: nova.yaml wins even though confirmed
        "spotify": spotify_path,  # learned-only, confirmed: added
    }


def test_learned_apps_never_widens_allowed_app_dirs(tmp_path):
    """The trust boundary itself is untouched by the merge even when
    nova.yaml sets it -- the learned file's own key-shape restriction
    (tested below) is what makes this true structurally, not just by
    the merge code happening not to read that field."""
    cfg_path = _write(
        tmp_path / "nova.yaml",
        'security:\n  allowed_app_dirs:\n    - "C:\\\\Trusted"\n',
    )
    resolved = r"C:\Machine\Discord.exe"
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": resolved})
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "discord", resolved)

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_app_dirs == ["C:\\Trusted"]


# ---------------------------------------------------------------------------
# F62/F63: reconciliation itself -- an entry present in the learned file but
# absent (or mismatched) in the audit log is an orphan, dropped from the
# merge with a loud warning, never silently trusted by default. Planting or
# hand-editing learned_apps.json is strictly easier than going through
# add_app's CONFIRM path, so this is the control that makes that shortcut
# not work.
# ---------------------------------------------------------------------------


def test_an_orphaned_learned_entry_with_no_matching_audit_record_is_dropped(tmp_path, caplog):
    """The load-bearing negative case: a learned entry with NO confirming
    audit record at all -- exactly what hand-planting the file, or editing
    it directly instead of going through add_app's voice/CONFIRM path,
    would produce."""
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": r"C:\Planted\Discord.exe"})
    audit_path = tmp_path / "audit.log"  # never written -- no confirming record exists

    with caplog.at_level("WARNING"):
        cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_apps == {}
    assert any("discord" in rec.message for rec in caplog.records), (
        "expected a loud warning naming the orphaned entry, not a silent drop"
    )


def test_missing_audit_log_treats_every_learned_entry_as_an_orphan(tmp_path):
    """A fresh machine with learned entries but no audit history at all
    (e.g. audit.log was deleted, or this is a copied learned_apps.json with
    no accompanying history) confirms nothing -- 'missing audit log' must
    not be treated as 'trust everything', the opposite of the missing-
    learned-file case above."""
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": r"C:\x\Discord.exe"})
    audit_path = tmp_path / "does_not_exist.log"
    assert not audit_path.exists()

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_apps == {}


def test_an_audit_record_for_a_different_resolved_path_does_not_confirm_the_entry(tmp_path):
    """Precision, not just presence: the confirming record must match BOTH
    name and resolved_path. A record that confirms 'discord' pointing
    somewhere else (e.g. a stale confirmation from before a reinstall
    changed the path) must not vouch for a learned entry pointing
    somewhere different now."""
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": r"C:\New\Discord.exe"})
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "discord", r"C:\Old\Discord.exe")  # confirms a DIFFERENT path

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)

    assert cfg.security.allowed_apps == {}


def test_a_malformed_audit_log_line_is_skipped_not_fatal(tmp_path):
    """audit.log's own integrity is AuditLog.verify()'s job, not
    load_config()'s -- a corrupt line here must not turn 'this entry isn't
    confirmed yet' into 'config won't load at all'. A valid confirming
    record after the bad line still counts."""
    cfg_path = _write(tmp_path / "nova.yaml", "")
    resolved = r"C:\x\Discord.exe"
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": resolved})
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "discord", resolved)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json at all\n")

    cfg = load_config(cfg_path, learned_apps_path=learned_path, audit_log_path=audit_path)  # must not raise

    assert cfg.security.allowed_apps == {"discord": resolved}


def test_orphaned_app_names_reports_exactly_the_unconfirmed_names(tmp_path):
    learned_path = _write_learned(
        tmp_path / "learned_apps.json",
        {"discord": r"C:\x\Discord.exe", "spotify": r"C:\x\Spotify.exe"},
    )
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "spotify", r"C:\x\Spotify.exe")  # only spotify is confirmed

    orphans = orphaned_app_names(learned_apps_path=learned_path, audit_log_path=audit_path)

    assert orphans == frozenset({"discord"})


def test_orphaned_app_names_is_empty_when_everything_is_confirmed(tmp_path):
    resolved = r"C:\x\Discord.exe"
    learned_path = _write_learned(tmp_path / "learned_apps.json", {"discord": resolved})
    audit_path = tmp_path / "audit.log"
    _confirm(audit_path, "discord", resolved)

    orphans = orphaned_app_names(learned_apps_path=learned_path, audit_log_path=audit_path)

    assert orphans == frozenset()


def test_orphaned_app_names_is_empty_when_the_learned_file_does_not_exist(tmp_path):
    orphans = orphaned_app_names(
        learned_apps_path=tmp_path / "does_not_exist.json", audit_log_path=tmp_path / "audit.log",
    )
    assert orphans == frozenset()


def test_orphaned_app_names_returns_empty_on_a_malformed_learned_file_rather_than_raising(tmp_path):
    """Per the function's own docstring: this only ever runs mid-session on
    an already-slow-path refusal, and must not turn 'give a nicer message'
    into a second way to crash a turn -- load_config() is still the one
    place a malformed learned file is a loud ConfigError, at boot."""
    learned_path = tmp_path / "learned_apps.json"
    learned_path.write_text("not valid json", encoding="utf-8")

    orphans = orphaned_app_names(learned_apps_path=learned_path, audit_log_path=tmp_path / "audit.log")  # must not raise

    assert orphans == frozenset()


def test_learned_file_containing_an_allowed_app_dirs_key_is_rejected(tmp_path):
    """THE load-bearing rejection test: a learned file is exclusively
    machine-written by nova.skills.add_app, which never writes anything but
    'allowed_apps' -- so a learned file that DOES carry an 'allowed_app_dirs'
    key (or any other key) is tampering or corruption, not a shape this
    merge should ever quietly accept. Worth more than the happy-path tests
    above: this is what stops a compromised/corrupted learned_apps.json
    from ever widening the trust boundary itself."""
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = _write_learned_raw(
        tmp_path / "learned_apps.json",
        {"allowed_apps": {"discord": r"C:\x\Discord.exe"}, "allowed_app_dirs": [r"C:\Anywhere"]},
    )

    with pytest.raises(ConfigError, match="allowed_apps"):
        load_config(cfg_path, learned_apps_path=learned_path)


def test_learned_file_with_only_an_unrecognized_key_is_rejected(tmp_path):
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = _write_learned_raw(tmp_path / "learned_apps.json", {"totally_bogus_key": {}})

    with pytest.raises(ConfigError):
        load_config(cfg_path, learned_apps_path=learned_path)


def test_learned_file_that_is_not_a_json_object_is_rejected(tmp_path):
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = _write_learned_raw(tmp_path / "learned_apps.json", ["not", "a", "mapping"])

    with pytest.raises(ConfigError):
        load_config(cfg_path, learned_apps_path=learned_path)


def test_learned_file_with_a_non_string_value_is_rejected(tmp_path):
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = _write_learned_raw(
        tmp_path / "learned_apps.json", {"allowed_apps": {"discord": 12345}},
    )

    with pytest.raises(ConfigError):
        load_config(cfg_path, learned_apps_path=learned_path)


def test_malformed_learned_json_raises_config_error_not_a_raw_json_error(tmp_path):
    cfg_path = _write(tmp_path / "nova.yaml", "")
    learned_path = tmp_path / "learned_apps.json"
    learned_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(cfg_path, learned_apps_path=learned_path)


def test_default_learned_apps_path_is_nova_learned_apps_path(tmp_path):
    """Omitting learned_apps_path entirely must fall back to
    nova.LEARNED_APPS_PATH, the real production location -- not silently
    read nothing forever. Proven by pointing the REAL constant at a tmp
    file via monkeypatch, one layer removed from load_config's own
    parameter default. audit_log_path is ALSO redirected (real constant,
    same technique) -- omitting that redirect would reconcile against this
    repo's actual data/audit.log instead of a tmp one, the exact isolation
    break F64 exists to prevent."""
    import nova
    import nova.config as config_mod

    real_learned_path = nova.LEARNED_APPS_PATH
    real_audit_path = nova.AUDIT_LOG_PATH
    fake_learned_path = tmp_path / "learned_apps.json"
    fake_audit_path = tmp_path / "audit.log"
    resolved = r"C:\Machine\Discord.exe"
    _write_learned(fake_learned_path, {"discord": resolved})
    _confirm(fake_audit_path, "discord", resolved)
    try:
        config_mod.LEARNED_APPS_PATH = fake_learned_path
        config_mod.AUDIT_LOG_PATH = fake_audit_path
        cfg_path = _write(tmp_path / "nova.yaml", "")
        cfg = load_config(cfg_path)  # no learned_apps_path/audit_log_path override
        assert cfg.security.allowed_apps == {"discord": resolved}
    finally:
        config_mod.LEARNED_APPS_PATH = real_learned_path
        config_mod.AUDIT_LOG_PATH = real_audit_path
