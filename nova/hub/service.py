"""What nova-hub does for each request (2026-11-22, docs/decisions/0019).

Plain methods with no network code -- nova.hub.listener owns the socket and
calls these, so everything here is testable directly.

Who may ask: `tailscale serve` publishes the hub's 127.0.0.1 port to Manav's
tailnet over HTTPS and adds a Tailscale-User-Login header naming the person
whose device sent the request. Only the owner recorded by `nova-hub login`
(data/hub_owner) gets /v1/route and /v1/memory; with no owner file nobody
does (fail closed). /v1/health is open to the hub itself and the tailnet.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from nova.hub.local_model import MAX_TEXT, MAX_TOOLS
from nova.log import get_logger

_log = get_logger("hub.service")

#: memory files the laptop may mirror here: flat or one folder deep
#: (journal/2026-11-22.md), plain text types only.
_MEMORY_REL = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}(/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80})?$")
_MEMORY_SUFFIXES = (".json", ".md", ".txt")
MAX_MEMORY_BYTES = 5_000_000
MAX_MEMORY_FILES = 2000


class HubError(ValueError):
    """A bad request: the listener answers 400 with this message."""


class HubService:
    def __init__(
        self,
        *,
        model: Any,
        data_dir: Path,
        owner_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._model = model
        self._data_dir = Path(data_dir)
        self._owner_path = Path(owner_path) if owner_path is not None else self._data_dir / "hub_owner"
        self._clock = clock
        self._started = clock()
        self._model_lock = threading.Lock()
        self._state_path = self._data_dir / "hub_state.json"

    # --- who --------------------------------------------------------------

    def owner(self) -> str:
        try:
            return self._owner_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return ""

    def is_owner(self, login: str | None) -> bool:
        owner = self.owner()
        return bool(owner) and bool(login) and login.strip().lower() == owner

    def hub_name(self) -> str:
        try:
            return (self._data_dir / "hub_name").read_text(encoding="utf-8").strip().lower().rstrip(".")
        except OSError:
            return ""

    def host_ok(self, host: str | None, port: int) -> bool:
        """The request was addressed to this hub, not to some other name
        that resolves to it (DNS rebinding from a web page open on the hub):
        its Tailscale name (as `tailscale serve` forwards it) or the
        loopback address itself."""
        h = (host or "").strip().lower().rstrip(".")
        allowed = {f"127.0.0.1:{port}"}
        name = self.hub_name()
        if name:
            allowed |= {name, f"{name}:443"}
        return h in allowed

    # --- /v1/health -------------------------------------------------------

    def health(self, *, full: bool = True) -> dict[str, Any]:
        """Everything for the owner (and the hub itself); just ok/not-ok for
        any other device on the tailnet."""
        if not full:
            return {"ok": True}
        out: dict[str, Any] = {
            "ok": True,
            "uptime_s": int(self._clock() - self._started),
            "model": self._model.model,
            "owner_set": bool(self.owner()),
        }
        try:
            installed = self._model.installed()
            loaded = self._model.loaded()
            out["ollama"] = True
            out["model_installed"] = any(n == self._model.model or n.startswith(self._model.model + ":") for n in installed)
            out["model_loaded"] = any(n == self._model.model or n.startswith(self._model.model + ":") for n in loaded)
        except Exception as exc:  # noqa: BLE001
            out.update(ok=False, ollama=False, model_installed=False, model_loaded=False, ollama_error=str(exc)[:120])
        if not out.get("model_installed"):
            out["ok"] = False
        state = self._read_state()
        out["memory_files"] = state.get("memory_files", 0)
        out["memory_synced_at"] = state.get("memory_synced_at", "")
        out["memory_free_mb"] = _mem_available_mb()
        try:
            out["load_1m"] = round(os.getloadavg()[0], 2)
        except (AttributeError, OSError):
            pass
        return out

    # --- /v1/route ----------------------------------------------------------

    def route(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise HubError("body must be a JSON object")
        text = body.get("text")
        tools = body.get("tools")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise HubError(f"text must be 1..{MAX_TEXT} characters")
        if not isinstance(tools, list) or len(tools) > MAX_TOOLS or not all(isinstance(t, dict) for t in tools):
            raise HubError(f"tools must be a list of at most {MAX_TOOLS} objects")
        with self._model_lock:  # one model, one sentence at a time
            decision = self._model.route(text, tools)
        _log.info("route -> %s%s (%s ms)", decision.get("route"),
                  f" {decision.get('tool')}" if decision.get("route") == "tool" else "", decision.get("ms", "?"))
        return decision

    # --- /v1/memory -----------------------------------------------------------

    def mirror_dir(self) -> Path:
        return self._data_dir / "memory-mirror"

    def put_memory(self, body: Any) -> dict[str, Any]:
        """Mirror the laptop's memory folder here (additive: files the laptop
        sends replace their copies; nothing else is deleted). Every
        destination is checked by realpath at write time (CLAUDE.md rule 3)."""
        if not isinstance(body, dict) or not isinstance(body.get("files"), dict):
            raise HubError('body must be {"files": {"relative/path.md": "text", ...}}')
        files: dict[str, Any] = body["files"]
        if len(files) > MAX_MEMORY_FILES:
            raise HubError(f"at most {MAX_MEMORY_FILES} files")
        total = 0
        for rel, text in files.items():
            if not isinstance(rel, str) or not _MEMORY_REL.match(rel) or ".." in rel or not rel.endswith(_MEMORY_SUFFIXES):
                raise HubError(f"refused memory path {rel!r}")
            if not isinstance(text, str):
                raise HubError(f"{rel}: content must be text")
            total += len(text.encode("utf-8"))
        if total > MAX_MEMORY_BYTES:
            raise HubError("memory upload too large")

        root = self.mirror_dir()
        root.mkdir(parents=True, exist_ok=True)
        real_root = os.path.realpath(root)
        removed = 0
        if body.get("complete") is True:
            # the laptop sent its whole memory: anything else here was
            # forgotten there (backups keep it for their 30 days, no longer)
            keep = {rel for rel in files}
            for path in sorted(root.rglob("*"), reverse=True):
                rel = path.relative_to(root).as_posix()
                if path.is_symlink() or (path.is_file() and rel not in keep):
                    path.unlink()
                    removed += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
        written = 0
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            real_parent = os.path.realpath(target.parent)
            if os.path.commonpath([real_root, real_parent]) != real_root or (
                target.exists() and os.path.realpath(target) != os.path.join(real_parent, target.name)
            ):
                raise HubError(f"refused memory path {rel!r} (leaves the mirror)")
            tmp = target.with_name(target.name + ".tmp")
            tmp.unlink(missing_ok=True)  # never write through a planted link
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
            written += 1
        count = sum(1 for p in root.rglob("*") if p.is_file())
        self._write_state({"memory_files": count,
                           "memory_synced_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._clock()))})
        _log.info("memory mirrored: %d written, %d removed, %d in the mirror", written, removed, count)
        return {"ok": True, "written": written, "removed": removed, "files": count}

    # --- small state file ---------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, update: dict[str, Any]) -> None:
        state = self._read_state()
        state.update(update)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name("hub_state.json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_path)


def _mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None
