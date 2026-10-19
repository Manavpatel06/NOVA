"""Learned wake-name spellings (scripts/teach_name.py -> data/wake_names.json)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from nova.brain.address import UNSAFE_NAMES, first_name_word, load_learned_names, strip_address

_spec = importlib.util.spec_from_file_location(
    "teach_name", Path(__file__).resolve().parents[1] / "scripts" / "teach_name.py"
)


def _teach():
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


def test_first_name_word_skips_greeting():
    assert first_name_word("Hey Noah, what time is it?") == "noah"
    assert first_name_word("Nava open notepad") == "nava"
    assert first_name_word("") is None


def test_choose_names_needs_two_hits_and_rejects_common_words():
    choose = _teach().choose_names
    heard = ["noah", "noah", "oh", "oh", "oh", "nova", "nova", "neva", None]
    assert choose(heard, ["nova"]) == ["noah"]


def test_loader_drops_unsafe_and_junk(tmp_path):
    p = tmp_path / "wake_names.json"
    p.write_text(json.dumps({"names": ["Noah", "oh", "that", "x y", "", "nava"]}), encoding="utf-8")
    assert load_learned_names(p) == ["noah", "nava"]


def test_loader_tolerates_missing_or_broken_file(tmp_path):
    assert load_learned_names(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_learned_names(bad) == []


def test_learned_name_is_still_a_strict_prefix():
    names = ["nova", "noah"]
    assert strip_address("Noah, what time is it?", names) == "what time is it?"
    assert strip_address("I told Noah yesterday", names) is None


def test_unsafe_list_covers_calibration_first_words():
    # first words seen in Manav's 2026-10-14 calibration run
    for w in ("that", "um", "oh"):
        assert w in UNSAFE_NAMES


def test_app_merges_learned_names(tmp_path, monkeypatch):
    import nova
    import nova.app as app_mod

    p = tmp_path / "wake_names.json"
    p.write_text(json.dumps({"names": ["noah"]}), encoding="utf-8")
    monkeypatch.setattr(nova, "WAKE_NAMES_PATH", p)
    app = app_mod.NovaApp()
    assert app._wake_names == ["nova", "noah"]
