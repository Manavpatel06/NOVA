"""Contract: nova/skills/add_app.py (T2.3a, docs/M2_OPEN_BRAIN_PLAN.md) --
CONFIRM-tier Start Menu app resolution + allowlist growth.

Five properties this file exists to pin, per the lead's explicit ask,
because add_app is CONFIRM tier and every one guards a real consequence of
getting it wrong:

  1. Ambiguity is a refusal, never a guess -- two Start Menu shortcuts that
     could both plausibly be "Discord" must never resolve to either one
     silently.
  2. nova.yaml always wins over the learned file on a name conflict, and
     the learned file can only ADD names (also covered directly at the
     config.py level in test_config.py; here it's proven through add_app's
     own write+round-trip path -- see property 5, which is the SAME
     mechanism from a different angle: a write that nova.yaml would shadow
     never actually takes effect, and add_app must notice and refuse).
  3. The learned file has no key for allowed_app_dirs -- pinned directly in
     test_config.py (test_learned_file_containing_an_allowed_app_dirs_key_
     is_rejected); not duplicated here.
  4. Rollback on audit failure: an audit-write failure must leave the app
     absent from the learned file AND Popen uncalled.
  5. Round-trip through load_config(): a write that would NOT actually take
     effect on next boot (because nova.yaml already shadows the name) is
     refused and rolled back, not reported as a success.

The Windows Shell API (`win32comext.shell`) and shortcut-reading COM calls
(`win32com.client`) are faked at the two functions that actually touch them
(`_start_menu_dirs`, `_shortcut_target`) -- the REAL filesystem-scanning and
exact/ambiguous-match logic in `_find_shortcut` runs unmodified against real
`.lnk`-named files on disk, so the ambiguity tests below exercise real code,
not a mock of the decision itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import nova.skills.add_app as add_app_mod
from nova.config import Config, SecurityConfig
from nova.security.audit import AuditLog, AuditWriteError
from nova.security.ladder import Ladder
from nova.skills.registry import SkillCtx, get_skill

# F62/F63/F66 history, kept here for whoever next touches the write-path
# tests below: this file briefly carried three xfail(strict=True) tests
# and a shared marker for a KNOWN, REAL bug -- AuditLog.record() redacts
# the C:\Users\<name>\ segment of every string value on write (F14's
# shared redact()), which made the 'skill.add_app.learned' audit record's
# resolved_path never literal-match learned_apps.json's necessarily-
# unredacted entry for any path under C:\Users\...\ (this module's own
# documented common case, and unavoidably every pytest tmp_path on this
# machine). Reported 2026-08-20; fixed the same day as F66
# (nova.config.learned_app_path_key() -- reconcile on a sha256 digest of
# the literal path instead of the path text, since a hex digest is immune
# to redact()'s home-path pattern). strict=True did exactly its job: the
# fix landing turned two of the three tests into unexpected passes,
# which FAILED loudly rather than silently going green, forcing the
# markers off rather than letting them rot. See tests/test_config.py's
# test_f66_reconciliation_key_is_immune_to_redaction_even_for_a_real_
# users_path for where that reasoning now lives.


class _UnusedLadder:
    """Placeholder proving ctx.ladder is never reached in a given test --
    a refusal that happens before record_event() would be called."""

    async def authorize(self, skill, params):
        raise AssertionError("ctx.ladder.authorize() was called but this test never expected it to be")

    def record_event(self, kind, payload):
        raise AssertionError("ctx.ladder.record_event() was called but this test never expected it to be")


class _RecordEventRaisingLadder:
    """record_event() always fails -- for the rollback-on-audit-failure
    test. Tests using this call the handler directly (bypassing
    SkillSpec.invoke()), so authorize() is never exercised here."""

    def __init__(self):
        self.record_event_calls: list[tuple[str, dict]] = []

    def record_event(self, kind, payload):
        self.record_event_calls.append((kind, payload))
        raise AuditWriteError("disk full")


class _NoopLadder:
    """A ladder whose record_event() succeeds silently -- for tests that
    need add_app's happy path to actually complete."""

    def record_event(self, kind, payload):
        pass


