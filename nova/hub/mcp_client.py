"""A small Model Context Protocol client (ADR 0030): one connector process,
spoken to over its stdin/stdout with newline-delimited JSON-RPC 2.0.

Only what NOVA needs: initialize, tools/list (with paging) and tools/call.
Requests from the server are answered politely: ping gets {}, anything else
"method not found" -- NOVA offers the server no sampling, roots or other
capabilities. No network here: the connector is a child process on pipes.
"""
from __future__ import annotations

import itertools
import json
import subprocess
import threading
from collections import deque
from typing import Any

from nova.log import get_logger

_log = get_logger("hub.mcp")

PROTOCOL = "2025-06-18"
MAX_LINE = 8 * 1024 * 1024     # one message; a bigger one ends the connection
START_TIMEOUT_S = 120.0        # first start may download the connector (uvx)
CALL_TIMEOUT_S = 60.0


class McpError(Exception):
    pass


class McpClient:
    def __init__(self, argv: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None,
                 name: str = "connector") -> None:
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.name = name
        self.server_info: dict[str, Any] = {}
        self.instructions = ""
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=40)   # the last lines, for "why did it fail"
        self._eof = False   # its stdout closed: gone, even if the OS hasn't reaped it yet

    # --- life ------------------------------------------------------------------

    def start(self, timeout: float = START_TIMEOUT_S) -> None:
        argv = list(self.argv)
        self._eof = False
        self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=self.env, cwd=self.cwd, bufsize=0, shell=False)
        threading.Thread(target=self._read, name=f"mcp-{self.name}", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name=f"mcp-{self.name}-err", daemon=True).start()
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "nova", "version": "1"},
        }, timeout=timeout)
        self.server_info = result.get("serverInfo") or {} if isinstance(result, dict) else {}
        instructions = result.get("instructions") if isinstance(result, dict) else None
        self.instructions = instructions if isinstance(instructions, str) else ""
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def alive(self) -> bool:
        return self._proc is not None and not self._eof and self._proc.poll() is None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._fail_all("the connector stopped")

    def last_errors(self) -> str:
        return "\n".join(self._stderr)

    # --- calls -------------------------------------------------------------------

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = CALL_TIMEOUT_S) -> Any:
        if not self.alive():
            raise McpError(f"{self.name} isn't running")
        rid = next(self._ids)
        slot: dict[str, Any] = {"event": threading.Event()}
        with self._lock:
            self._pending[rid] = slot
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._send(msg)
            if not slot["event"].wait(timeout):
                raise McpError(f"{self.name} didn't answer {method} in {int(timeout)} s")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if "error" in slot:
            err = slot["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            raise McpError(f"{self.name}: {str(text)[:300]}")
        return slot.get("result")

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(50):   # paging, bounded
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools") if isinstance(result, dict) else None
            tools += [t for t in page or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = CALL_TIMEOUT_S) -> tuple[str, bool]:
        """(text, is_error)."""
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if not isinstance(result, dict):
            return "", True
        parts: list[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text", "")))
            elif kind == "resource" and isinstance(block.get("resource"), dict) \
                    and isinstance(block["resource"].get("text"), str):
                parts.append(block["resource"]["text"])
            elif kind in ("image", "audio", "resource", "resource_link"):
                parts.append(f"[{kind}]")
        if not parts and isinstance(result.get("structuredContent"), (dict, list)):
            parts.append(json.dumps(result["structuredContent"])[:20000])
        return "\n".join(parts), bool(result.get("isError"))

    # --- the pipe ----------------------------------------------------------------

    def _send(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError(f"{self.name} isn't running")
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise McpError(f"{self.name} stopped") from exc

    def _read(self) -> None:
        proc = self._proc
        out = proc.stdout if proc is not None else None
        if out is None:
            return
        while True:
            try:
                line = out.readline(MAX_LINE + 1)
            except (OSError, ValueError):
                break
            if not line:
                break
            if len(line) > MAX_LINE:
                _log.warning("%s sent a message over %d bytes; stopping it", self.name, MAX_LINE)
                self.close()
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # stray output on stdout
            if isinstance(msg, dict):
                self._dispatch(msg)
        self._eof = True
        self._fail_all("the connector stopped")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:  # a request from the server
            if msg.get("method") == "ping":
                reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
            else:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32601, "message": "not supported by NOVA"}}
            try:
                self._send(reply)
            except McpError:
                pass
            return
        if "id" not in msg or "method" in msg:
            return  # a notification: logging, progress, list_changed...
        with self._lock:
            slot = self._pending.get(msg["id"]) if isinstance(msg["id"], int) else None
        if slot is None:
            return
        if "error" in msg:
            slot["error"] = msg["error"]
        else:
            slot["result"] = msg.get("result")
        slot["event"].set()

    def _drain_stderr(self) -> None:
        proc = self._proc
        err = proc.stderr if proc is not None else None
        if err is None:
            return
        for raw in iter(lambda: err.readline(65536), b""):
            self._stderr.append(raw.decode("utf-8", "replace").rstrip()[:500])

    def _fail_all(self, why: str) -> None:
        with self._lock:
            slots = list(self._pending.values())
        for slot in slots:
            if not slot["event"].is_set():
                slot["error"] = {"message": why}
                slot["event"].set()
