"""Shared fixtures for the NOVA test suite.

Contract-first note: these tests are written against docs/CONTRACTS.md and,
where the real T0.1-T0.3 modules have landed, against their actual (white-box)
seams — but always asserting *contract behavior*, not incidental
implementation choices.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def spaced_tmp_path(tmp_path: Path) -> Path:
    """A tmp subdirectory whose name contains a space, a Windows-legal
    non-ASCII-free path stressor. CLAUDE.md: 'Long paths, spaces, and
    backslashes are normal — test for them.'"""
    d = tmp_path / "dir with spaces (NOVA)"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _never_start_a_real_claude_session(monkeypatch):
    """The Max-plan brain (nova.brain.max_brain) keeps a real `claude`
    process running. No test may ever start one: it would use whatever
    account the machine is signed in with, and be slow. Tests that exercise
    ClaudeSession itself replace the spawn with a fake."""
    import nova.brain.claude_arm as arm

    async def _refuse(self):
        raise RuntimeError("tests never start a real claude session")

    monkeypatch.setattr(arm.ClaudeSession, "start", _refuse)


@pytest.fixture(autouse=True)
def _memory_in_tmp(tmp_path, monkeypatch):
    """NOVA's memory (facts, journal) never touches the real data folder in tests."""
    import nova.memory.store as store

    monkeypatch.setattr(store, "_DEFAULT", store.MemoryStore(tmp_path / "nova-memory"))