class _FakePopen:
    calls: list[tuple[list, dict]] = []

    def __init__(self, argv, **kwargs):
        _FakePopen.calls.append((argv, kwargs))


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.calls = []
    fake_subprocess = type("FakeSubprocessModule", (), {"Popen": _FakePopen})()
    monkeypatch.setattr(add_app_mod, "subprocess", fake_subprocess)
    return _FakePopen


@pytest.fixture
def trusted_dir(tmp_path):
    d = tmp_path / "Trusted"
    d.mkdir()
    return d


@pytest.fixture
def start_menu(tmp_path, monkeypatch):
    """Stands in for the real Windows Start Menu directories. The real
    _find_shortcut() scanning/matching logic runs against this real
    directory, unmodified -- only where the scan starts is faked."""
    d = tmp_path / "StartMenu"
    d.mkdir()
    monkeypatch.setattr(add_app_mod, "_start_menu_dirs", lambda: [d])
    return d


@pytest.fixture
def shortcut_targets(monkeypatch):
    """Fakes the one COM-dependent function (_shortcut_target): a dict from
    .lnk Path to its target Path. Everything else in the resolution
    pipeline (_find_shortcut's scanning, _verify_within_trusted_dirs'
    containment check) is real, unmodified code."""
    targets: dict[Path, Path] = {}

    def fake_target(lnk_path: Path):
        return targets.get(lnk_path)

    monkeypatch.setattr(add_app_mod, "_shortcut_target", fake_target)
    return targets


def _make_shortcut(start_menu: Path, shortcut_targets: dict, stem: str, target: Path, subdir: str = "") -> Path:
    base = (start_menu / subdir) if subdir else start_menu
    base.mkdir(parents=True, exist_ok=True)
    lnk = base / f"{stem}.lnk"
    lnk.write_bytes(b"")  # content is irrelevant -- _shortcut_target is faked
    shortcut_targets[lnk] = target
    return lnk


def _real_exe(trusted_dir: Path, name: str) -> Path:
    exe = trusted_dir / name
    exe.write_bytes(b"fake exe")
    return exe


@pytest.fixture(autouse=True)
def _learned_apps_path(tmp_path, monkeypatch):
    """Every test in this file redirects add_app's own LEARNED_APPS_PATH
    binding to a tmp file -- unconditionally, autouse, regardless of
    whether a given test also asks for redirected_load_config below.

    This fixture exists because an earlier version of this suite got it
    wrong: a test that called add_app_mod.add_app() directly without
    requesting the (then opt-in) redirection fixture ran the real
    resolve-write-launch pipeline far enough to call the real
    `_write_learned_app()`, which wrote a tmp-only path into THIS REPO'S
    ACTUAL `data/learned_apps.json` (caught immediately by an unrelated
    test_config.py failure in the same run, since that file is real,
    machine-read state that load_config() merges in). The stray entry was
    deleted by hand after confirming (via mtime) it was created by that
    test run and nothing else. Making this autouse is the structural fix:
    it is no longer possible to write a test in this file that reaches
    add_app's disk-writing path without redirection."""
    learned_path = tmp_path / "learned_apps.json"
    monkeypatch.setattr(add_app_mod, "LEARNED_APPS_PATH", learned_path)
    return learned_path


@pytest.fixture
def redirected_load_config(tmp_path, monkeypatch, _learned_apps_path):
    """Additionally redirects load_config()'s own DEFAULT_CONFIG_PATH,
    LEARNED_APPS_PATH, and AUDIT_LOG_PATH bindings (nova/config.py imports
    each of these nova.* constants independently of add_app.py, per
    nova/__init__.py's "no module hardcodes a path" design -- each importer
    gets its own copy of the reference) to tmp files, reusing the SAME
    learned-apps path the autouse fixture above already pointed add_app's
    reader/writer at, so add_app()'s bare `load_config()` call inside the
    module round-trips against the exact file it just wrote, not a
    different tmp file that happens to also be empty. AUDIT_LOG_PATH is
    redirected for the same isolation reason F64 exists to name: without
    it, load_config()'s F62/F63 reconciliation would read THIS REPO'S
    ACTUAL data/audit.log to decide which learned entries to trust.

    Returns (nova_yaml_path, learned_apps_path); nova_yaml_path is not yet
    written -- callers write it with whatever `security:` section a given
    test needs."""
    import nova.config as config_mod

    nova_yaml = tmp_path / "nova.yaml"
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", nova_yaml)
    monkeypatch.setattr(config_mod, "LEARNED_APPS_PATH", _learned_apps_path)
    monkeypatch.setattr(config_mod, "AUDIT_LOG_PATH", tmp_path / "audit.log")
    return nova_yaml, _learned_apps_path


