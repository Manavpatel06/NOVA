"""Kept-open HTTPS connections (2026-11-26, voice delay): same rules, fewer handshakes."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nova.security.egress import ConfigError, Egress, EgressError, EgressRedirectBlocked

HOST = "hub.example.com"


class _Audit:
    def __init__(self):
        self.rows = []

    def record(self, row):
        self.rows.append(row)


@pytest.fixture
def server():
    ports = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            ports.append(self.client_address[1])
            if self.path == "/sse":
                payload = b"event: x\ndata: {\"type\": \"a\"}\n\n: comment\ndata: not json\n\ndata: {\"type\": \"b\"}\n\n"
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("location", "https://evil.example.net/")
                self.send_header("content-length", "0")
                self.end_headers()
                return
            code = 500 if self.path == "/err" else 200
            body = json.dumps({"error": {"message": "boom"}} if code == 500 else {"ok": True}).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            if self.path == "/close":
                self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(body)
            if self.path == "/silentclose":   # closes without saying so, like an idle timeout
                self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], ports
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def plain(monkeypatch, server):
    """Stand in for TLS: the test server speaks plain HTTP on 127.0.0.1."""
    port = server[0]

    class Conn(http.client.HTTPConnection):
        def __init__(self, host, p=None, timeout=None, context=None):
            assert host == HOST and context is not None   # a verifying TLS context is always passed
            super().__init__("127.0.0.1", port, timeout=timeout)

    monkeypatch.setattr(http.client, "HTTPSConnection", Conn)
    return server[1]


def _egress(audit=None):
    return Egress([HOST], audit=audit or _Audit(), keep_alive_hosts=[HOST])


def test_requests_reuse_one_connection(plain):
    eg = _egress()
    for _ in range(4):
        assert eg.post_json(f"https://{HOST}/ok", {"a": 1}, headers={}) == {"ok": True}
    assert len(set(plain)) == 1


def test_a_server_closed_connection_is_replaced(plain):
    eg = _egress()
    eg.post_json(f"https://{HOST}/close", {}, headers={})
    eg.post_json(f"https://{HOST}/ok", {}, headers={})
    assert len(set(plain)) == 2


def test_redirects_are_still_refused_and_audited(plain):
    audit = _Audit()
    with pytest.raises(EgressRedirectBlocked):
        _egress(audit).post_json(f"https://{HOST}/redir", {}, headers={})
    assert any(r["kind"] == "egress.post_redirect_blocked" for r in audit.rows)


def test_http_errors_carry_status_and_message(plain):
    with pytest.raises(EgressError) as e:
        _egress().post_json(f"https://{HOST}/err", {}, headers={})
    assert e.value.status == 500 and "boom" in str(e.value)


def test_keep_alive_hosts_must_be_allowlisted():
    with pytest.raises(ConfigError):
        Egress(["api.example.com"], audit=_Audit(), keep_alive_hosts=["other.example.com"])


def test_other_hosts_still_go_through_urlopen(monkeypatch):
    import urllib.request

    called = []

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"x": 1}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: called.append(req.full_url) or R())
    eg = Egress([HOST, "api.example.com"], audit=_Audit(), keep_alive_hosts=[HOST])
    assert eg.post_json("https://api.example.com/v1", {}, headers={}) == {"x": 1}
    assert called == ["https://api.example.com/v1"]


def test_a_connection_that_went_stale_while_idle_is_retried_once(plain):
    eg = _egress()
    eg.post_json(f"https://{HOST}/silentclose", {}, headers={})
    import time
    time.sleep(0.2)
    assert eg.post_json(f"https://{HOST}/ok", {}, headers={}) == {"ok": True}
    assert len(set(plain)) == 2


def test_streams_yield_each_event_and_keep_the_connection(plain):
    audit = _Audit()
    eg = _egress(audit)
    assert [e["type"] for e in eg.post_stream(f"https://{HOST}/sse", {}, headers={})] == ["a", "b"]
    assert [e["type"] for e in eg.post_stream(f"https://{HOST}/sse", {}, headers={})] == ["a", "b"]
    assert len(set(plain)) == 1
    assert any(r.get("stream") for r in audit.rows) and any(r.get("bytes_in") for r in audit.rows)


def test_streams_refuse_redirects_errors_and_other_hosts(plain):
    eg = _egress()
    with pytest.raises(EgressRedirectBlocked):
        list(eg.post_stream(f"https://{HOST}/redir", {}, headers={}))
    with pytest.raises(EgressError) as e:
        list(eg.post_stream(f"https://{HOST}/err", {}, headers={}))
    assert e.value.status == 500
    with pytest.raises(EgressError):
        list(eg.post_stream("https://evil.example.net/x", {}, headers={}))
