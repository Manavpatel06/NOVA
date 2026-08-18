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

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

logger = get_logger("skills.open_app")


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
    trusts_input=False,
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
        return Reply(text=f"I don't know how to open '{app}'.", success=False)

    logger.info("open_app: launching %r -> %s", app, resolved)
    try:
        subprocess.Popen([str(resolved)], shell=False)
    except OSError as exc:
        logger.error("open_app: failed to launch %s: %s", resolved, exc)
        return Reply(text=f"Couldn't open {app}: {exc}", success=False)

    return Reply(text=f"Opening {app}.", success=True)
