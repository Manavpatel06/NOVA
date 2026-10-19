"""open_app — ACT tier (docs/CONTRACTS.md T0.5, hardened per
docs/THREAT_MODEL.md §1.1).

`open_app` is arbitrary code execution wearing a hat: it takes a parameter
derived from what the user said and launches a process. The obvious
implementation — hand the routed string to `os.startfile()`, `ShellExecute`,
or `subprocess.Popen(name, shell=True)` — lets that string be a URL
(`os.startfile("http://evil.example")` opens it in the default browser,
invisibly to Egress) or a UNC path (an SMB fetch, same blind spot) or shell
metacharacters. None of that is hypothetical; §1.1 calls it live, not
theoretical.

What this module actually does, in order, on every call:
  1. Reject the raw `app` string outright if it's shaped like a URL
     (contains "://") or a UNC path (starts with "\\\\" or "//") — before it
     touches any Windows API, not relying on the allowlist to catch it.
  2. Look the (normalized) name up in a closed allowlist
     (config.security.allowed_apps) — an unknown name is a refusal, never a
     best-effort guess. The free-text name itself never reaches Popen.
  3. realpath()-resolve the allowlisted path *at call time* (not when config
     was loaded) and verify the resolved target still lands inside a trusted
     directory (config.security.allowed_app_dirs), using path containment,
     not a string prefix — a junction pointed out of Program Files defeats
     `startswith` and is cheap to create on Windows.
  4. Launch with `subprocess.Popen([resolved_path], shell=False)` — an argv
     list, never a shell string.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import asyncio
import json
import re
import time

from nova.config import orphaned_app_names
from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

logger = get_logger("skills.open_app")

# --- installed apps from the Start menu (2026-10-15) -----------------------
# Apps outside the curated allowlist (Spotify, Discord, VS Code, Store apps)
# are launched by the AppUserModelID Windows itself reports in Get-StartApps
# (shell:AppsFolder\<id>) -- the same thing clicking the Start-menu entry
# does. Speech only *selects* an entry from Windows' own list; it never
# becomes a path or a command.
_START_APPS_PS = "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
_START_APPS_TTL_S = 300.0
_start_apps_cache: tuple[float, list[tuple[str, str]]] | None = None
_SKIP_WORDS = (
    "uninstall", "readme", "help", "documentation", "release notes", "website", "license",
    "repair", "reset", "setup", "modify", "remove", "update",
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


async def _start_apps() -> list[tuple[str, str]]:
    global _start_apps_cache
    now = time.monotonic()
    if _start_apps_cache is not None and now - _start_apps_cache[0] < _START_APPS_TTL_S:
        return _start_apps_cache[1]
    from nova.system import winops

    raw = await asyncio.to_thread(winops.powershell, _START_APPS_PS, timeout=20.0)
    data = json.loads(raw) if raw else []
    if isinstance(data, dict):
        data = [data]
    apps = [
        (str(d.get("Name", "")), str(d.get("AppID", "")))
        for d in data if isinstance(d, dict) and d.get("Name") and d.get("AppID")
    ]
    _start_apps_cache = (now, apps)
    return apps


_ALIASES = {
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "edge": "microsoft edge", "teams": "microsoft teams", "store": "microsoft store",
    "terminal": "terminal", "powershell": "windows powershell", "outlook app": "outlook",
}


def best_start_app(spoken: str, apps: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Pick the Start-menu entry the spoken name means, or None. Exact name
    first, then every spoken word present in the entry's name (shortest
    such name wins: "spotify" -> "Spotify", not "Spotify Uninstaller")."""
    want = _words(_ALIASES.get(" ".join(_words(spoken)), spoken))
    if not want:
        return None
    usable = [(n, i) for n, i in apps if not any(w in n.lower() for w in _SKIP_WORDS)]
    joined = " ".join(want)
    for name, app_id in usable:
        if " ".join(_words(name)) == joined:
            return name, app_id
    squashed = "".join(want)
    candidates = [
        (name, app_id) for name, app_id in usable
        if all(w in _words(name) for w in want) or "".join(_words(name)) == squashed
    ]
    if not candidates:
        candidates = [(n, i) for n, i in usable if "".join(_words(n)).startswith(squashed) and len(squashed) >= 4]
    if not candidates:
        return None
    return min(candidates, key=lambda c: len(c[0]))


def _is_url_or_unc_shaped(raw: str) -> bool:
    """True if `raw` parses as a URL or a UNC path. Checked on the raw,
    unmodified string before any normalization — the shape check must not be
    foolable by whitespace/case tricks that would still resolve the same way
    to Windows."""
    stripped = raw.strip()
    if "://" in stripped:
        return True
    if stripped.startswith("\\\\") or stripped.startswith("//"):
        return True
    return False


def _resolve_and_verify(name: str, ctx: SkillCtx) -> tuple[Path, str] | tuple[None, str]:
    """Look `name` up in the allowlist and verify it at call time. Returns
    (resolved_path, "") on success, or (None, reason) on refusal."""
    configured = ctx.config.security.allowed_apps.get(name)
    if configured is None:
        return None, f"'{name}' is not in the allowed-apps list"

    configured_path = Path(configured)
    if not configured_path.is_absolute():
        logger.error(
            "open_app: allowed_apps['%s'] = %r is not an absolute path; refusing",
            name, configured,
        )
        return None, f"'{name}' is misconfigured (not an absolute path)"

    try:
        resolved = configured_path.resolve(strict=True)
    except OSError as exc:
        logger.error(
            "open_app: allowed_apps['%s'] = %r does not resolve to a real file: %s",
            name, configured, exc,
        )
        return None, f"'{name}' is configured but the target no longer exists"

    trusted_dirs = [Path(d).resolve() for d in ctx.config.security.allowed_app_dirs]
    if not any(resolved.is_relative_to(d) for d in trusted_dirs):
        logger.warning(
            "open_app: allowed_apps['%s'] resolved to %s, which is outside every "
            "allowed_app_dirs entry - refusing (possible junction/symlink tampering)",
            name, resolved,
        )
        return None, f"'{name}' resolved outside the trusted application directories"

    return resolved, ""


