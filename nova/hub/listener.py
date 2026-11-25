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

Cross-site requests are refused (2026-11-25). The owner check is about the
DEVICE (Tailscale names who is connecting, not which web page), so without
this any website open in a browser on one of Manav's devices could fire
requests here -- it couldn't read the answers, but it could start things.
Browsers always mark such requests (Origin / Sec-Fetch-Site), and a request
body must be declared JSON, which browsers can't send cross-site without
asking first (CORS preflight, which this server never grants).
"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nova.hub.service import HubError, HubService
from nova.log import get_logger

_log = get_logger("hub.listener")

BIND_HOST = "127.0.0.1"
TALK_PAGE = Path(__file__).with_name("talk.html")
MAX_BODY = 6_000_000
OWNER_HEADER = "Tailscale-User-Login"


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
            ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise HubError("body must be sent as application/json")
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

        def _cross_site(self) -> bool:
            """A browser sent this from some other website."""
            fetch_site = (self.headers.get("sec-fetch-site") or "").strip().lower()
            if fetch_site and fetch_site not in ("same-origin", "none"):
                return True
            origin = (self.headers.get("origin") or "").strip().lower().rstrip("/")
            if not origin:
                return False
            name = service.hub_name()
            return not name or origin != f"https://{name}"

        def _owner_only(self) -> bool:
            if self._cross_site():
                _log.warning("refused %s %s: sent from another website", self.command, self.path)
                self._send(HTTPStatus.FORBIDDEN, {"error": "cross-site requests are not allowed"})
                return False
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

        def _send_talk_page(self) -> None:
            nonce = secrets.token_urlsafe(18)
            data = TALK_PAGE.read_text(encoding="utf-8").replace("__NONCE__", nonce).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-frame-options", "DENY")
            self.send_header("referrer-policy", "no-referrer")
            self.send_header("content-security-policy",
                             f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; media-src blob: data:; base-uri 'none'; form-action 'none'; "
                             "frame-ancestors 'none'")
            self.send_header("permissions-policy", "microphone=(self), camera=(), geolocation=()")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/talk", "/"):
                if self._owner_only():
                    self._send_talk_page()
                return
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
            if self.path in service.ROUTES:
                self._handle(getattr(service, service.ROUTES[self.path]))
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
