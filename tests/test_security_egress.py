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

import pytest

import nova.security.egress as egress_mod
from nova.config import ConfigError
from nova.security.egress import Egress, EgressError, normalize_host


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
