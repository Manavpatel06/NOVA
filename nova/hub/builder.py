"""NOVA builds things with Claude Code on the Max plan (ADR 0027).

Manav (2026-11-25): talking uses the API credit; "when I ask Nova to build
something ... it should use the Max plan along with all the skills on my
plan". So `build_project` hands the request to Claude Code -- the `claude`
program signed in to his Max plan, with his skills, plugins and MCP servers --
running headless on the hub, in a project folder he can open from his laptop
(~/Shared/Projects/<name> = H:\\Projects\\<name>).

It needs real tools (shell, installs, running the code), so it runs with
permission prompts off -- but inside bubblewrap, where it sees:
  * the OS read-only, a throwaway /tmp and home, the network;
  * its own project folder, read-write;
  * Claude Code's sign-in, so it can use the Max plan -- with everything in
    there that could later run code OUTSIDE the sandbox held read-only
    (settings, hooks, CLAUDE.md, commands, agents, skills, plugins) and
    ~/.claude.json given as a throwaway copy (it lists MCP servers);
and nothing else of the hub: not ~/NOVA (keys, phone settings, memory), not
the GitHub sign-in, not Tailscale, D-Bus or other projects.

One build at a time, an hour at most; when it ends, Manav's phone gets a
fixed-text ping and NOVA can tell him how it went.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("hub.builder")

BUILD_TIMEOUT_S = 60 * 60
MAX_TURNS = 200
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
#: inside ~/.claude: what could run code later, outside this sandbox
PROTECTED = ("settings.json", "settings.local.json", "CLAUDE.md", "commands", "agents", "skills",
             "plugins", "hooks", "output-styles", "workflows")

PROMPT = """You are building software for Manav, on his request to NOVA (his voice assistant). Work in this folder
(it is the project; nothing outside it is yours). Build it completely: real code, set up and run it, test it,
fix what fails. Write README.md saying what it is and exactly how to run it on Windows (he'll open the
folder from his laptop as H:\\Projects\\{slug}). Use any of your skills that help.

Request: {request}

