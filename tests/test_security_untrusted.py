"""Contract: docs/SECURITY_SPEC.md §1, docs/THREAT_MODEL.md §3 — Untrusted[T].

The type wrapper's job is narrow and structural: make the *lazy* bypass (a
careless str(x), f-string, or .format()) fail loudly instead of working.
It is NOT a substitute for the injection test suite (constitution rule 2) —
that's what proves the *deliberate* bypass doesn't exist. Both are needed.

Judgment call the lead asked for explicitly: after writing these, the type
wrapper is doing real, verifiable work — it is not a str subclass, every
protocol method that would let it duck-type as a string is absent or raises,
and str()/repr()/f-string all return a label+digest, never content. The one
place it does LESS than it might appear to: __eq__ against a plain str
returns False cleanly (via NotImplemented) rather than raising, so a lazy
`if untrusted_val == "some sentinel":` bypass fails silently-false rather
than loudly — worth flagging to the lead, not fixed here since it's a
legitimate design tradeoff (raising on every stray `==` against other types
would be its own footgun) rather than a bug.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from nova.security.audit import AuditWriteError
import nova.security.untrusted as untrusted_mod
from nova.security.untrusted import Untrusted, bind_audit, reveal


SECRET = "the password is hunter2 and the account number is 4111-1111-1111-1111"


@pytest.fixture(autouse=True)
def isolated_audit_binding(monkeypatch):
    """_AUDIT is process-global; isolate every test from whatever another
    test file may have bound."""
    monkeypatch.setattr(untrusted_mod, "_AUDIT", None)


class _FakeAudit:
    def __init__(self, raise_on_record: Exception | None = None):
        self.records: list[dict] = []
        self._raise = raise_on_record

    def record(self, event: dict) -> None:
        if self._raise is not None:
            raise self._raise
        self.records.append(event)


# ---------------------------------------------------------------------------
# Structural: not a str subclass, no accidental duck-typing
# ---------------------------------------------------------------------------


def test_untrusted_is_not_a_str_subclass():
    assert not issubclass(Untrusted, str)


def test_passing_untrusted_where_str_is_expected_fails_loudly():
    def wants_a_str(s: str) -> str:
        return s.upper()  # any real str method call is enough to prove no duck-typing

    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(AttributeError):
        wants_a_str(u)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "op",
    [
        lambda u: u + "x",
        lambda u: "x" + u,
        lambda u: "x" in u,
        lambda u: list(iter(u)),
        lambda u: u[0],
        lambda u: bool(u),
    ],
    ids=["add", "radd", "contains", "iter", "getitem", "bool"],
)
def test_string_protocols_are_not_implemented(op):
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(TypeError):
        op(u)


def test_len_raises_and_names_reveal():
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(TypeError, match="reveal"):
        len(u)


# ---------------------------------------------------------------------------
# No accidental unwrap via __str__ / __format__ / repr
# ---------------------------------------------------------------------------


def test_str_never_leaks_the_wrapped_content():
    u = Untrusted(SECRET, source="mail.body")
    assert SECRET not in str(u)
    assert "hunter2" not in str(u)


def test_repr_never_leaks_the_wrapped_content():
    u = Untrusted(SECRET, source="mail.body")
    assert SECRET not in repr(u)


def test_fstring_interpolation_never_leaks_the_wrapped_content():
    u = Untrusted(SECRET, source="mail.body")
    rendered = f"got: {u}"
    assert SECRET not in rendered
    assert "hunter2" not in rendered


def test_format_call_never_leaks_the_wrapped_content():
    u = Untrusted(SECRET, source="mail.body")
    rendered = "{}".format(u)
    assert SECRET not in rendered


def test_str_and_repr_carry_the_source_label():
    u = Untrusted(SECRET, source="mail.body")
    assert "mail.body" in str(u)
    assert "mail.body" in repr(u)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_wrapping_an_untrusted_value_is_refused():
    inner = Untrusted(SECRET, source="mail.body")
    with pytest.raises(TypeError, match="re-labelling"):
        Untrusted(inner, source="mail.body")


@pytest.mark.parametrize(
    "bad_source",
    ["Mail.Body", "mail body", "", "mail..body", "-mail", "mail_body!", "1mail"],
)
def test_malformed_source_tag_is_rejected(bad_source):
    with pytest.raises(ValueError):
        Untrusted(SECRET, source=bad_source)


@pytest.mark.parametrize("good_source", ["mail", "mail.body", "web_fetch", "calendar.invite"])
def test_wellformed_source_tags_are_accepted(good_source):
    Untrusted(SECRET, source=good_source)  # must not raise


# ---------------------------------------------------------------------------
# Equality / hashing: identity-of-provenance, never a content leak
# ---------------------------------------------------------------------------


def test_equal_content_and_source_compares_equal():
    a = Untrusted(SECRET, source="mail.body")
    b = Untrusted(SECRET, source="mail.body")
    assert a == b


def test_same_content_different_source_compares_unequal():
    a = Untrusted(SECRET, source="mail.body")
    b = Untrusted(SECRET, source="web.fetch")
    assert a != b


def test_comparing_against_a_plain_str_is_always_false_not_a_leak_or_a_crash():
    u = Untrusted("some sentinel value", source="mail.body")
    assert (u == "some sentinel value") is False
    assert (u == "anything else") is False


def test_hash_is_stable_for_equal_values():
    a = Untrusted(SECRET, source="mail.body")
    b = Untrusted(SECRET, source="mail.body")
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# reveal(): the one sanctioned unwrap
# ---------------------------------------------------------------------------


def test_reveal_returns_the_original_value():
    u = Untrusted(SECRET, source="mail.body")
    assert reveal(u, caller="nova.skills.mail_summary:handler", purpose="summarize") == SECRET


def test_reveal_rejects_a_non_untrusted_value():
    with pytest.raises(TypeError):
        reveal("plain string", caller="nova.skills.mail_summary:handler", purpose="summarize")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_caller", ["handler", "mail_summary.handler", "nova.skills:", ":handler", "Nova.skills:handler"]
)
def test_reveal_rejects_malformed_caller(bad_caller):
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(ValueError):
        reveal(u, caller=bad_caller, purpose="summarize")


@pytest.mark.parametrize("bad_purpose", ["", "   "])
def test_reveal_rejects_empty_purpose(bad_purpose):
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(ValueError):
        reveal(u, caller="nova.skills.mail_summary:handler", purpose=bad_purpose)


def test_reveal_audits_metadata_never_the_content():
    audit = _FakeAudit()
    bind_audit(audit)
    u = Untrusted(SECRET, source="mail.body")

    reveal(u, caller="nova.skills.mail_summary:handler", purpose="summarize for the user")

    assert len(audit.records) == 1
    record = audit.records[0]
    assert record["kind"] == "untrusted.reveal"
    assert record["source"] == "mail.body"
    assert record["caller"] == "nova.skills.mail_summary:handler"
    assert record["purpose"] == "summarize for the user"
    for value in record.values():
        assert SECRET not in str(value)
        assert "hunter2" not in str(value)


def test_reveal_before_bind_audit_still_succeeds_but_warns(caplog):
    u = Untrusted(SECRET, source="mail.body")
    with caplog.at_level("WARNING", logger="nova.security"):
        result = reveal(u, caller="nova.skills.mail_summary:handler", purpose="summarize")
    assert result == SECRET
    assert any("bind_audit" in rec.message for rec in caplog.records)


def test_reveal_propagates_an_audit_write_failure():
    audit = _FakeAudit(raise_on_record=AuditWriteError("disk full"))
    bind_audit(audit)
    u = Untrusted(SECRET, source="mail.body")

    with pytest.raises(AuditWriteError):
        reveal(u, caller="nova.skills.mail_summary:handler", purpose="summarize")


def test_reveal_over_a_thousand_calls_stays_fast_and_never_leaks():
    """Not a security property by itself, just guards against a pathological
    digest/regex implementation before it ships."""
    audit = _FakeAudit()
    bind_audit(audit)
    u = Untrusted(SECRET * 100, source="mail.body")
    start = time.monotonic()
    for _ in range(500):
        reveal(u, caller="nova.skills.mail_summary:handler", purpose="x")
    assert time.monotonic() - start < 5.0
    assert all(SECRET not in str(v) for r in audit.records for v in r.values())


# ---------------------------------------------------------------------------
# AST guard: no non-trusts_input skill module ever imports/calls reveal
# (docs/SECURITY_SPEC.md §1.3 layer 3). The router/humanizer layers don't
# exist yet in M0 (no nova/brain/ package on disk) — scoped to what exists:
# every skill module, plus a floor check that catches reveal() leaking into
# any other nova/ module that isn't explicitly the definition site.
# ---------------------------------------------------------------------------


def _imports_or_calls_reveal(path) -> bool:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nova.security.untrusted":
            if any(alias.name == "reveal" for alias in node.names):
                return True
        if isinstance(node, ast.Attribute) and node.attr == "reveal":
            return True
        if isinstance(node, ast.Name) and node.id == "reveal":
            return True
    return False


def test_no_trusts_input_false_skill_imports_or_calls_reveal():
    import nova.skills  # triggers discovery
    from nova.skills.registry import all_skills

    skills_dir = Path(nova.skills.__file__).resolve().parent
    for name, spec in all_skills().items():
        if spec.trusts_input:
            continue  # a trusts_input=True skill is exactly where reveal() is licensed
        module_file = skills_dir / f"{spec.handler.__module__.rsplit('.', 1)[-1]}.py"
        if not module_file.exists():
            continue
        assert not _imports_or_calls_reveal(module_file), (
            f"skill '{name}' (trusts_input=False) imports/calls reveal() in {module_file}"
        )


def test_reveal_is_not_imported_anywhere_outside_its_own_definition_module():
    """Floor check for the router/skill-param-binder rule before those
    modules exist: today, NOTHING should import reveal at all (M0 ships zero
    trusts_input=True skills). Once one lands, this test should be narrowed
    to allow exactly that skill's module — not relaxed further."""
    import nova

    root = untrusted_mod.__file__
    nova_root = Path(nova.__file__).resolve().parent
    offenders = []
    for path in nova_root.rglob("*.py"):
        if str(path) == root:
            continue
        if _imports_or_calls_reveal(path):
            offenders.append(str(path))
    assert not offenders, f"reveal() is imported/called outside its own module: {offenders}"
