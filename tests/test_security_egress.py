"""Contract: docs/SECURITY_SPEC.md §2, docs/THREAT_MODEL.md §1.3 — Egress.

THE ONLY MODULE THAT MAY REACH THE NETWORK. Deny-by-default allowlist,
normalized and validated at construction (not at config-load time); no
runtime mutator; exact-match host membership, never suffix matching;
release_for_process redacts+audits (lengths and a boolean, never the text)
and propagates an audit-write failure uncaught.

No real network I/O happens in this file — urllib.request.urlopen is
replaced with a fake for every fetch() test.
"""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import nova.security.egress as egress_mod
from nova.config import ConfigError
from nova.security.egress import Egress, EgressError, EgressRedirectBlocked, normalize_host


class _FakeAudit:
    def __init__(self, raise_on_record: Exception | None = None):
        self.records: list[dict] = []
        self._raise = raise_on_record

    def record(self, event: dict) -> None:
        if self._raise is not None:
            raise self._raise
        self.records.append(event)


@pytest.fixture
def audit() -> _FakeAudit:
    return _FakeAudit()


# ---------------------------------------------------------------------------
# normalize_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.com", "example.com"),
        ("EXAMPLE.com", "example.com"),
        ("  example.com  ", "example.com"),
        ("api.example.com", "api.example.com"),
    ],
)
def test_normalize_host_accepts_bare_hostnames(raw, expected):
    assert normalize_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com",
        "http://example.com",
        "example.com/path",
        "example.com:8080",
        "*.example.com",
        "example.com.",  # trailing dot
        "192.168.1.1",
        "",
        "   ",
        "::1",
    ],
)
def test_normalize_host_rejects_anything_not_a_bare_hostname(raw):
    assert normalize_host(raw) is None


# ---------------------------------------------------------------------------
# Construction: allowlist validated at Egress.__init__, not at config-load
# ---------------------------------------------------------------------------


def test_construction_normalizes_and_lowercases_the_allowlist(audit):
    eg = Egress(["Example.COM", "api.example.com"], audit=audit)
    assert eg.allowed_hosts == frozenset({"example.com", "api.example.com"})


@pytest.mark.parametrize("bad_entry", ["https://example.com", "*.example.com", "192.168.1.1", ""])
def test_construction_raises_config_error_on_a_malformed_entry(audit, bad_entry):
    with pytest.raises(ConfigError):
        Egress([bad_entry], audit=audit)


def test_empty_allowlist_is_valid_deny_everything(audit):
    eg = Egress([], audit=audit)
    assert eg.allowed_hosts == frozenset()


# ---------------------------------------------------------------------------
# No runtime allowlist mutator, anywhere
# ---------------------------------------------------------------------------


def test_egress_has_no_runtime_allowlist_mutator_method(audit):
    forbidden_name_fragments = ("add_allowed", "allow", "permit", "whitelist", "unblock")
    for attr_name in dir(Egress):
        if attr_name.startswith("_"):
            continue
        if attr_name in ("allowed_hosts", "fetch", "release_for_process"):
            continue
        lowered = attr_name.lower()
        assert not any(frag in lowered for frag in forbidden_name_fragments), (
            f"Egress has a method/attr {attr_name!r} shaped like a runtime allowlist mutator"
        )


# ---------------------------------------------------------------------------
# fetch(): deny-by-default, exact match, audited
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_urlopen(monkeypatch):
    calls = []

    def fake(url, *a, **kw):
        calls.append(url)
        return _FakeResponse(b"fake response body")

    monkeypatch.setattr(egress_mod.urllib.request, "urlopen", fake)
    return calls


def test_fetch_allowed_host_returns_the_body_and_audits_allowed_true(audit, fake_urlopen):
    eg = Egress(["example.com"], audit=audit)
    body = eg.fetch("https://example.com/path?x=1")

    assert body == b"fake response body"
    assert fake_urlopen == ["https://example.com/path?x=1"]
    assert audit.records[-1] == {"kind": "egress.fetch", "host": "example.com", "allowed": True}


def test_fetch_disallowed_host_raises_and_never_calls_urlopen(audit, fake_urlopen):
    eg = Egress(["example.com"], audit=audit)

    with pytest.raises(EgressError):
        eg.fetch("https://evil.example/path")

    assert fake_urlopen == []
    assert audit.records[-1]["allowed"] is False


