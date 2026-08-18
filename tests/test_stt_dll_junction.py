"""Contract: nova/stt/_dll.py — DLL search-path trust grant must not follow
an unresolved junction outside site-packages.

The gap (found by nova-security, confirmed by the lead): `_site_roots()`
resolved its own root candidates, but `ensure_dll_directories()` stopped one
level short — it walked `nvidia_root.iterdir()` and handed each discovered
`pkg_dir / "bin"` straight to `os.add_dll_directory()` without resolving it.
A junction at `site-packages/nvidia/<anything>/bin` pointing elsewhere on
disk got silently added to the process's trusted DLL search path, and
Windows resolves junctions at *load* time, not when the code inspected the
path — same TOCTOU shape as the constitution's filesystem-write rule,
applied to a trust grant instead of a write.

This test was written as the POST-FIX promise per the lead's instruction.
By the time it ran against the real code, nova-dev-stt had already landed
the fix (`_ALLOWED_NVIDIA_PACKAGES` + `_resolved_descendant()` — both a
package-name allowlist AND a resolve-and-check-containment step), so this
is now a green regression guard rather than a documented gap. Test data
uses real allowlisted package names (`cublas`, `cudnn`, ...) rather than
made-up ones, specifically so it exercises the containment check and not
just the (separate, also-real) package-name filter.

Skips cleanly (names the missing capability) if this machine/account can't
create NTFS junctions rather than failing — it needs a real junction on a
real filesystem, this isn't mockable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import nova.stt._dll as dll_mod


def _make_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


@pytest.fixture
def isolated_dll_state(monkeypatch):
    """ensure_dll_directories() is idempotent via a module-global _done flag
    — by the time this test runs, importing nova.stt anywhere in the suite
    has already populated it for real. Reset both globals so this test
    exercises a fresh scan, and stub os.add_dll_directory so nothing here
    touches the real process DLL search path."""
    monkeypatch.setattr(dll_mod, "_done", False)
    monkeypatch.setattr(dll_mod, "_added_dirs", [])
    recorded: list[str] = []
    monkeypatch.setattr(dll_mod.os, "add_dll_directory", lambda p: recorded.append(p))
    return recorded


def test_junction_escaping_site_packages_is_excluded(tmp_path, monkeypatch, isolated_dll_state):
    fake_site = tmp_path / "fake_site_packages"
    # "cudnn" is a real entry in _ALLOWED_NVIDIA_PACKAGES — using it (rather
    # than a made-up name) means this test exercises the resolve/containment
    # check specifically, not the separate package-name allowlist.
    evil_pkg_bin = fake_site / "nvidia" / "cudnn" / "bin"
    evil_pkg_bin.parent.mkdir(parents=True)  # nvidia/cudnn/, not /bin itself — that's the junction

    outside = tmp_path / "outside_the_tree"
    outside.mkdir()
    (outside / "malicious.dll").write_bytes(b"not really a dll")

    if not _make_junction(evil_pkg_bin, outside):
        pytest.skip("mklink /J unavailable on this machine/account")

    # A legitimate nvidia bin dir under another allowlisted package name, to
    # prove the fix doesn't break real discovery while closing the escape.
    good_bin = fake_site / "nvidia" / "cublas" / "bin"
    good_bin.mkdir(parents=True)

    monkeypatch.setattr(dll_mod, "_site_roots", lambda: [fake_site.resolve()])

    dll_mod.ensure_dll_directories()

    recorded = isolated_dll_state
    resolved_site = fake_site.resolve()
    resolved_outside = outside.resolve()

    assert not any(Path(p).resolve() == resolved_outside for p in recorded), (
        f"a junction pointing outside site-packages was added to the DLL "
        f"search path: recorded={recorded}"
    )
    assert all(Path(p).resolve().is_relative_to(resolved_site) for p in recorded), (
        f"every recorded DLL directory must resolve inside the site-packages "
        f"tree it was discovered under; recorded={recorded}"
    )
    assert any(Path(p).resolve() == good_bin.resolve() for p in recorded), (
        "the fix must not also exclude a legitimate, non-junction nvidia bin dir"
    )


def test_non_allowlisted_package_name_is_excluded_even_with_a_legitimate_path(
    tmp_path, monkeypatch, isolated_dll_state
):
    """The second, independent half of the fix: `_ALLOWED_NVIDIA_PACKAGES`
    and the resolve/containment check are two separate gates. A directory
    with a perfectly legitimate resolved path (no junction, genuinely inside
    site-packages) must still be excluded if its package name isn't on the
    allowlist — nova-dev-stt reported testing this during their own
    verification (nvidia\\evilpkg\\bin), but nothing pinned it with a test
    until now."""
    fake_site = tmp_path / "fake_site_packages"
    unlisted_bin = fake_site / "nvidia" / "evilpkg" / "bin"
    unlisted_bin.mkdir(parents=True)  # real directory, no junction involved at all

    allowed_bin = fake_site / "nvidia" / "cublas" / "bin"
    allowed_bin.mkdir(parents=True)

    monkeypatch.setattr(dll_mod, "_site_roots", lambda: [fake_site.resolve()])

    dll_mod.ensure_dll_directories()

    recorded = isolated_dll_state
    assert not any(Path(p).resolve() == unlisted_bin.resolve() for p in recorded), (
        f"a directory under a non-allowlisted nvidia package name was granted "
        f"DLL search authority despite a legitimate path: recorded={recorded}"
    )
    assert any(Path(p).resolve() == allowed_bin.resolve() for p in recorded), (
        "the allowlisted package's bin dir should still have been found"
    )


def test_ensure_dll_directories_is_idempotent_within_a_single_scan(tmp_path, monkeypatch, isolated_dll_state):
    """Sanity check unrelated to the junction gap: calling it twice in the
    same (fresh) state must not double-add the same real directory."""
    fake_site = tmp_path / "fake_site_packages"
    good_bin = fake_site / "nvidia" / "cublas" / "bin"
    good_bin.mkdir(parents=True)
    monkeypatch.setattr(dll_mod, "_site_roots", lambda: [fake_site.resolve()])

    dll_mod.ensure_dll_directories()
    first_count = len(isolated_dll_state)
    assert first_count == 1  # sanity: the allowlisted dir was actually found
    dll_mod.ensure_dll_directories()  # _done is now True; must be a no-op

    assert len(isolated_dll_state) == first_count
