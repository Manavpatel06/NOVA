"""Connected apps (ADR 0030): NOVA uses other apps the way Claude does, through
MCP servers ("connectors") running on the hub.

Manav, 2026-11-25: "I want to be able to connect my Nova to any app as I do
with Claude ... multiple Gmail accounts ... Canvas ... Calendar".

  * data/connectors.json (0600, written only by `nova-hub connect`, secrets
    typed by Manav) lists the connectors: the command, its settings, a note
    for NOVA ("personal: x@gmail.com, school: y@asu.edu"), and which tools
    are never offered (`deny`) or the only ones offered (`only`).
  * Each connector runs as a child process in a bubblewrap sandbox: the OS
    read-only, an empty home except its own folder under
    data/connectors/<name>/ -- it can't see NOVA's memory, keys, Claude's
    login or Manav's files. It does use the network itself (Google's or
    Canvas's API): that traffic is the connector's, outside NOVA's egress
    gate, like Claude Code's when building (ADR 0027).
  * Every tool becomes a skill named <connector>_<tool>, tiered from the
    tool's own labels: read-only -> READ, explicitly non-destructive -> ACT,
    anything else -> CONFIRM ("Nova, confirm"), the MCP default.
  * Everything a connector returns is outside content (an email body, a
    course announcement): untrusted_output, so any action after it in the
    same conversation needs approval -- rule 2 of the constitution.
  * Gmail stays drafts-only: the Google preset asks Google for draft
    permission only and also denies the send, forward and filter tools.
"""
from __future__ import annotations

import fnmatch
import inspect
import json
import keyword
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("hub.connectors")

NAME_RE = re.compile(r"[a-z][a-z0-9]{1,19}")
MAX_RESULT = 6000
MAX_DESC = 300
MAX_SCHEMA = 4000
RESTART_GAP_S = 60.0

#: tools never offered, whatever the connector says (drafts only: CLAUDE.md / Manav)
GOOGLE_DENY = ("send_gmail_message", "manage_gmail_filter", "*forward*", "*send*",
               "start_google_auth")  # signing in is `nova-hub connect signin`, at the hub
PRESETS: dict[str, dict[str, Any]] = {
    "google": {
        "command": ["uvx", "workspace-mcp", "--permissions", "gmail:drafts", "calendar:full"],
        "deny": list(GOOGLE_DENY),
    },
    "canvas": {
        "command": ["uvx", "--from", "canvas-mcp", "canvas-mcp-server", "--role", "student"],
        "deny": ["send_conversation", "*delete*"],
    },
}


def _venv_bin() -> Path:
    from nova import ROOT

    return Path(ROOT) / ".venv" / "bin"


# --- the settings file -------------------------------------------------------------

def load(path: Path) -> dict[str, dict[str, Any]]:
    """The connectors, or {} (missing file, bad JSON, or readable by others:
    it holds secrets, so a too-open file is refused, never used)."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return {}
    if os.name == "posix" and st.st_mode & 0o077:
        _log.error("%s is readable by other users; fix with: chmod 600 %s (connectors are off until then)",
                   path, path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log.error("Couldn't read %s (%s)", path, type(exc).__name__)
        return {}
    out = {}
    for name, entry in (data.get("connectors") or {}).items() if isinstance(data, dict) else []:
        if not (isinstance(name, str) and NAME_RE.fullmatch(name) and isinstance(entry, dict)):
            continue
        cmd = entry.get("command")
        if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd)):
            continue
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        out[name] = {
            "command": cmd,
            "env": {str(k): str(v) for k, v in env.items()},
            "note": str(entry.get("note") or "")[:500],
            "deny": [str(x).lower() for x in entry.get("deny") or []],
            "only": [str(x).lower() for x in entry.get("only") or []],
            "enabled": entry.get("enabled", True) is not False,
            "sandbox": entry.get("sandbox", True) is not False,
            # several accounts on one connector: NOVA picks by label ("personal"), the address
            # itself never goes to Claude (emails are redacted from everything sent there)
            "accounts": {str(k).lower(): str(v) for k, v in (entry.get("accounts") or {}).items()
                         if NAME_RE.fullmatch(str(k).lower())} if isinstance(entry.get("accounts"), dict) else {},
            "account_param": str(entry.get("account_param") or ""),
        }
    return out


def save(path: Path, connectors: dict[str, dict[str, Any]]) -> None:
    """Write the file 0600, atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"connectors": connectors}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- one tool -> one skill -----------------------------------------------------------

