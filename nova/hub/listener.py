"""nova-hub's ONE listening socket (2026-11-22, docs/decisions/0019).

The second file allowed to import a transport (tests/test_no_network_or_
process_spawn.py TRANSPORT_ALLOWLIST): nova.security.egress is how NOVA
reaches OUT; this is how the hub is reached IN, and nothing else listens.

It binds 127.0.0.1 only -- never a LAN or Tailscale address. The only way in
from another device is `tailscale serve`, which terminates HTTPS on the
tailnet and forwards here with a Tailscale-User-Login header naming who is
asking; HubService.is_owner() decides what that person gets, and only for
requests addressed to the hub's own name (HubService.host_ok: a web page
open on the hub can't reach this port under some other name and pass). The firewall
(setup_hub.sh) already drops everything not coming over Tailscale.

Requests and replies are small JSON. Sentence text is never logged here.
"""
from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nova.hub.service import HubError, HubService
from nova.log import get_logger

_log = get_logger("hub.listener")

BIND_HOST = "127.0.0.1"
MAX_BODY = 6_000_000
OWNER_HEADER = "Tailscale-User-Login"


#: owner-only JSON routes -> HubService method
_POST_ROUTES = {
    "/v1/brain/start": "brain_start",
    "/v1/brain/send": "brain_send",
    "/v1/brain/close": "brain_close",
    "/v1/forge/request": "forge_request",
    "/v1/forge/list": "forge_list",
    "/v1/forge/ready": "forge_ready",
    "/v1/forge/apply": "forge_apply",
    "/v1/forge/discard": "forge_discard",
}


def make_server(service: HubService, port: int) -> ThreadingHTTPServer:
    """The hub's server, bound to loopback. port 0 = any free port (tests)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "nova-hub"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 -- no paths/bodies in our logs
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Any:
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                raise HubError("bad content-length") from None
            if length <= 0 or length > MAX_BODY:
                raise HubError("body missing or too large")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise HubError("body is not JSON") from None

        def _is_owner(self) -> bool:
            return service.host_ok(self.headers.get("host"), port_bound) and service.is_owner(
                self.headers.get(OWNER_HEADER))

        def _owner_only(self) -> bool:
            if self._is_owner():
                return True
            _log.warning("refused %s %s: not the hub's owner", self.command, self.path)
            self._send(HTTPStatus.FORBIDDEN, {"error": "only the hub's owner may do this"})
            return False

        def _send_html(self, page: str) -> None:
            data = page.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.send_header("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            ack = re.fullmatch(r"/v1/ack/([0-9a-f]{8})\?t=([A-Za-z0-9_-]{8,40})", self.path)
            if ack:
                if self._owner_only():
                    self._send_html(service.ack_page(ack.group(1), ack.group(2)))
                return
            if self.path == "/v1/health":
                local = self.headers.get(OWNER_HEADER) is None and service.host_ok(self.headers.get("host"), port_bound)
                self._send(HTTPStatus.OK, service.health(full=local or self._is_owner()))
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _handle(self, action) -> None:
            if not self._owner_only():
                return
            try:
                self._send(HTTPStatus.OK, action(self._body()))
            except HubError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 -- one bad request never takes the hub down
                _log.error("%s %s failed (%s)", self.command, self.path, type(exc).__name__)
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/v1/route":
                self._handle(service.route)
            elif self.path == "/v1/reminders":
                self._handle(service.add_reminder)
            elif self.path == "/v1/reminders/list":
                self._handle(service.list_reminders)
            elif self.path == "/v1/reminders/cancel":
                self._handle(service.cancel_reminder)
            elif self.path in _POST_ROUTES:
                self._handle(getattr(service, _POST_ROUTES[self.path]))
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_PUT(self) -> None:  # noqa: N802
            if self.path == "/v1/memory":
                self._handle(service.put_memory)
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    server = ThreadingHTTPServer((BIND_HOST, port), Handler)
    server.daemon_threads = True
    port_bound = server.server_address[1]
    return server
