"""Contract: docs/THREAT_MODEL.md §1.1, docs/CONTRACTS.md T0.5 — open_app.

open_app is arbitrary code execution wearing a hat. Rules under test:
  - URL-shaped and UNC-shaped input rejected before any Windows API call;
  - unknown app name refused, never a best-effort guess;
  - realpath verification happens at CALL time, not load/config time — a
    junction that moves the resolved target between the two must be caught;
  - launch is always Popen([resolved_path], shell=False), never a string,
    never shell=True.

subprocess.Popen is replaced with a fake for every test here — nothing in
this file actually launches a process.
"""
from __future__ import annotations

import subprocess

import pytest

import nova.skills.open_app as open_app_mod
from nova.config import Config, SecurityConfig
from nova.skills.registry import SkillCtx


class _UnusedLadder:
    """Placeholder for SkillCtx.ladder in tests that call the open_app
    handler directly (bypassing SkillSpec.invoke()), so it's never actually
    read. Deliberately not a real Ladder — a call through this would fail
    loudly rather than accidentally doing something real."""

    async def authorize(self, skill, params):
        raise AssertionError("ctx.ladder.authorize() was called but this test never expected it to be")


class _FakePopen:
    calls: list[tuple[list, dict]] = []

    def __init__(self, argv, **kwargs):
        _FakePopen.calls.append((argv, kwargs))


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.calls = []
    fake_subprocess = type("FakeSubprocessModule", (), {"Popen": _FakePopen})()
    monkeypatch.setattr(open_app_mod, "subprocess", fake_subprocess)
    return _FakePopen


@pytest.fixture
def trusted_dir(tmp_path):
    d = tmp_path / "Program Files" / "TrustedApps"
    d.mkdir(parents=True)
    return d


def _ctx_with(allowed_apps: dict, allowed_app_dirs: list) -> SkillCtx:
    # These tests call open_app_mod.open_app(ctx, ...) directly (the raw
    # handler), not via SkillSpec.invoke() — so ctx.ladder is never read;
    # see test_skills_registry.py / test_security_ladder.py for the
    # authorization path itself.
    return SkillCtx(config=Config(security=SecurityConfig(
        allowed_apps=allowed_apps, allowed_app_dirs=allowed_app_dirs,
    )), ladder=_UnusedLadder())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_exact_match_allowed_app_launches(trusted_dir, fake_popen):
    exe = trusted_dir / "notepad.exe"
    exe.write_bytes(b"fake exe")
    ctx = _ctx_with({"notepad": str(exe)}, [str(trusted_dir)])

    reply = await open_app_mod.open_app(ctx, "notepad")

    assert reply.success is True
    assert len(fake_popen.calls) == 1
    argv, kwargs = fake_popen.calls[0]
    assert argv == [str(exe.resolve())]
    assert kwargs.get("shell") is False


async def test_launch_is_never_a_bare_string_or_shell_true(trusted_dir, fake_popen):
    exe = trusted_dir / "notepad.exe"
    exe.write_bytes(b"fake exe")
    ctx = _ctx_with({"notepad": str(exe)}, [str(trusted_dir)])

    await open_app_mod.open_app(ctx, "notepad")

    argv, kwargs = fake_popen.calls[0]
    assert isinstance(argv, list)
    assert kwargs.get("shell") is not True


# ---------------------------------------------------------------------------
# Unknown names, shell metacharacters: refused, not guessed
# ---------------------------------------------------------------------------


async def test_unknown_app_name_is_refused(trusted_dir, fake_popen):
    ctx = _ctx_with({}, [str(trusted_dir)])

    reply = await open_app_mod.open_app(ctx, "some_app_nobody_configured")

    assert reply.success is False
    assert fake_popen.calls == []


async def test_shell_metacharacter_string_is_refused_as_an_unknown_name(trusted_dir, fake_popen):
    exe = trusted_dir / "notepad.exe"
    exe.write_bytes(b"fake exe")
    ctx = _ctx_with({"notepad": str(exe)}, [str(trusted_dir)])

    reply = await open_app_mod.open_app(ctx, "notepad & calc")

    assert reply.success is False
    assert fake_popen.calls == []


async def test_empty_or_whitespace_app_name_is_refused(trusted_dir, fake_popen):
    ctx = _ctx_with({}, [str(trusted_dir)])
    reply = await open_app_mod.open_app(ctx, "   ")
    assert reply.success is False
    assert fake_popen.calls == []