def tier_for(tool: dict[str, Any]) -> Any:
    from nova.security.tiers import Tier

    ann = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    if ann.get("readOnlyHint") is True:
        return Tier.READ
    if ann.get("destructiveHint") is False:
        return Tier.ACT
    return Tier.CONFIRM   # MCP's default for a tool that may change things: assume it's destructive


def offered(tool_name: str, entry: dict[str, Any]) -> bool:
    name = tool_name.lower()
    if any(fnmatch.fnmatchcase(name, pat) for pat in entry.get("deny", [])):
        return False
    only = entry.get("only") or []
    return not only or any(fnmatch.fnmatchcase(name, pat) for pat in only)


def skill_name(connector: str, tool: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", f"{connector}_{tool}".lower()).strip("_")[:64]


def _clean_schema(schema: Any) -> dict[str, Any] | None:
    """The tool's parameters, trimmed to what the brain needs. None if a
    parameter name can't be a Python keyword argument."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    props_in = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    props: dict[str, Any] = {}
    for pname, p in props_in.items():
        if not (isinstance(pname, str) and pname.isidentifier() and not keyword.iskeyword(pname)
                and pname != "ctx"):
            return None
        p = p if isinstance(p, dict) else {}
        kept: dict[str, Any] = {}
        t = p.get("type")
        if t is None and isinstance(p.get("anyOf"), list):  # Optional[X] comes as anyOf [X, null]
            ts = [x.get("type") for x in p["anyOf"] if isinstance(x, dict) and x.get("type") != "null"]
            t = ts[0] if ts else None
        if isinstance(t, str):
            kept["type"] = t
        if isinstance(p.get("description"), str):
            kept["description"] = p["description"][:200]
        if isinstance(p.get("enum"), list):
            kept["enum"] = p["enum"][:30]
        if kept.get("type") == "array" and isinstance(p.get("items"), dict) and isinstance(p["items"].get("type"), str):
            kept["items"] = {"type": p["items"]["type"]}
        props[pname] = kept
    required = [r for r in schema.get("required") or [] if r in props]
    out = {"type": "object", "properties": props, "required": required}
    if len(json.dumps(out)) > MAX_SCHEMA:
        out["properties"] = {k: {kk: vv for kk, vv in v.items() if kk != "description"} for k, v in props.items()}
    return out


def _coerce(value: Any, prop: dict[str, Any]) -> Any:
    """The Max brain passes every parameter as text; give the tool its type."""
    if not isinstance(value, str):
        return value
    t = prop.get("type")
    try:
        if t == "integer":
            return int(value.strip())
        if t == "number":
            return float(value.strip())
        if t == "boolean":
            return value.strip().lower() in ("true", "yes", "1")
        if t in ("array", "object"):
            parsed = json.loads(value)
            if t == "array" and not isinstance(parsed, list):
                return [parsed]
            return parsed
    except (ValueError, TypeError):
        if t == "array":
            return [x.strip() for x in value.split(",") if x.strip()]
    return value


def make_skill(connectors: "Connectors", cname: str, entry: dict[str, Any], tool: dict[str, Any]) -> Any:
    from nova.skills.registry import Reply, SkillSpec

    schema = _clean_schema(tool.get("inputSchema"))
    if schema is None:
        return None
    tname = tool["name"]
    props = schema["properties"]
    accounts: dict[str, str] = entry.get("accounts") or {}
    acct = entry.get("account_param") or ""
    if acct in props and accounts:
        # NOVA chooses an account by its label; with one account, it needn't choose
        props[acct] = {"type": "string", "enum": sorted(accounts),
                       "description": "which of Manav's accounts: " + ", ".join(sorted(accounts))}
        if len(accounts) == 1 and acct in schema["required"]:
            schema["required"].remove(acct)
    required = set(schema["required"])

    async def handler(ctx, **params):
        import asyncio

        args = {k: _coerce(v, props.get(k, {})) for k, v in params.items() if v is not None}
        if acct in props and accounts:
            label = str(args.get(acct) or (next(iter(accounts)) if len(accounts) == 1 else "")).strip().lower()
            if label not in accounts:
                return Reply(text=f"Which account? I know {', '.join(sorted(accounts))}.", success=False)
            args[acct] = accounts[label]
        try:
            text, is_error = await asyncio.to_thread(connectors.call, cname, tname, args)
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s.%s failed (%s)", cname, tname, type(exc).__name__)
            return Reply(text=f"{cname.capitalize()} didn't answer ({exc}).", success=False)
        text = text.strip()
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + " ...(cut)"
        return Reply(text=text or ("That didn't work." if is_error else "Done."), success=not is_error)

    params = [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    for pname in props:
        params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                        default=inspect.Parameter.empty if pname in required else None))
    handler.__signature__ = inspect.Signature(params)
    desc = f"[{cname}] " + " ".join(str(tool.get("description") or tool.get("title") or tname).split())[:MAX_DESC]
    words = tname.replace("_", " ")
    return SkillSpec(
        name=skill_name(cname, tname), tier=tier_for(tool), examples=(f"{words} ({cname})",),
        description=desc, trusts_input=False, handler=handler, untrusted_output=True,
        input_schema=schema,
    )


# --- all connectors ------------------------------------------------------------------

class Connectors:
    def __init__(self, path: Path, state_root: Path, *, client_factory: Callable[..., Any] | None = None,
                 builtin: Callable[[], set[str]] | None = None) -> None:
        from nova.hub.mcp_client import McpClient

        self.path = Path(path)
        self.state_root = Path(state_root)
        self._factory = client_factory or McpClient
        self._builtin = builtin or (lambda: set())
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, Any] = {}
        self._skills: dict[str, dict[str, Any]] = {}
        self._state: dict[str, dict[str, Any]] = {}
        self._last_start: dict[str, float] = {}

    def start(self, wait: bool = False) -> None:
        """Start every enabled connector (in the background unless `wait`)."""
        self._entries = load(self.path)
        threads = []
        for name, entry in self._entries.items():
            if not entry["enabled"]:
                self._state[name] = {"state": "off", "tools": 0}
                continue
            self._state[name] = {"state": "starting", "tools": 0}
            t = threading.Thread(target=self._start_one, args=(name,), name=f"connector-{name}", daemon=True)
            t.start()
            threads.append(t)
        if wait:
            for t in threads:
                t.join()

    def _argv(self, name: str, entry: dict[str, Any]) -> tuple[list[str], dict[str, str], str]:
        state = self.state_root / name
        state.mkdir(parents=True, exist_ok=True)
        venv = _venv_bin()
        cmd = list(entry["command"])
        exe = cmd[0]
        if "/" not in exe:
            local = venv / exe
            exe = str(local) if local.exists() else (shutil.which(exe) or exe)
        cmd[0] = exe
        env = {
            "PATH": f"{venv}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(state), "LANG": "C.UTF-8",
            "UV_CACHE_DIR": str(state / ".cache" / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(state / ".python"),
            "UV_TOOL_DIR": str(state / ".tools"),
            "UV_PYTHON": "3.12",
            "XDG_CONFIG_HOME": str(state / ".config"), "XDG_DATA_HOME": str(state / ".local" / "share"),
            "XDG_CACHE_HOME": str(state / ".cache"),
            **entry["env"],
        }
        if not entry["sandbox"]:
            return cmd, env, str(state)
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise RuntimeError("bubblewrap isn't installed (sudo apt install bubblewrap)")
        from nova.hub.builder import sandbox_root

        argv = [bwrap, *sandbox_root(), "--unshare-all", "--share-net"]
        if Path("/run/systemd/resolve").is_dir():
            argv += ["--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve"]  # DNS
        if venv.is_dir():
            argv += ["--ro-bind", str(venv), str(venv)]
        exe_dir = Path(exe).resolve().parent
        if exe_dir.is_dir() and not exe_dir.is_relative_to(Path("/usr")):
            argv += ["--ro-bind-try", str(exe_dir), str(exe_dir)]
        argv += ["--bind", str(state), str(state), "--chdir", str(state), "--", *cmd]
        return argv, env, str(state)

    def _start_one(self, name: str) -> None:
        entry = self._entries[name]
        self._last_start[name] = time.monotonic()
        try:
            argv, env, cwd = self._argv(name, entry)
            client = self._factory(argv, env=env, cwd=cwd, name=name)
            client.start()
            tools = client.list_tools()
        except Exception as exc:  # noqa: BLE001
            why = str(exc)[:200]
            tail = ""
            try:
                tail = client.last_errors().splitlines()[-1][:200]  # type: ignore[possibly-undefined]
                client.close()
            except Exception:  # noqa: BLE001
                pass
            _log.error("Connector %s didn't start: %s %s", name, why, tail)
            self._state[name] = {"state": "failed", "tools": 0, "error": why}
            return
        skills = self._build(name, entry, tools)
        with self._lock:
            old = self._clients.get(name)
            self._clients[name] = client
            self._skills[name] = skills
        if old is not None and old is not client:
            old.close()
        hidden = len(tools) - len(skills)
        self._state[name] = {"state": "ready", "tools": len(skills), "hidden": hidden}
        _log.info("Connector %s ready: %d tools (%d not offered)", name, len(skills), hidden)

    def _build(self, name: str, entry: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        builtin = self._builtin()
        out: dict[str, Any] = {}
        for tool in tools:
            if not offered(tool["name"], entry):
                continue
            spec = make_skill(self, name, entry, tool)
            if spec is None or spec.name in builtin or spec.name in out:
                continue
            out[spec.name] = spec
        return out

    # --- used by the assistant --------------------------------------------------------

    def skills(self) -> dict[str, Any]:
        with self._lock:
            merged: dict[str, Any] = {}
            for name in sorted(self._skills):
                merged.update(self._skills[name])
            return merged

    def call(self, name: str, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
        with self._lock:
            client = self._clients.get(name)
        if client is None or not client.alive():
            if time.monotonic() - self._last_start.get(name, 0.0) < RESTART_GAP_S:
                raise RuntimeError("it stopped and is restarting")
            _log.warning("Connector %s stopped; restarting it", name)
            self._start_one(name)
            with self._lock:
                client = self._clients.get(name)
            if client is None or not client.alive():
                raise RuntimeError("it isn't running")
        return client.call_tool(tool, args)

    def notes(self) -> dict[str, str]:
        """What each ready connector is, for NOVA's context line (Manav wrote these)."""
        return {n: e["note"] for n, e in self._entries.items()
                if e.get("note") and self._state.get(n, {}).get("state") == "ready"}

    def status(self) -> list[dict[str, Any]]:
        return [{"name": n, **s} for n, s in sorted(self._state.items())]

    def close(self) -> None:
        with self._lock:
            clients, self._clients = list(self._clients.values()), {}
            self._skills = {}
        for c in clients:
            c.close()


# --- `nova-hub connect` ---------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    got = input(f"   {prompt}{f' [{default}]' if default else ''}: ").strip()
    return got or default


def _secret(prompt: str) -> str:
    import getpass

    return getpass.getpass(f"   {prompt} (typing is hidden): ").strip()


def _paths() -> tuple[Path, Path]:
    from nova import DATA_DIR

    return DATA_DIR / "connectors.json", DATA_DIR / "connectors"


def cli(argv: list[str]) -> int:
    path, state_root = _paths()
    cmd = argv[0] if argv else "list"
    entries = load(path)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8")).get("connectors") or {}
        except (OSError, ValueError):
            raw = {}

    if cmd == "list":
        if not entries:
            print("   No apps connected yet. Try: nova-hub connect google | canvas | add NAME -- COMMAND...")
        for name, e in entries.items():
            print(f"   {name:10s} {'on ' if e['enabled'] else 'off'}  {' '.join(e['command'][:4])}"
                  f"{'  -- ' + e['note'] if e['note'] else ''}")
        return 0

    if cmd == "google":
        print("   Gmail (drafts only -- NOVA never sends) + Google Calendar, for every account you sign in.")
        print("   You need a Google OAuth client (Desktop app) with the Gmail and Calendar APIs on:")
        print("   https://console.cloud.google.com/apis/credentials  (add yourself as a test user)")
        cid = _ask("OAuth client ID")
        secret = _secret("OAuth client secret")
        typed = _ask("Your accounts, like  personal=you@gmail.com, school=you@asu.edu")
        accounts = {}
        for part in typed.split(","):
            label, _, email = part.partition("=")
            label, email = label.strip().lower(), email.strip()
            if NAME_RE.fullmatch(label) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email, re.I):
                accounts[label] = email
        if not cid or not secret or not accounts:
            print("   Nothing saved (need the client ID, secret and at least one label=email).")
            return 1
        raw["google"] = {**PRESETS["google"], "env": {
            "GOOGLE_OAUTH_CLIENT_ID": cid, "GOOGLE_OAUTH_CLIENT_SECRET": secret,
            "OAUTHLIB_INSECURE_TRANSPORT": "1",   # the sign-in comes back to localhost on the hub
            "WORKSPACE_MCP_CREDENTIALS_DIR": str(state_root / "google" / "credentials"),
            "WORKSPACE_MCP_DISABLE_LOCAL_FILES": "true",
        }, "accounts": accounts, "account_param": "user_google_email",
            "note": (f"Gmail (drafts only) and Calendar for Manav's Google accounts: {', '.join(accounts)}. "
                     "If a tool says an account needs signing in, tell him to run nova-hub connect signin "
                     "on the hub; never read out a link")}
        save(path, raw)
        print("   Saved. Now sign in each account:  " + "   ".join(f"nova-hub connect signin {k}" for k in accounts))
        return 0

    if cmd == "canvas":
        url = _ask("Canvas address", "https://canvas.asu.edu")
        print("   Make a token in Canvas: Account -> Settings -> New Access Token")
        token = _secret("Canvas token")
        if not token:
            print("   Nothing saved.")
            return 1
        raw["canvas"] = {**PRESETS["canvas"], "env": {"CANVAS_API_URL": url, "CANVAS_API_TOKEN": token},
                         "note": "Manav's courses, assignments, due dates and grades (read-only)"}
        save(path, raw)
        print("   Saved. NOVA reads your courses; it can't submit or message anyone.")
        return 0

    if cmd == "add" and len(argv) >= 4 and argv[2] == "--":
        name = argv[1].lower()
        if not NAME_RE.fullmatch(name):
            print("   Name: 2-20 lowercase letters or digits, starting with a letter.")
            return 1
        env: dict[str, str] = {}
        while True:
            key = _ask("A setting it needs (like API_TOKEN), or Enter when done")
            if not key:
                break
            env[key] = _secret(f"Value for {key}")
        note = _ask("What is it, for NOVA (one line)")
        raw[name] = {"command": argv[3:], "env": env, "note": note, "deny": []}
        save(path, raw)
        print(f"   Saved {name}. Anything that may change things will need \"Nova, confirm\".")
        return 0

    if cmd == "remove" and len(argv) == 2:
        if raw.pop(argv[1], None) is None:
            print("   No such connector.")
            return 1
        save(path, raw)
        print(f"   Removed {argv[1]} (its sign-ins stay in {state_root / argv[1]} until you delete that folder).")
        return 0

    if cmd in ("on", "off") and len(argv) == 2 and argv[1] in raw:
        raw[argv[1]]["enabled"] = cmd == "on"
        save(path, raw)
        return 0

    if cmd == "test" and len(argv) == 2 and argv[1] in entries:
        c = Connectors(path, state_root)
        c._entries = entries
        c._start_one(argv[1])
        st = c._state.get(argv[1], {})
        print(f"   {argv[1]}: {st.get('state')} -- {st.get('tools', 0)} tools offered"
              f"{', ' + str(st.get('hidden')) + ' kept away from NOVA' if st.get('hidden') else ''}"
              f"{' -- ' + st['error'] if st.get('error') else ''}")
        c.close()
        return 0 if st.get("state") == "ready" else 1

    if cmd == "signin" and len(argv) == 2 and "google" in entries:
        email = entries["google"]["accounts"].get(argv[1].lower(), argv[1])
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email, re.I):
            print("   That doesn't look like an email address.")
            return 1
        c = Connectors(path, state_root)
        c._entries = entries
        c._start_one("google")
        if c._state.get("google", {}).get("state") != "ready":
            print(f"   Google connector didn't start: {c._state.get('google', {}).get('error')}")
            return 1
        try:
            text, _ = c.call("google", "start_google_auth", {"service_name": "Gmail", "user_google_email": email})
        except Exception as exc:  # noqa: BLE001
            print(f"   Couldn't start the sign-in: {exc}")
            c.close()
            return 1
        m = re.search(r"https://accounts\.google\.com/\S+", text)
        if m:
            url = m.group(0).rstrip(").,'\"")
            print(f"   Sign in as {email}: open this link in the hub's browser (Ctrl+click it here):\n\n   {url}\n")
        else:
            print("   " + text[:600].replace("\n", "\n   "))
        input("   Press Enter after the browser says you're signed in... ")
        c.close()
        return 0

    print("usage: nova-hub connect [list | google | canvas | signin EMAIL | add NAME -- COMMAND... |"
          " test NAME | on NAME | off NAME | remove NAME]")
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(cli(sys.argv[1:]))
