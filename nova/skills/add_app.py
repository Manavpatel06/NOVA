"""add_app — CONFIRM tier (T2.3a, docs/M2_OPEN_BRAIN_PLAN.md).

`open_app` refuses an unknown app name and stays refused (docs/THREAT_
MODEL.md §1.1) — the closed allowlist is the boundary and it holds. This
module is what happens *after* that refusal: NOVA can locate a real
executable for the name, show a human exactly what it resolved to, and —
only on explicit CONFIRM approval — add it to the allowlist and launch it.

Same threat shape as open_app.py, one step earlier: a spoken name must
never become a launched (or now, *allowlisted*) path without going through
verified resolution first. Four things this module does NOT do, each a
direct answer to how that could go wrong:

  1. **No filesystem sweep.** Resolution is a lookup against a known, small,
     curated surface — the two canonical Windows Start Menu directories
     (`CSIDL_PROGRAMS` / `CSIDL_COMMON_PROGRAMS`, resolved via the Shell API
     itself, not a guessed `%APPDATA%` join) — never a search over
     everything Manav owns. A name that doesn't resolve there is refused,
     not searched for harder.
  2. **No expanding the trust boundary.** A resolved shortcut target must
     still land inside the EXISTING `config.security.allowed_app_dirs` —
     the same containment check open_app.py's own `_resolve_and_verify`
     uses, deliberately re-implemented here rather than imported, so this
     file never has to touch open_app.py (which stays exactly as it was).
     This module can only ever narrow within an already-trusted boundary;
     it cannot widen it. See the module-level docstring further down (and
     this skill's report to the lead) for what that costs in practice —
     it's real, and worth reading before assuming this "just works" for an
     arbitrary Start Menu entry.
  3. **No unverified write.** `_resolve_candidate()` — the one resolution
     path, called at confirm-display time (via `resolve_for_confirm`, F46),
     again immediately after the ladder's CONFIRM decision (F46's own
     built-in TOCTOU check, in `SkillSpec.invoke()`), and a THIRD time here
     immediately before the write — never trusts a resolution made earlier
     in the conversation to still hold once a human has answered a dialog
     that could have sat open for up to 60s (constitution rule 3, and F46's
     "resolved-for-display disagreeing with resolved-at-call is a refusal"
     rule, one layer further out).
  4. **No unrecorded grant.** A successful write is audited with the
     resolved path via `Ladder.record_event()`, THEN round-tripped through
     `load_config()` (F65 — that order, not the reverse: `load_config()`
     trusts a learned entry only once it has a matching audit record, per
     nova/config.py's own reconciliation, F62/F63 — verifying first meant
     every fresh write looked unrecorded to that exact check and the write
     always rolled back, deadlocking every call). A NEW allowlist entry
     silently existing with no durable record of how it got there is
     exactly the kind of unaudited capability grant the constitution's
     audit rules exist to prevent. If the audit write itself fails, there
     is nothing yet to verify or correct — roll back the file and stop. If
     it succeeds but the round-trip THEN fails, the grant is already
     durably recorded and the audit log cannot un-record it (append-only,
     hash-chained) — so the file is rolled back AND a compensating
     `skill.add_app.rolled_back` event is appended for the same pair,
     never a silent discard. `nova.config._confirmed_learned_apps()`
     trusts a pair only if its LATEST recorded event is `learned`, not
     `rolled_back`.

**Where the allowlist entries actually live**: `data/learned_apps.json`
(nova.LEARNED_APPS_PATH), NOT `config/nova.yaml` — see nova/config.py's
module docstring for the merge rule and why a separate, machine-managed
file was the right shape (nova.yaml is hand-authored and carries comments
a dump-and-rewrite would destroy; this file is exclusively machine-written
and gitignored, same lifecycle as audit.log/audio_profile.json). nova.yaml
always wins on a name conflict — this module can only ever ADD a name
nova.yaml doesn't already configure, never override or shadow one.

**What "resolved outside the trusted directories" means in practice,
found while building this**: most real, user-installed Windows apps do
NOT live under `config.security.allowed_app_dirs` as shipped
(`C:\\Windows\\System32`, `SysWOW64`, `C:\\Windows`) — e.g. Discord
resolves to `%LOCALAPPDATA%\\Discord\\app-<version>\\Discord.exe`, well
outside all three. This module will correctly, honestly refuse to add such
an app rather than silently trusting a path outside the boundary Manav
configured — that refusal is the security boundary working as designed,
not a bug in this module, but it does mean T2.3a is of limited practical
use until `allowed_app_dirs` itself is widened, which is Manav's decision
to make in nova.yaml (a human, git-diffable edit), not this skill's to
make on his behalf by resolving somewhere new and hoping.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from nova import LEARNED_APPS_PATH
from nova.config import ConfigError, learned_app_path_key, load_config
from nova.log import get_logger
from nova.security.audit import AuditWriteError
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

logger = get_logger("skills.add_app")


# ---------------------------------------------------------------------------
# Resolution: Start Menu shortcut -> a verified, in-boundary absolute path.
# One function, called three separate times across the lifetime of one
# call (display, post-decision, pre-write) -- see module docstring point 3.
# ---------------------------------------------------------------------------


def _start_menu_dirs() -> list[Path]:
    """The two canonical Windows Start Menu Programs directories, resolved
    through the Shell API's own special-folder lookup (CSIDL_PROGRAMS /
    CSIDL_COMMON_PROGRAMS) rather than joining `%APPDATA%`/`%ProgramData%`
    by hand -- the API is authoritative about where these actually are
    (roaming profiles, folder redirection, etc.), an env-var guess is not.
    Imported lazily, like every pywin32 use elsewhere in this codebase
    (nova/security/confirm.py's own module docstring explains why): a
    missing pywin32 must degrade to "nothing resolved", never crash a
    skill call.
    """
    try:
        from win32comext.shell import shell, shellcon
    except Exception as exc:  # noqa: BLE001 — no pywin32 means no resolution, not a crash
        logger.warning("add_app: could not import the Windows Shell API (%s) — no Start Menu lookup possible", exc)
        return []
    dirs: list[Path] = []
    for csidl in (shellcon.CSIDL_PROGRAMS, shellcon.CSIDL_COMMON_PROGRAMS):
        try:
            dirs.append(Path(shell.SHGetFolderPath(0, csidl, 0, 0)))
        except Exception as exc:  # noqa: BLE001 — one folder failing to resolve must not take the other down with it
            logger.warning("add_app: SHGetFolderPath(%r) failed: %s", csidl, exc)
    return dirs


def _find_shortcut(name: str) -> tuple[Path | None, str]:
    """Search the two Start Menu directories (never anything else — see
    module docstring point 1) for a `.lnk` whose filename matches `name`.
    An exact, case-insensitive filename match wins outright. Failing that,
    a SINGLE unambiguous substring match is accepted; more than one is a
    refusal ("be more specific"), never a guess at which one was meant.
    """
    bases = _start_menu_dirs()
    if not bases:
        return None, "could not search the Start Menu (Windows Shell API unavailable)"

    candidates: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        candidates.extend(base.rglob("*.lnk"))

    name_lower = name.lower()
    exact = [c for c in candidates if c.stem.lower() == name_lower]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"more than one Start Menu shortcut is literally named '{name}' — ambiguous"

    partial = [c for c in candidates if name_lower in c.stem.lower()]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        names = ", ".join(sorted({p.stem for p in partial}))
        return None, f"more than one Start Menu shortcut matches '{name}' ({names}) — be more specific"
    return None, f"no Start Menu shortcut found for '{name}'"


def _shortcut_target(lnk_path: Path) -> Path | None:
    """The `.lnk`'s stored target path. Deliberately reads ONLY `Targetpath`
    — a shortcut's `Arguments` string is ignored and never forwarded
    anywhere, the same "argv list, no surprise parameters" reasoning
    open_app.py already applies to its own `Popen([resolved_path])` call.
    """
    try:
        import win32com.client

        wsh = win32com.client.Dispatch("WScript.Shell")
        target = wsh.CreateShortCut(str(lnk_path)).Targetpath
    except Exception as exc:  # noqa: BLE001 — a shortcut this module can't read is a refusal, not a crash
        logger.warning("add_app: could not read shortcut %s: %s", lnk_path, exc)
        return None
    if not target:
        return None
    return Path(target)


def _verify_within_trusted_dirs(candidate: Path, ctx: SkillCtx) -> tuple[Path | None, str]:
    """The containment check — deliberately the same shape as open_app.py's
    own `_resolve_and_verify` (module docstring point 2), re-implemented
    rather than imported so this file never has to touch open_app.py."""
    if not candidate.is_absolute():
        return None, "the shortcut's target is not an absolute path"
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None, f"the shortcut's target does not exist on disk ({candidate})"
    trusted_dirs = [Path(d).resolve() for d in ctx.config.security.allowed_app_dirs]
    if not any(resolved.is_relative_to(d) for d in trusted_dirs):
        return None, f"resolved outside every trusted application directory ({resolved})"
    return resolved, ""


def _resolve_candidate(name: object, ctx: SkillCtx) -> tuple[Path | None, str]:
    """One resolution path: Start Menu lookup -> shortcut target -> trust-
    boundary containment. Called fresh every time (module docstring point
    3) — nothing here is cached across calls, on purpose."""
    if not isinstance(name, str) or not name.strip():
        return None, "no app name given"
    lnk, reason = _find_shortcut(name.strip())
    if lnk is None:
        return None, reason
    target = _shortcut_target(lnk)
    if target is None:
        return None, f"could not read the shortcut '{lnk.name}'"
    return _verify_within_trusted_dirs(target, ctx)


async def _resolve_for_confirm(ctx: SkillCtx, params: dict) -> dict:
    """F46: what the CONFIRM prompt shows. Consent is to the resolved path,
    never to the spoken word — an unresolvable name still shows the honest
    reason it would be refused rather than nothing, so approving it (which
    changes nothing — see open_app._resolve_for_confirm for the identical
    precedent) never reads as approving a name NOVA couldn't actually
    verify."""
    name = params.get("app")
    resolved, reason = _resolve_candidate(name, ctx)
    if resolved is None:
        return {"app": f"{name!r} (would be refused: {reason})"}
    return {"app": str(resolved)}


# ---------------------------------------------------------------------------
# The machine-managed allowlist file (data/learned_apps.json) — atomic
# read-modify-write, rollback on any downstream verification failure.
# ---------------------------------------------------------------------------


def _read_learned_apps() -> dict[str, str]:
    if not LEARNED_APPS_PATH.exists():
        return {}
    try:
        with LEARNED_APPS_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        apps = raw.get("allowed_apps", {})
        return dict(apps) if isinstance(apps, dict) else {}
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        # A file this module itself owns coming back unreadable is treated
        # as "nothing learned yet", not a crash -- the next successful
        # write below repairs it. load_config()'s own read of this same
        # file (nova/config.py) is the one that fails LOUD on a malformed
        # file; this local read is only ever used to compute the next
        # write, so silent recovery here does not hide anything from a
        # human -- load_config() still would, and does.
        logger.warning("add_app: %s unreadable, treating as empty (%s)", LEARNED_APPS_PATH, exc)
        return {}


def _atomic_write_learned_apps(apps: dict[str, str]) -> None:
    """Write to a temp file in the same directory, then `os.replace()` —
    atomic on the same volume, Windows included — so a crash or a second
    concurrent write mid-save can never leave a half-written, corrupt JSON
    file for the next `load_config()` to choke on."""
    LEARNED_APPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = LEARNED_APPS_PATH.with_name(LEARNED_APPS_PATH.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump({"allowed_apps": apps}, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, LEARNED_APPS_PATH)


def _write_learned_app(name: str, resolved_path: Path) -> None:
    apps = _read_learned_apps()
    apps[name] = str(resolved_path)
    _atomic_write_learned_apps(apps)


def _remove_learned_app(name: str) -> None:
    """Rollback counterpart to `_write_learned_app` — used when a write
    can't be verified (round-trip through `load_config()` fails, or the
    write's own audit event can't be durably recorded)."""
    apps = _read_learned_apps()
    if name in apps:
        del apps[name]
        _atomic_write_learned_apps(apps)


# ---------------------------------------------------------------------------
# The skill itself.
# ---------------------------------------------------------------------------


@skill(
    name="add_app",
    tier=Tier.CONFIRM,
    examples=[
        "add spotify",
        "add discord",
        "add steam",
        "add obs",
        "add postman",
    ],
    trusts_input=False,
    resolve_for_confirm=_resolve_for_confirm,
)
async def add_app(ctx: SkillCtx, app: str) -> Reply:
    """Resolve `app` against the Start Menu, and — CONFIRM approval already
    granted by the time this runs — write it to the allowlist and launch
    it. See module docstring for the four things this deliberately does
    not do."""
    resolved, reason = _resolve_candidate(app, ctx)
    if resolved is None:
        return Reply(text=f"I couldn't add '{app}': {reason}.", success=False)

    name = app.strip().lower()

    # Constitution rule 3 / F46's TOCTOU shape, one layer further out: the
    # CONFIRM dialog (nova/security/confirm.py) can sit open for up to 60s,
    # and SkillSpec.invoke()'s own pre/post resolve_for_confirm check only
    # proves nothing changed between the PROMPT and the ladder's DECISION —
    # it says nothing about the gap between that decision and this line
    # actually running. Re-resolve one more time, right before the write.
    final_resolved, final_reason = _resolve_candidate(app, ctx)
    if final_resolved is None or final_resolved != resolved:
        logger.warning(
            "add_app: resolution changed or failed between decision and write for %r (now: %s)",
            app, final_reason or final_resolved,
        )
        return Reply(text=f"What '{app}' resolves to just changed — please ask again to be safe.", success=False)

    try:
        _write_learned_app(name, final_resolved)
    except OSError as exc:
        logger.error("add_app: failed to write %s: %s", LEARNED_APPS_PATH, exc)
        return Reply(text=f"Couldn't save '{app}' to the allowed apps list.", success=False)

    # F65 (nova-security, HIGH): record the grant BEFORE verifying, not
    # after -- F63's reconciliation (nova/config.py) trusts a learned entry
    # only if it has a matching 'skill.add_app.learned' audit record, so a
    # round-trip check run before that record exists would always see its
    # own fresh write as an orphan and roll it back. Every add_app call
    # deadlocked that way until this reorder. Not best-effort: an
    # unrecorded grant that a future ACT-tier open_app call could silently
    # use is exactly what "audit failure means deny" exists to prevent,
    # applied here to "was written" rather than "was allowed" -- if we
    # can't durably record the attempt, there is nothing to verify or
    # compensate for yet, so just roll back the file and stop.
    # F66 (nova-security): AuditLog.record() redacts every string it writes
    # (F14) -- a resolved path under the user's home directory (most real
    # installed apps) comes back out of the log with the username segment
    # rewritten to [USER], so it can never text-match learned_apps.json's
    # own literal, unredacted path (which has to stay literal -- it's what
    # Popen actually launches). `resolved_path_key` is a sha256 digest of
    # the literal path instead: it doesn't match any of redact()'s
    # patterns, so it comes back unchanged, and it's what
    # nova.config._confirmed_learned_apps() actually compares -- see
    # nova.config.learned_app_path_key()'s own docstring for the verified
    # claim. `resolved_path` stays in the payload too, redacted or not,
    # purely for a human reading the log.
    resolved_path_key = learned_app_path_key(str(final_resolved))

    try:
        ctx.ladder.record_event(
            "skill.add_app.learned",
            {
                "skill": "add_app",
                "app": name,
                "resolved_path": str(final_resolved),
                "resolved_path_key": resolved_path_key,
            },
        )
    except AuditWriteError as exc:
        _remove_learned_app(name)
        logger.error("add_app: audit write failed after writing %r, rolled back: %s", name, exc)
        return Reply(text=f"Couldn't durably record adding '{app}' — not saved.", success=False)

    # "The result must load" -- round-trip through the REAL load_config(),
    # never eyeballed. A config NOVA can't parse on next boot is a self-
    # inflicted denial of service; roll the write back rather than risk it.
    # The grant is now durably recorded (above), so a failure HERE is a
    # different class of problem than an unrecorded write: the audit log is
    # append-only and hash-chained, so the 'learned' entry just written
    # cannot be un-recorded. Silently leaving it standing would make the
    # ledger assert a grant that never actually took effect -- worse than
    # the deadlock this replaced, because it makes the log lie rather than
    # fail loudly. So a round-trip failure here rolls back the FILE (the
    # only part that's actually mutable) AND appends a compensating
    # 'skill.add_app.rolled_back' event for the exact same (app,
    # resolved_path) pair -- never a delete, always a correction, same
    # principle a real ledger uses. nova/config.py's reconciliation trusts
    # an entry only if its latest matching event is 'learned', not
    # 'rolled_back' -- see _confirmed_learned_apps() there.
    def _compensate(reason: str) -> None:
        _remove_learned_app(name)
        try:
            ctx.ladder.record_event(
                "skill.add_app.rolled_back",
                {
                    "skill": "add_app",
                    "app": name,
                    "resolved_path": str(final_resolved),
                    "resolved_path_key": resolved_path_key,
                    "reason": reason,
                },
            )
        except AuditWriteError as exc:
            # The FILE is still rolled back either way -- this name is not
            # in learned_apps.json any more, so nova/config.py's merge
            # will never trust it regardless of what the audit log shows.
            # A failure here only degrades the forensic trail (an
            # unexplained 'learned' with no visible follow-up), not the
            # security outcome, so it's loud and best-effort, not another
            # reason to keep retrying or to change what's returned below.
            logger.error(
                "add_app: could not record the compensating rollback for %r (%s): %s", name, reason, exc,
            )

    try:
        reloaded = load_config()
    except ConfigError as exc:
        _compensate("config_unparseable")
        logger.error("add_app: write produced an unparseable config, rolled back: %s", exc)
        return Reply(text=f"Adding '{app}' would have broken my config on restart — not saved.", success=False)
    if reloaded.security.allowed_apps.get(name) != str(final_resolved):
        _compensate("round_trip_mismatch")
        logger.error("add_app: round-trip mismatch for %r after write, rolled back", name)
        return Reply(text=f"Something went wrong saving '{app}' — not saved.", success=False)

    logger.info("add_app: learned %r -> %s, launching", app, final_resolved)
    try:
        subprocess.Popen([str(final_resolved)], shell=False)
    except OSError as exc:
        logger.error("add_app: learned %r but failed to launch: %s", app, exc)
        return Reply(text=f"Added '{app}', but couldn't launch it: {exc}", success=False)

    return Reply(text=f"Added {app} to my allowed apps, and opening it.", success=True)
