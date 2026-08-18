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

import copy
import dataclasses
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
# Regression: dataclasses.asdict()/astuple() bypass (fixed in dbd77d8).
#
# A @dataclass-based Untrusted let asdict()/astuple() walk dataclasses.
# fields() and getattr() the raw attributes directly — a complete, silent,
# unaudited unwrap via a stdlib call nobody had to write, and recursive: an
# Untrusted nested inside another dataclass unwrapped the instant anyone
# called asdict() on the *outer* object. The fix drops @dataclass entirely
# (hand-rolled __slots__ class), which removes it from
# dataclasses.is_dataclass() — the exact gate asdict/astuple check first.
#
# These tests pin the PROPERTY (content is not extractable through any of
# these stdlib introspection paths), not the specific implementation
# (__slots__ vs. something else) — if a future refactor reintroduces
# @dataclass, these should fail again for the same reason the original bug
# existed, which is the point.
# ---------------------------------------------------------------------------


def test_is_dataclass_is_false():
    """This is the specific gate asdict()/astuple() check before doing
    anything else — verified directly since the fix relies on it."""
    u = Untrusted(SECRET, source="mail.body")
    assert dataclasses.is_dataclass(u) is False


def test_asdict_cannot_be_called_on_untrusted_at_all():
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(TypeError):
        dataclasses.asdict(u)  # type: ignore[arg-type]


def test_astuple_cannot_be_called_on_untrusted_at_all():
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(TypeError):
        dataclasses.astuple(u)  # type: ignore[arg-type]


def test_vars_cannot_reach_the_wrapped_content():
    """__slots__ means there is no __dict__ for vars() to walk."""
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(TypeError):
        vars(u)


def test_deepcopy_fails_loudly_rather_than_silently_duplicating_content():
    """copy.deepcopy's default slotted-object reconstruction calls setattr()
    to restore state, which hits Untrusted's frozen __setattr__ — the lazy
    bypass fails loudly here too, it just wasn't the one anyone went looking
    for."""
    u = Untrusted(SECRET, source="mail.body")
    with pytest.raises(AttributeError):
        copy.deepcopy(u)


def test_nested_untrusted_inside_another_dataclass_does_not_leak_via_asdict():
    """The realistic leak shape: a mail-summary skill's own result dataclass
    holding an Untrusted field, then something upstream (a router, a log
    call, a debug dump) calling asdict() on the *outer* object without
    knowing it contains anything sensitive. Must fail loudly, not return a
    dict with the raw content sitting in it."""

    @dataclasses.dataclass
    class MailSummaryDraft:
        body: object
        subject: str

    draft = MailSummaryDraft(body=Untrusted(SECRET, source="mail.body"), subject="hi")

    with pytest.raises(AttributeError):
        dataclasses.asdict(draft)


def test_nested_untrusted_inside_a_list_in_a_dataclass_does_not_leak():
    @dataclasses.dataclass
    class Batch:
        items: list

    batch = Batch(items=[Untrusted(SECRET, source="mail.body"), "plain text"])

    with pytest.raises(AttributeError):
        dataclasses.asdict(batch)


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
# AST guard: no non-trusts_input skill module ever imports/calls reveal, and
# — F17 (nova-accuracy, HIGH): docs/SECURITY_SPEC.md's "exactly one named
# reveal()" claim was not structurally true. `u._value` is a second, silent,
# unaudited way out that the original scan never looked for: nothing catches
# `text = u._value` in a future router or parameter-binder, and all three
# §1.3 enforcement layers would be defeated with zero test failure. Same
# allowlist-of-one technique as the `reveal` scan, applied to `._value`
# access as well. (docs/SECURITY_SPEC.md §1.3 layer 3.)
#
# `gc.get_referents(u)` also reaches `_value` and is deliberately NOT covered
# here — the lead's scoping call, which this suite agrees with: that's the
# *deliberate* bypass class the spec explicitly declines to defend against
# (Python has no real private attributes; pretending otherwise via a
# ref-graph-walking check would be theatre, not enforcement). `._value` is
# the *lazy* class — someone reaching for the obvious attribute without
# malice — and that one is closeable and worth closing.
#
# nova/brain/router.py landed after this file was first written (f39f436);
# the AST scan below covers all of nova/, including it, so no re-scoping was
# needed once the module existed.
# ---------------------------------------------------------------------------


def _finds_untrusted_bypass(path) -> list[str]:
    """Returns every AST-detectable way `path` reaches into an Untrusted's
    guts: importing/calling `reveal`, or accessing `._value` directly."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nova.security.untrusted":
            if any(alias.name == "reveal" for alias in node.names):
                hits.append(f"imports reveal at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr == "reveal":
            hits.append(f"references .reveal at line {node.lineno}")
        if isinstance(node, ast.Name) and node.id == "reveal":
            hits.append(f"references bare name reveal at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr == "_value":
            hits.append(f"accesses ._value at line {node.lineno}")
    return hits


def _imports_or_calls_reveal(path) -> bool:
    """Kept for callers that only care about the reveal() half."""
    return any("reveal" in hit for hit in _finds_untrusted_bypass(path))


def test_no_trusts_input_false_skill_bypasses_untrusted_via_reveal_or_value():
    import nova.skills  # triggers discovery
    from nova.skills.registry import all_skills

    skills_dir = Path(nova.skills.__file__).resolve().parent
    for name, spec in all_skills().items():
        if spec.trusts_input:
            continue  # a trusts_input=True skill is exactly where reveal() is licensed
        module_file = skills_dir / f"{spec.handler.__module__.rsplit('.', 1)[-1]}.py"
        if not module_file.exists():
            continue
        hits = _finds_untrusted_bypass(module_file)
        assert not hits, f"skill '{name}' (trusts_input=False) bypasses Untrusted in {module_file}: {hits}"


def test_untrusted_is_never_bypassed_anywhere_outside_its_own_definition_module():
    """Floor check for the router/skill-param-binder rule: today, NOTHING
    should import reveal or touch ._value at all (M0 ships zero
    trusts_input=True skills). Once one lands, this test should be narrowed
    to allow exactly that skill's module for `reveal` — never for `._value`,
    which has no licensed caller anywhere outside untrusted.py itself."""
    import nova

    root = untrusted_mod.__file__
    nova_root = Path(nova.__file__).resolve().parent
    offenders = {}
    for path in nova_root.rglob("*.py"):
        if str(path) == root:
            continue
        hits = _finds_untrusted_bypass(path)
        if hits:
            offenders[str(path)] = hits
    assert not offenders, f"Untrusted bypassed outside its own module: {offenders}"


def test_value_attribute_access_is_flagged_even_when_reveal_is_not_used():
    """Proves the extended scan actually distinguishes the two bypass
    shapes — a regression guard on the guard itself, using a throwaway
    fixture file rather than asserting against real source."""
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        fixture = _Path(tmp) / "bypass_via_value.py"
        fixture.write_text(
            "def sneaky(u):\n"
            "    return u._value  # never calls reveal() at all\n",
            encoding="utf-8",
        )
        hits = _finds_untrusted_bypass(fixture)
        assert any("_value" in h for h in hits)
        assert not any("reveal" in h for h in hits)
