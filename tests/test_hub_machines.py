"""Runs your machines by voice (2026-11-25): the hub's own status and restart,
and opening any of Manav's documents or folders on the laptop."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from nova.hub import hub_skills
from nova.security.tiers import Tier
from nova.skills import open_file as of


# --- the hub ----------------------------------------------------------------------------

class _Devices:
    def __init__(self, on):
        self.on = on

    def online(self, d):
        return self.on


class _Conn:
    def status(self):
        return [{"name": "google", "state": "ready"}, {"name": "canvas", "state": "failed"}]


class _Spend:
    def status(self):
        return {"usd": 3.456, "budget_usd": 90.0}


def test_hub_status_says_how_the_hub_is():
    f = {"up_s": 2 * 86400 + 3 * 3600, "battery_pct": 81, "battery_status": "discharging", "temp_c": 61,
         "mem_used_pct": 42, "disk_free_gb": 300}
    text = hub_skills.status_text(f, connectors=_Conn(), devices=_Devices(True), spend=_Spend())
    assert text.startswith("The hub's been up 2 days 3 hours, battery 81 percent, on battery -- is it unplugged?")
    assert "61 degrees" in text and "42 percent used" in text and "300 gigabytes free" in text
    assert "laptop is connected" in text and "Connected apps: google." in text and "Not working: canvas" in text
    assert "3.46 of 90 dollars" in text


def test_hub_facts_read_without_running_anything():
    f = hub_skills.facts()
    if Path("/proc/uptime").exists():
        assert f["up_s"] > 0 and 0 <= f["mem_used_pct"] <= 100
    assert "disk_free_gb" in f


def test_hub_skills_tiers_and_restart():
    restarted = []
    skills = hub_skills.make(restart=lambda s: restarted.append(s))
    assert skills["hub_status"].tier is Tier.READ and skills["restart_nova"].tier is Tier.ACT
    reply = asyncio.run(skills["restart_nova"].handler(None))
    assert reply.success and restarted == [6.0]


def test_the_hubs_nova_offers_them_and_the_laptop_never_gets_them():
    from nova.config import Config
    from nova.hub.assistant import HubAssistant
    from nova.security.audit import AuditLog
    import nova.skills  # noqa: F401
    from nova.skills.registry import all_skills
    import tempfile

    a = HubAssistant(Config(), audit=AuditLog(Path(tempfile.mkdtemp()) / "a.log"), egress=None, brain=object(),
                     local_skills=hub_skills.make())
    assert {"hub_status", "restart_nova"} <= set(a.skills())
    assert "hub_status" not in all_skills() and "restart_nova" not in all_skills()


# --- open any file or folder -------------------------------------------------------------

@pytest.fixture
def home(tmp_path):
    docs = tmp_path / "Documents"
    (docs / "CSE 576").mkdir(parents=True)
    (docs / "CSE 576" / "quiz2_notes.pdf").write_text("x")
    (docs / "Resume_Manav_2026.docx").write_text("x")
    (docs / "budget.xlsx").write_text("x")
    (docs / "budget.exe").write_text("x")             # never offered
    (docs / "budget.lnk").write_text("x")
    (docs / ".hidden_budget.txt").write_text("x")
    (docs / "AppData").mkdir()
    (docs / "AppData" / "resume.pdf").write_text("x")  # skipped folder
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "Resume (old).pdf").write_text("x")
    (tmp_path / "Downloads" / "Resume (new).pdf").write_text("x")
    return tmp_path


def _roots(home):
    return [home / "Documents", home / "Downloads"]


def test_finds_a_document_by_part_of_its_name(home):
    hits = of.find("my resume", [home / "Documents"])
    assert hits[0][1].name == "Resume_Manav_2026.docx"


def test_finds_a_folder_when_asked_for_one(home):
    hits = of.find("cse 576", _roots(home), want_folder=True)
    assert hits[0][1].name == "CSE 576" and hits[0][1].is_dir()


def test_programs_shortcuts_hidden_and_skipped_folders_are_never_candidates(home):
    names = {p.name for _, p in of.find("budget", _roots(home))}
    assert names == {"budget.xlsx"}
    assert all("AppData" not in p.relative_to(home).parts for _, p in of.find("resume", _roots(home)))


def test_two_equally_good_matches_are_asked_about_not_guessed(home, monkeypatch):
    monkeypatch.setattr(of, "search_roots", lambda: [home / "Downloads"])
    of._cache.clear()
    path, choices = asyncio.run(of._best("resume"))
    assert path is None and {p.name for p in choices} == {"Resume (old).pdf", "Resume (new).pdf"}


def test_the_skill_opens_only_through_the_checked_opener(home, monkeypatch):
    from nova.system import winops
    from nova.skills.registry import get_skill

    opened = []
    monkeypatch.setattr(of, "search_roots", lambda: [home / "Documents"])
    monkeypatch.setattr(winops, "open_document", lambda p, roots: opened.append((p, roots)) or p)
    of._cache.clear()
    reply = asyncio.run(get_skill("open_file").handler(None, name="budget spreadsheet"))
    assert reply.success and reply.text == "Opened budget.xlsx (XLSX)."
    assert opened == [(str(home / "Documents" / "budget.xlsx"), [str(home / "Documents")])]
    miss = asyncio.run(get_skill("open_file").handler(None, name="tax return 1999"))
    assert not miss.success and "couldn't find" in miss.text


def test_the_approval_question_shows_the_path_found(home, monkeypatch):
    from nova.skills.registry import get_skill

    monkeypatch.setattr(of, "search_roots", lambda: [home / "Documents"])
    of._cache.clear()
    shown = asyncio.run(get_skill("open_file").resolve_for_confirm(None, {"name": "budget"}))
    assert shown == {"name": str(home / "Documents" / "budget.xlsx")}


@pytest.mark.skipif(os.name != "nt", reason="opens with Windows")
def test_the_opener_refuses_programs_outside_paths_and_network_paths(home, monkeypatch):
    from nova.system import winops

    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)
    roots = [str(home / "Documents")]
    with pytest.raises(ValueError, match="program"):
        winops.open_document(str(home / "Documents" / "budget.exe"), roots)
    with pytest.raises(ValueError, match="outside"):
        winops.open_document(str(home / "Downloads" / "Resume (old).pdf"), roots)
    with pytest.raises(ValueError, match="network"):
        winops.open_document("\\\\evil\\share\\x.pdf", roots)
    assert winops.open_document(str(home / "Documents" / "budget.xlsx"), roots).endswith("budget.xlsx")


def test_the_opener_is_windows_only_elsewhere():
    from nova.system import winops

    if os.name == "nt":
        pytest.skip("on Windows it opens")
    with pytest.raises(Exception):
        winops.open_document("/tmp/x.pdf", ["/tmp"])


def test_runnable_list_covers_the_dangerous_ones():
    from nova.system.winops import RUNNABLE

    assert {"exe", "bat", "cmd", "ps1", "vbs", "js", "msi", "lnk", "url", "hta", "scr", "docm", "xlsm", "reg"} <= RUNNABLE