async def _resolve_for_confirm(ctx: SkillCtx, params: dict) -> dict:
    """F46/T2.0: what a CONFIRM prompt shows for this skill, in place of the
    raw free-text `app` name — open_app is ACT-tier today, so nothing calls
    this yet in production, but a floor (F28/T2.4) can raise ACT to CONFIRM,
    and when that happens consent must be to the resolved executable path,
    not to the word a human said. Reuses the exact same
    `_is_url_or_unc_shaped` / `_resolve_and_verify` checks `open_app()`
    itself runs — display-only, and deliberately NOT cached or reused by
    the handler below: `open_app()` still calls `_resolve_and_verify` again,
    itself, at actual launch time, exactly as it did before this existed
    (constitution rule 3 — a resolution is never trusted to still mean the
    same thing later, including a resolution this very function produced
    moments earlier for display).
    """
    app = params.get("app")
    if not isinstance(app, str) or not app.strip():
        return {"app": "(no app name given)"}
    if _is_url_or_unc_shaped(app):
        return {"app": f"{app!r} (would be refused: looks like a URL or network path)"}

    name = app.strip().lower()
    resolved, reason = _resolve_and_verify(name, ctx)
    if resolved is None:
        if name not in orphaned_app_names():
            # The handler falls back to the Start menu for anything off the
            # allowlist, so consent must be to THAT entry (and the post-
            # decision re-check must see a different entry as a change) --
            # not to a "would be refused" the handler then ignores.
            from nova.system import winops

            try:
                found = best_start_app(app, await _start_apps())
            except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
                return {"app": f"{app!r} (would be refused: Start-menu apps unavailable: {type(exc).__name__})"}
            if found is not None:
                return {"app": found[0], "start_menu_app_id": found[1]}
        return {"app": f"{app!r} (would be refused: {reason})"}
    return {"app": str(resolved)}


async def _open_from_start_menu(app: str) -> Reply:
    from nova.system import winops

    try:
        found = best_start_app(app, await _start_apps())
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        logger.error("open_app: could not list Start-menu apps (%s)", exc)
        return Reply(text=f"I don't know how to open '{app}'.", success=False)
    if found is None:
        return Reply(text=f"I couldn't find an app called {app}.", success=False)
    title, app_id = found
    logger.info("open_app: launching Start-menu app %r (%s)", title, app_id)
    try:
        await asyncio.to_thread(winops.launch_start_app, app_id)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError, OSError) as exc:
        logger.error("open_app: launching %r failed (%s)", title, exc)
        return Reply(text=f"Couldn't open {title}.", success=False)
    return Reply(text=f"Opening {title}.", success=True)


@skill(
    name="open_app",
    tier=Tier.ACT,
    examples=[
        "open notepad",
        "launch calculator",
        "open file explorer",
        "open task manager",
        "open control panel",
    ],
    description=(
        "Launch an application already on the allowed-apps list by name, e.g. "
        "'notepad', 'calculator', 'file explorer'. One parameter: app (str) -- "
        "the app's name. An app not already on the list will be refused, not launched."
    ),
    trusts_input=False,
    resolve_for_confirm=_resolve_for_confirm,
    confirm_phrase="open {app}",
    catch_all_patterns=[
        # The verb alternation must be unambiguous: "start up" used to match
        # both as "start up" and as "start" + " up", so a sentence repeating
        # it backtracked 2^n ways (~50 s for 22 repeats, on the event loop).
        r"(?:(?:(?:open|launch|start|run|load)(?: up)?|fire up|boot up) )+(?:the |my )?(?P<app>(?!.*\b(?:settings|website|site|tab|page|file|folder)$)[a-z0-9][a-z0-9 '&+.-]{0,40}?)(?: app| application| program)?",
    ],
)
async def open_app(ctx: SkillCtx, app: str) -> Reply:
    """Launch a known Windows application by name. `app` is resolved only
    against `config.security.allowed_apps` — never passed to a shell, never
    used as a path directly."""
    if not isinstance(app, str) or not app.strip():
        return Reply(text="I need an app name to open.", success=False)

    if _is_url_or_unc_shaped(app):
        logger.warning("open_app: refused URL/UNC-shaped input %r", app)
        return Reply(text="That doesn't look like an app name.", success=False)

    name = app.strip().lower()
    resolved, reason = _resolve_and_verify(name, ctx)
    if resolved is None:
        # F62's UX follow-up: a name that's in data/learned_apps.json but
        # was refused at load time for having no matching audit record
        # (F63 -- see nova.config's module docstring) still deserves a
        # more honest refusal than the generic "I don't know how to open
        # X" a name NOVA has genuinely never seen. This is a text-only
        # difference -- both branches return the identical Reply shape
        # (ACT tier, success=False, no side effect) -- it changes nothing
        # about what's trusted, only what a human is told about why.
        if name in orphaned_app_names():
            return Reply(
                text=f"I have '{app}' on file, but no record of ever approving it — try adding it again.",
                success=False,
            )
        return await _open_from_start_menu(app)

    logger.info("open_app: launching %r -> %s", app, resolved)
    try:
        subprocess.Popen([str(resolved)], shell=False)
    except OSError as exc:
        logger.error("open_app: failed to launch %s: %s", resolved, exc)
        return Reply(text=f"Couldn't open {app}: {exc}", success=False)

    return Reply(text=f"Opening {app}.", success=True)