def _write_nova_yaml(path: Path, *, allowed_app_dirs: list, allowed_apps: dict | None = None) -> None:
    """JSON is valid YAML, and it sidesteps hand-built indentation and
    Windows-backslash escaping entirely -- every path below is written
    through json.dumps, never string-concatenated into the document."""
    security: dict = {"allowed_app_dirs": allowed_app_dirs}
    if allowed_apps is not None:
        security["allowed_apps"] = allowed_apps
    path.write_text(json.dumps({"security": security}), encoding="utf-8")


def _read_learned(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("allowed_apps", {})


# ---------------------------------------------------------------------------
# 1. Ambiguity is a refusal, never a guess
# ---------------------------------------------------------------------------


async def test_two_identically_named_shortcuts_refuse_rather_than_guess(
    start_menu, shortcut_targets, trusted_dir, fake_popen
):
    target = _real_exe(trusted_dir, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target, subdir="A")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target, subdir="B")
    ctx = SkillCtx(
        config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])),
        ladder=_UnusedLadder(),
    )

    reply = await add_app_mod.add_app(ctx, "discord")

    assert reply.success is False
    assert fake_popen.calls == []


async def test_two_partially_matching_shortcuts_refuse_rather_than_guess(
    start_menu, shortcut_targets, trusted_dir, fake_popen
):
    """This machine genuinely has more than one Start Menu entry whose name
    contains 'discord' (Discord, Discord Canary, Discord PTB) -- a
    substring match must refuse ambiguity exactly like an exact-name
    collision, not silently prefer the first one found."""
    canary = _real_exe(trusted_dir, "discord_canary.exe")
    ptb = _real_exe(trusted_dir, "discord_ptb.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord Canary", canary)
    _make_shortcut(start_menu, shortcut_targets, "Discord PTB", ptb)
    ctx = SkillCtx(
        config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])),
        ladder=_UnusedLadder(),
    )

    reply = await add_app_mod.add_app(ctx, "discord")

    assert reply.success is False
    assert fake_popen.calls == []


async def test_a_single_unambiguous_match_is_not_refused(
    start_menu, shortcut_targets, trusted_dir, fake_popen, redirected_load_config, tmp_path
):
    """The other half of the ambiguity proof: this ISN'T a suite that
    refuses everything. A single match -- even a partial, non-exact one --
    resolves and succeeds. Needs redirected_load_config like the other
    write-path tests: add_app() always round-trips through the real
    load_config() before declaring success, even when a test's own focus
    is ambiguity rather than the round-trip check itself.

    Uses a REAL Ladder/AuditLog (F66), not _NoopLadder -- reconciliation
    (F62/F63) only trusts a learned entry once a real 'skill.add_app.
    learned' record naming it exists, so record_event() has to actually
    persist something for the round-trip check to succeed."""
    nova_yaml, _ = redirected_load_config
    _write_nova_yaml(nova_yaml, allowed_app_dirs=[str(trusted_dir)])
    target = _real_exe(trusted_dir, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target)
    ladder = Ladder(AuditLog(path=tmp_path / "audit.log"))
    ctx = SkillCtx(
        config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])),
        ladder=ladder,
    )

    reply = await add_app_mod.add_app(ctx, "discord")

    assert reply.success is True
    assert len(fake_popen.calls) == 1
    assert fake_popen.calls[0][0] == [str(target.resolve())]
    assert fake_popen.calls[0][1].get("shell") is False


async def test_no_shortcut_found_is_a_refusal_not_a_crash(start_menu, shortcut_targets, trusted_dir, fake_popen):
    ctx = SkillCtx(
        config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])),
        ladder=_UnusedLadder(),
    )

    reply = await add_app_mod.add_app(ctx, "nothing_named_this_exists")

    assert reply.success is False
    assert fake_popen.calls == []