def test_fetch_uses_exact_match_not_suffix_match(audit, fake_urlopen):
    """'evil-notion.so' must not pass an allowlist of 'notion.so' via
    endswith — the exact bypass the threat model calls out by name."""
    eg = Egress(["notion.so"], audit=audit)

    with pytest.raises(EgressError):
        eg.fetch("https://evil-notion.so/exfil")

    assert fake_urlopen == []


def test_fetch_subdomain_is_not_implicitly_allowed(audit, fake_urlopen):
    eg = Egress(["example.com"], audit=audit)
    with pytest.raises(EgressError):
        eg.fetch("https://sub.example.com/")
    assert fake_urlopen == []


def test_fetch_always_audits_even_when_denied(audit, fake_urlopen):
    eg = Egress(["example.com"], audit=audit)
    before = len(audit.records)
    with pytest.raises(EgressError):
        eg.fetch("https://evil.example/")
    assert len(audit.records) == before + 1


def test_fetch_propagates_audit_write_failure_and_never_reaches_urlopen(fake_urlopen):
    failing_audit = _FakeAudit(raise_on_record=RuntimeError("disk full"))
    eg = Egress(["example.com"], audit=failing_audit)

    with pytest.raises(RuntimeError):
        eg.fetch("https://example.com/")

    assert fake_urlopen == []  # audit write happens before urlopen; failure must block it


# ---------------------------------------------------------------------------
# release_for_process(): redact, audit lengths+bool (never text), propagate
# audit failures
# ---------------------------------------------------------------------------


def test_release_for_process_redacts_the_payload(audit):
    eg = Egress([], audit=audit)
    out = eg.release_for_process("claude_cli_dispatch", r"look at C:\Users\manav\notes.txt")
    assert "manav" not in out
    assert "[USER]" in out


def test_release_for_process_audit_record_never_contains_the_text(audit):
    eg = Egress([], audit=audit)
    payload = "the secret sauce is 12345, do not repeat it"
    eg.release_for_process("claude_cli_dispatch", payload)

    record = audit.records[-1]
    assert record["kind"] == "egress.release_for_process"
    assert record["purpose"] == "claude_cli_dispatch"
    assert record["chars_in"] == len(payload)
    assert "chars_out" in record
    assert isinstance(record["redacted"], bool)
    for value in record.values():
        assert "secret sauce" not in str(value)
        assert "12345" not in str(value)


def test_release_for_process_flags_redacted_true_when_something_changed(audit):
    eg = Egress([], audit=audit)
    record_before = eg.release_for_process("purpose_ok", r"C:\Users\manav\x")
    eg2 = Egress([], audit=audit)
    assert audit.records[-1]["redacted"] is True


def test_release_for_process_flags_redacted_false_when_nothing_changed(audit):
    eg = Egress([], audit=audit)
    eg.release_for_process("purpose_ok", "nothing sensitive here at all")
    assert audit.records[-1]["redacted"] is False


@pytest.mark.parametrize("bad_purpose", ["", "AB", "Has Spaces", "UPPERCASE", "a" * 100])
def test_release_for_process_rejects_malformed_purpose(audit, bad_purpose):
    eg = Egress([], audit=audit)
    with pytest.raises(ValueError):
        eg.release_for_process(bad_purpose, "text")


def test_release_for_process_rejects_non_str_payload(audit):
    eg = Egress([], audit=audit)
    with pytest.raises(TypeError):
        eg.release_for_process("purpose_ok", b"not a str")  # type: ignore[arg-type]


def test_release_for_process_propagates_audit_write_failure():
    failing_audit = _FakeAudit(raise_on_record=RuntimeError("disk full"))
    eg = Egress([], audit=failing_audit)

    with pytest.raises(RuntimeError):
        eg.release_for_process("purpose_ok", "some text to release")


