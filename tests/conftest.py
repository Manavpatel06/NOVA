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