async def test_resolution_outside_trusted_dirs_is_refused(start_menu, shortcut_targets, tmp_path, fake_popen):
    """A shortcut resolves somewhere real, but outside every
    allowed_app_dirs entry -- the exact 'most real apps live outside the
    boundary' case the module docstring documents."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _real_exe(outside, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target)
    trusted_elsewhere = tmp_path / "Trusted"
    trusted_elsewhere.mkdir()
    ctx = SkillCtx(
        config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_elsewhere)])),
        ladder=_UnusedLadder(),
    )

    reply = await add_app_mod.add_app(ctx, "discord")

    assert reply.success is False
    assert fake_popen.calls == []


# ---------------------------------------------------------------------------
# 4. Rollback on audit failure: app absent from the learned file, Popen
# never called.
# ---------------------------------------------------------------------------


async def test_audit_write_failure_rolls_back_the_learned_file_and_never_launches(
    start_menu, shortcut_targets, trusted_dir, fake_popen, redirected_load_config
):
    nova_yaml, learned_path = redirected_load_config
    _write_nova_yaml(nova_yaml, allowed_app_dirs=[str(trusted_dir)])  # no shadow -- round trip would succeed
    target = _real_exe(trusted_dir, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target)
    ladder = _RecordEventRaisingLadder()
    ctx = SkillCtx(config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])), ladder=ladder)

    reply = await add_app_mod.add_app(ctx, "discord")

    assert reply.success is False
    assert len(ladder.record_event_calls) == 1  # the audit write WAS attempted, and failed
    assert "discord" not in _read_learned(learned_path)
    assert fake_popen.calls == [], "add_app launched the app despite the audit write failing"


# ---------------------------------------------------------------------------
# 5. Round-trip through load_config(): a write nova.yaml would shadow never
# actually takes effect on next boot, and must be refused, not reported as
# a success. This is property 2 (nova.yaml always wins) proven through
# add_app's own write path, not just load_config() in isolation.
# ---------------------------------------------------------------------------


async def test_a_write_that_nova_yaml_would_shadow_is_refused_and_rolled_back(
    start_menu, shortcut_targets, trusted_dir, fake_popen, redirected_load_config
):
    """nova.yaml ALREADY configures 'discord' pointing somewhere else. The
    learned-file write below would, in isolation, appear to succeed on
    disk -- but the merge rule means nova.yaml's entry always wins, so the
    write would never actually take effect on next boot. add_app's own
    round-trip check must catch this and roll the write back rather than
    telling Manav 'added' about something that didn't actually change
    anything."""
    nova_yaml, learned_path = redirected_load_config
    human_configured = _real_exe(trusted_dir, "human_discord.exe")
    _write_nova_yaml(
        nova_yaml,
        allowed_app_dirs=[str(trusted_dir)],
        allowed_apps={"discord": str(human_configured)},
    )
    machine_target = _real_exe(trusted_dir, "machine_discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", machine_target)
    # F65: record_event() now runs BEFORE the round-trip check (fixing the
    # deadlock where every add always saw its own unconfirmed write as an
    # orphan) -- so it legitimately gets called here too, before the shadow
    # is detected and rolled back. _NoopLadder, not _UnusedLadder: this
    # test's intent is the shadow/round-trip outcome, not whether
    # record_event happens to run.
    ctx = SkillCtx(config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])), ladder=_NoopLadder())

    reply = await add_app_mod.add_app(ctx, "discord")

    assert reply.success is False
    assert "discord" not in _read_learned(learned_path), (
        "the learned-file write was not rolled back even though nova.yaml "
        "shadows the same name -- 'added' would have been a false claim"
    )
    assert fake_popen.calls == [], "add_app launched despite the write never actually taking effect"


async def test_a_write_nova_yaml_does_not_shadow_succeeds_and_survives_the_round_trip(
    start_menu, shortcut_targets, trusted_dir, fake_popen, redirected_load_config, tmp_path
):
    """The other half of the round-trip proof: this isn't a suite that
    refuses every write. A name nova.yaml says nothing about really does
    get learned, launched, and left in the learned file afterward.

    Real Ladder/AuditLog (F66), not _NoopLadder -- see the comment on
    test_a_single_unambiguous_match_is_not_refused for why."""
    nova_yaml, learned_path = redirected_load_config
    _write_nova_yaml(nova_yaml, allowed_app_dirs=[str(trusted_dir)])
    target = _real_exe(trusted_dir, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target)
    ladder = Ladder(AuditLog(path=tmp_path / "audit.log"))
    ctx = SkillCtx(config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])), ladder=ladder)

    reply = await add_app_mod.add_app(ctx, "discord")

    assert reply.success is True
    assert _read_learned(learned_path).get("discord") == str(target.resolve())
    assert len(fake_popen.calls) == 1


# ---------------------------------------------------------------------------
# resolve_for_confirm: consent must be to the resolved path (or an honest
# refusal reason), never to the bare spoken name -- same contract
# open_app._resolve_for_confirm already earns coverage for.
# ---------------------------------------------------------------------------


async def test_resolve_for_confirm_shows_the_resolved_path_on_success(start_menu, shortcut_targets, trusted_dir):
    target = _real_exe(trusted_dir, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target)
    ctx = SkillCtx(config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])), ladder=_UnusedLadder())

    shown = await add_app_mod._resolve_for_confirm(ctx, {"app": "discord"})

    assert shown == {"app": str(target.resolve())}


async def test_resolve_for_confirm_shows_an_honest_refusal_reason_not_a_silent_success_shape(
    start_menu, shortcut_targets, trusted_dir
):
    ctx = SkillCtx(config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])), ladder=_UnusedLadder())

    shown = await add_app_mod._resolve_for_confirm(ctx, {"app": "nothing_named_this"})

    assert "would be refused" in shown["app"]


# ---------------------------------------------------------------------------
# End to end: real Ladder, real CONFIRM approval, through SkillSpec.invoke()
# -- proves the whole pipeline, not just the handler called directly.
# ---------------------------------------------------------------------------


class _ApprovingChannel:
    async def request(self, skill, params):
        return True


class _DecliningChannel:
    async def request(self, skill, params):
        return False


async def test_end_to_end_through_the_real_ladder_with_an_approving_channel(
    start_menu, shortcut_targets, trusted_dir, fake_popen, redirected_load_config, tmp_path
):
    nova_yaml, learned_path = redirected_load_config
    _write_nova_yaml(nova_yaml, allowed_app_dirs=[str(trusted_dir)])
    target = _real_exe(trusted_dir, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target)

    audit = AuditLog(path=tmp_path / "audit.log")
    ladder = Ladder(audit, confirm_channel=_ApprovingChannel())
    ctx = SkillCtx(config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])), ladder=ladder)
    spec = get_skill("add_app")
    assert spec is not None
    from nova.security.tiers import Tier
    assert spec.tier is Tier.CONFIRM

    reply = await spec.invoke(ctx, app="discord")

    assert reply.success is True
    assert len(fake_popen.calls) == 1
    assert _read_learned(learned_path).get("discord") == str(target.resolve())
    # authorize()'s own allow record, plus add_app's separate record_event
    # for the write itself (module docstring: deliberately two records).
    assert audit.verify().ok is True
    assert audit.verify().entries == 2


async def test_end_to_end_declined_confirm_never_writes_or_launches(
    start_menu, shortcut_targets, trusted_dir, fake_popen, redirected_load_config, tmp_path
):
    nova_yaml, learned_path = redirected_load_config
    _write_nova_yaml(nova_yaml, allowed_app_dirs=[str(trusted_dir)])
    target = _real_exe(trusted_dir, "discord.exe")
    _make_shortcut(start_menu, shortcut_targets, "Discord", target)

    audit = AuditLog(path=tmp_path / "audit.log")
    ladder = Ladder(audit, confirm_channel=_DecliningChannel())
    ctx = SkillCtx(config=Config(security=SecurityConfig(allowed_app_dirs=[str(trusted_dir)])), ladder=ladder)
    spec = get_skill("add_app")
    assert spec is not None

    reply = await spec.invoke(ctx, app="discord")

    assert reply.success is False
    assert fake_popen.calls == []
    assert "discord" not in _read_learned(learned_path)