# ---------------------------------------------------------------------------
# F35 — the F13 open-redirect fix, end to end, unmocked.
#
# Every fetch() test above replaces urllib.request.urlopen with a fake, so
# none of them ever runs install_opener()/_NoRedirectHandler.redirect_request
# at all -- a refactor that drops the install_opener() call, or stops
# fetch() using the module-installed opener, would leave every test above
# green while reopening a live security bypass. This section is the only
# thing in the suite that would catch that: two real
# http.server.HTTPServer instances, a real urlopen, a real install_opener()
# call, nothing mocked except DNS resolution.
#
# normalize_host() correctly rejects "localhost" (no dot -- see _HOST_RE)
# and any IP literal, so a real server needs a real DOTTED hostname to
# fetch() against. loopback_dns below fakes socket.getaddrinfo() so any
# hostname resolves to 127.0.0.1 -- that's the ONLY thing faked; the HTTP
# client, the redirect, and Egress's own opener are all completely real.
# ".test" is the IANA/RFC 2606-reserved TLD for exactly this purpose.
#
# Framing, precisely: the fix blocks ALL redirects, host-agnostic -- it
# never re-validates a redirect target at all, allowlisted or not. So the
# two tests below assert the general claim ("a 302 is refused and
# audited"), not the narrower one ("a redirect to a non-allowlisted host is
# refused") -- the second test specifically proves the fix doesn't
# secretly get narrower than that by pointing the redirect at a host that
# IS on the allowlist and confirming it's refused too.
# ---------------------------------------------------------------------------


@pytest.fixture
def loopback_dns(monkeypatch):
    """Any hostname resolves to 127.0.0.1. The only fake in this section --
    everything downstream (TCP connect, HTTP, the redirect, Egress's real
    installed opener) is completely real."""
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def http_server():
    """Starts real http.server.HTTPServer instances on their own daemon
    threads (127.0.0.1, OS-assigned port) and shuts them all down cleanly
    at teardown -- this is the fixture, not per-test boilerplate, because
    every test below needs at least one and some need two."""
    started: list[HTTPServer] = []

    def _start(server: HTTPServer) -> HTTPServer:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield _start

    for server in started:
        server.shutdown()
        server.server_close()


@pytest.fixture
def restore_urllib_opener():
    """Egress.__init__ calls urllib.request.install_opener() -- a real,
    process-wide global. Save and restore it so a real-network test here
    can't leak a redirect-blocking opener into whatever test runs next in
    the same process."""
    original = egress_mod.urllib.request._opener
    yield
    egress_mod.urllib.request._opener = original


class _RedirectToOtherHostHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.server.redirect_target)  # type: ignore[attr-defined]
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 -- BaseHTTPRequestHandler's own name
        pass  # silence default request logging to stderr


class _RedirectToOtherHostServer(HTTPServer):
    def __init__(self, addr, redirect_target: str):
        self.redirect_target = redirect_target
        super().__init__(addr, _RedirectToOtherHostHandler)


class _CountingTargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.request_count += 1  # type: ignore[attr-defined]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"this must never be reached")

    def log_message(self, format, *args):  # noqa: A002
        pass


class _CountingTargetServer(HTTPServer):
    def __init__(self, addr):
        self.request_count = 0
        super().__init__(addr, _CountingTargetHandler)


class _SelfRedirectHandler(BaseHTTPRequestHandler):
    """Redirects "/" to "/target" on the SAME host:port -- lands on an
    ALLOWLISTED host by construction, for the host-agnostic test below."""

    def do_GET(self):
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", f"http://allowlisted.test:{self.server.server_port}/target")
            self.end_headers()
        else:
            self.server.request_count += 1  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"this must never be reached")

    def log_message(self, format, *args):  # noqa: A002
        pass


class _SelfRedirectServer(HTTPServer):
    def __init__(self, addr):
        self.request_count = 0
        super().__init__(addr, _SelfRedirectHandler)


def test_fetch_blocks_a_real_redirect_to_a_non_allowlisted_host_end_to_end(
    audit, http_server, loopback_dns, restore_urllib_opener
):
    target = http_server(_CountingTargetServer(("127.0.0.1", 0)))
    origin = http_server(
        _RedirectToOtherHostServer(("127.0.0.1", 0), f"http://evil.test:{target.server_port}/exfil")
    )

    eg = Egress(["allowlisted.test"], audit=audit)  # real install_opener() runs here

    with pytest.raises(EgressRedirectBlocked):
        eg.fetch(f"http://allowlisted.test:{origin.server_port}/")

    assert target.request_count == 0, "the redirect target was actually reached -- the block failed"
    assert audit.records[0] == {"kind": "egress.fetch", "host": "allowlisted.test", "allowed": True}
    assert audit.records[1]["kind"] == "egress.fetch_redirect_blocked"
    assert audit.records[1]["redirect_target_host"] == "evil.test"
    assert audit.records[1]["status"] == 302