# ---------------------------------------------------------------------------
# URL-shaped and UNC-shaped input: rejected before any Windows API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evil", [
    "http://attacker.example",
    "https://attacker.example/payload",
    "file://C:/Windows/System32/cmd.exe",
])
async def test_url_shaped_input_is_refused(trusted_dir, fake_popen, evil):
    ctx = _ctx_with({evil.lower(): str(trusted_dir / "x.exe")}, [str(trusted_dir)])
    reply = await open_app_mod.open_app(ctx, evil)
    assert reply.success is False
    assert fake_popen.calls == []


@pytest.mark.parametrize("evil", [
    r"\\attacker-host\share\payload.exe",
    "//attacker-host/share/payload.exe",
])
async def test_unc_shaped_input_is_refused(trusted_dir, fake_popen, evil):
    ctx = _ctx_with({evil.lower(): str(trusted_dir / "x.exe")}, [str(trusted_dir)])
    reply = await open_app_mod.open_app(ctx, evil)
    assert reply.success is False
    assert fake_popen.calls == []


async def test_url_shaped_input_never_even_reaches_the_allowlist_lookup(trusted_dir, fake_popen, monkeypatch):
    """Stronger than 'it gets refused': it must be refused by the shape
    check specifically, not merely because it happens not to match an
    allowlist entry."""
    calls = []
    real = open_app_mod._resolve_and_verify

    def spy(name, ctx):
        calls.append(name)
        return real(name, ctx)

    monkeypatch.setattr(open_app_mod, "_resolve_and_verify", spy)
    ctx = _ctx_with({}, [str(trusted_dir)])

    await open_app_mod.open_app(ctx, "http://attacker.example")

    assert calls == []  # never reached the allowlist-resolution step at all


# ---------------------------------------------------------------------------
# realpath verification at CALL time, not load time: junction moves between
# calls must be caught (the TOCTOU the constitution's filesystem-write rule
# targets, applied to a launch decision instead of a write).
# ---------------------------------------------------------------------------


def _make_junction(link: "object", target: "object") -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


async def test_realpath_verification_happens_at_call_time_junction_swap(tmp_path, fake_popen):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    real_target_inside = trusted / "real_target"
    real_target_inside.mkdir()
    (real_target_inside / "app.exe").write_bytes(b"good")

    real_target_outside = outside / "real_target"
    real_target_outside.mkdir()
    (real_target_outside / "app.exe").write_bytes(b"evil")

    link = trusted / "link"
    if not _make_junction(link, real_target_inside):
        pytest.skip("mklink /J unavailable on this machine/account")

    ctx = _ctx_with({"myapp": str(link / "app.exe")}, [str(trusted)])

    # First call: junction points inside the trusted dir -> allowed.
    reply1 = await open_app_mod.open_app(ctx, "myapp")
    assert reply1.success is True
    assert len(fake_popen.calls) == 1

    # Swap the junction to point outside the trusted dir, *after* config was
    # already loaded — nothing about ctx.config changed.
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)
    if not _make_junction(link, real_target_outside):
        pytest.skip("mklink /J re-creation failed on this machine/account")

    reply2 = await open_app_mod.open_app(ctx, "myapp")

    assert reply2.success is False, (
        "open_app allowed a launch through a junction that now resolves "
        "outside allowed_app_dirs — realpath verification is not happening "
        "at call time"
    )
    assert len(fake_popen.calls) == 1  # still just the first, legitimate call


async def test_target_outside_allowed_dirs_from_the_start_is_refused(tmp_path, fake_popen):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside" / "app.exe"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")

    ctx = _ctx_with({"myapp": str(outside)}, [str(trusted)])

    reply = await open_app_mod.open_app(ctx, "myapp")

    assert reply.success is False
    assert fake_popen.calls == []


# ---------------------------------------------------------------------------
# Misconfiguration: refused, not a crash
# ---------------------------------------------------------------------------


async def test_relative_configured_path_is_refused(trusted_dir, fake_popen):
    ctx = _ctx_with({"myapp": "relative/path.exe"}, [str(trusted_dir)])
    reply = await open_app_mod.open_app(ctx, "myapp")
    assert reply.success is False
    assert fake_popen.calls == []


async def test_configured_target_missing_on_disk_is_refused(trusted_dir, fake_popen):
    ctx = _ctx_with({"myapp": str(trusted_dir / "does_not_exist.exe")}, [str(trusted_dir)])
    reply = await open_app_mod.open_app(ctx, "myapp")
    assert reply.success is False
    assert fake_popen.calls == []