When done, end your reply with ONE short paragraph, two sentences at most, that NOVA will read aloud:
what you built and how to open it. No markdown in that paragraph."""


@dataclass
class Build:
    id: str
    request: str
    slug: str
    status: str = "building"   # building done failed
    started: float = 0.0
    finished: float = 0.0
    summary: str = ""


def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    stop = {"a", "an", "the", "me", "my", "build", "make", "create", "app", "for", "that", "which", "to", "please"}
    words = [w for w in words if w not in stop][:5] or ["project"]
    slug = "-".join(words)[:40].strip("-")
    return slug if SLUG_RE.fullmatch(slug) else "project"


def last_paragraph(text: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    tail = paras[-1] if paras else ""
    tail = re.sub(r"[*_`#>]+", "", tail)
    return " ".join(tail.split())[:400]


class Builder:
    def __init__(
        self,
        *,
        projects: Path,
        state: Path,
        notify: Callable[[str], Any] | None = None,
        runner: Callable[[list[str], Path, float, dict[str, str] | None], tuple[int, str]] | None = None,
        egress: Any = None,
        sandbox: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from nova.hub.forge import run

        self.projects = Path(projects)
        self._state = Path(state)
        self._notify = notify
        self._run = runner or run
        self._egress = egress
        self._sandbox = sandbox
        self._clock = clock
        self._lock = threading.Lock()

    # --- state ------------------------------------------------------------------

    def _load(self) -> list[Build]:
        try:
            raw = json.loads(self._state.read_text(encoding="utf-8"))
            return [Build(**b) for b in raw if isinstance(b, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self, builds: list[Build]) -> None:
        self._state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(b) for b in builds[-30:]]), encoding="utf-8")
        tmp.replace(self._state)

    def _update(self, bid: str, **fields: Any) -> None:
        with self._lock:
            builds = self._load()
            for b in builds:
                if b.id == bid:
                    for k, v in fields.items():
                        setattr(b, k, v)
            self._save(builds)

    def recover(self) -> None:
        """A build cut short by a restart is marked failed, not left 'building'."""
        with self._lock:
            builds = self._load()
            for b in builds:
                if b.status == "building":
                    b.status, b.summary, b.finished = "failed", "The hub restarted during the build.", self._clock()
            self._save(builds)

    def list(self) -> list[dict[str, Any]]:
        return [asdict(b) for b in reversed(self._load())][:10]

    # --- start ------------------------------------------------------------------

    def start(self, request: str, name: str = "") -> dict[str, Any]:
        from nova.hub.forge import clean_request

        request = clean_request(request)
        slug = slugify(name or request)
        with self._lock:
            builds = self._load()
            if any(b.status == "building" for b in builds):
                raise ValueError("I'm already building something; one at a time")
            folder = self.projects / slug
            build = Build(id=secrets.token_hex(4), request=request, slug=slug, started=self._clock())
            builds.append(build)
            self._save(builds)
        self.projects.mkdir(parents=True, exist_ok=True)
        folder.mkdir(exist_ok=True)
        if not folder.resolve().is_relative_to(self.projects.resolve()) or folder.is_symlink():
            self._update(build.id, status="failed", summary="That project folder isn't safe to use.")
            raise ValueError("unsafe project folder")
        threading.Thread(target=self._work, args=(build, folder), name="builder", daemon=True).start()
        return {"ok": True, "id": build.id, "project": slug, "continuing": any(folder.iterdir())}

    def _work(self, build: Build, folder: Path) -> None:
        prompt = PROMPT.format(slug=build.slug, request=build.request)
        if self._egress is not None:
            prompt = self._egress.release_for_process("builder", prompt)
        try:
            code, out = self._claude(folder, prompt)
        except Exception as exc:  # noqa: BLE001
            code, out = 1, f"couldn't start Claude Code ({type(exc).__name__})"
        from nova.hub.forge import _result_text

        result = _result_text(out)
        if code == 0 and result:
            self._update(build.id, status="done", finished=self._clock(),
                         summary=last_paragraph(result) or "It's built.")
            _log.info("build %s done", build.id)
            self._say(f"NOVA finished building {build.slug}. Ask NOVA how it went.")
        else:
            why = "it ran out of time" if code == 124 else "Claude Code stopped with an error"
            self._update(build.id, status="failed", finished=self._clock(), summary=f"The build failed: {why}.")
            _log.warning("build %s failed (exit %s)", build.id, code)
            self._say(f"NOVA's build of {build.slug} didn't finish.")

    def _say(self, text: str) -> None:
        if self._notify is not None:
            try:
                self._notify(text)
            except Exception:  # noqa: BLE001
                pass

    # --- Claude Code, sandboxed ---------------------------------------------------

    def _claude(self, folder: Path, prompt: str) -> tuple[int, str]:
        from nova.brain.claude_arm import _child_env

        exe = shutil.which("claude")
        if exe is None:
            return 127, "claude is not installed"
        env = _child_env()  # the Max plan, never an API key
        cmd = [exe, "-p", prompt, "--output-format", "json", "--permission-mode", "bypassPermissions",
               "--max-turns", str(MAX_TURNS)]
        if not self._sandbox:
            return self._run(cmd, folder, BUILD_TIMEOUT_S, env)
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            return 127, "bubblewrap is not installed"
        home = Path.home()
        with tempfile.TemporaryDirectory(prefix="nova-build-") as tmp:
            state_copy = Path(tmp) / "claude.json"
            try:
                shutil.copyfile(home / ".claude.json", state_copy)
            except OSError:
                state_copy.write_text("{}", encoding="utf-8")
            argv = [bwrap, *sandbox_root(), "--unshare-all", "--share-net"]
            if Path("/run/systemd/resolve").is_dir():
                argv += ["--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve"]  # DNS
            argv += ["--bind-try", str(home / ".claude"), str(home / ".claude")]
            for name in PROTECTED:
                argv += ["--ro-bind-try", str(home / ".claude" / name), str(home / ".claude" / name)]
            argv += ["--bind", str(state_copy), str(home / ".claude.json"),
                     "--ro-bind-try", str(home / ".local/share/claude"), str(home / ".local/share/claude"),
                     "--ro-bind-try", str(Path(exe).parent), str(Path(exe).parent),
                     "--ro-bind-try", str(Path(exe).resolve().parent), str(Path(exe).resolve().parent),
                     "--bind", str(folder), str(folder), "--chdir", str(folder),
                     "--setenv", "HOME", str(home), *cmd]
            return self._run(argv, folder, BUILD_TIMEOUT_S, env)


def sandbox_root() -> list[str]:
    """The OS read-only and nothing of the hub user's (same root as the forge)."""
    args = ["--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for d in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
        p = Path(d)
        if p.is_symlink():
            args += ["--symlink", str(p.readlink()), d]
        elif p.is_dir():
            args += ["--ro-bind", d, d]
    return args + ["--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/home", "--tmpfs", "/root",
                   "--dev", "/dev", "--proc", "/proc", "--die-with-parent", "--new-session"]