def test_fetch_blocks_a_real_redirect_even_when_it_lands_on_an_allowlisted_host(
    audit, http_server, loopback_dns, restore_urllib_opener
):
    """The sharper, more precise claim: the fix is host-agnostic. A version
    that only re-validated the redirect target against the allowlist
    (rather than refusing every redirect outright) would pass the test
    above but incorrectly follow this one."""
    server = http_server(_SelfRedirectServer(("127.0.0.1", 0)))

    eg = Egress(["allowlisted.test"], audit=audit)

    with pytest.raises(EgressRedirectBlocked):
        eg.fetch(f"http://allowlisted.test:{server.server_port}/")

    assert server.request_count == 0, "the redirect landed on an allowlisted host and was followed anyway"
    assert audit.records[-1]["kind"] == "egress.fetch_redirect_blocked"
    assert audit.records[-1]["redirect_target_host"] == "allowlisted.test"


# 2026-10-15: fetch() takes an optional timeout and hands it to urlopen, so a
# hung host can't stall a voice turn forever.


def test_fetch_passes_a_default_timeout_to_urlopen(audit, monkeypatch):
    seen = []

    def fake(url, *a, **kw):
        seen.append(kw.get("timeout"))
        return _FakeResponse(b"ok")

    monkeypatch.setattr(egress_mod.urllib.request, "urlopen", fake)
    eg = Egress(["example.com"], audit=audit)
    eg.fetch("https://example.com/")
    eg.fetch("https://example.com/", timeout=2.5)
    assert seen == [10.0, 2.5]


def test_fetch_timeout_propagates_and_was_audited_first(audit, monkeypatch):
    def slow(url, *a, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(egress_mod.urllib.request, "urlopen", slow)
    eg = Egress(["example.com"], audit=audit)
    with pytest.raises(TimeoutError):
        eg.fetch("https://example.com/", timeout=0.01)
    assert audit.records[-1] == {"kind": "egress.fetch", "host": "example.com", "allowed": True}


# 2026-10-15 security review: post_json (the Claude API call) -- refusal,
# what the audit log may contain, and the blocked-redirect path.

_KEY = "sk-ant-api03-SECRETSECRETSECRETSECRET"


def test_post_json_refuses_non_allowlisted_and_plain_http_before_any_network(audit, monkeypatch, restore_urllib_opener):
    def _boom(*a, **k):
        raise AssertionError("urlopen must not be reached")

    monkeypatch.setattr(egress_mod.urllib.request, "urlopen", _boom)
    eg = Egress(["api.anthropic.com"], audit=audit)
    for url in ("https://evil.test/v1/messages", "http://api.anthropic.com/v1/messages"):
        with pytest.raises(EgressError) as ei:
            eg.post_json(url, {"q": "hello"}, headers={"x-api-key": _KEY})
        assert _KEY not in str(ei.value)
    assert [r["allowed"] for r in audit.records] == [False, False]


def test_post_json_audit_never_holds_body_or_key_and_redirects_are_audited(audit, monkeypatch, restore_urllib_opener):
    def _redirect(req, timeout):
        raise EgressRedirectBlocked(original_url=req.full_url, redirect_url="https://evil.test/x", status=307)

    monkeypatch.setattr(egress_mod.urllib.request, "urlopen", _redirect)
    eg = Egress(["api.anthropic.com"], audit=audit)
    with pytest.raises(EgressRedirectBlocked) as ei:
        eg.post_json("https://api.anthropic.com/v1/messages", {"messages": "my secret sentence"},
                     headers={"x-api-key": _KEY})
    assert _KEY not in str(ei.value)
    dumped = repr(audit.records)
    assert _KEY not in dumped and "my secret sentence" not in dumped
    assert audit.records[-1]["kind"] == "egress.post_redirect_blocked"
    assert audit.records[-1]["redirect_target_host"] == "evil.test"
