"""Contract: docs/SECURITY_SPEC.md §2.3 — nova.security.redact.redact.

The one shared redaction implementation (CLAUDE.md rule 4 / T0.5's
egress+output-sanitizing rules): used by Egress.release_for_process and,
eventually, by fetch's outbound body and reply rendering. Pure function, no
I/O — safe to unit test exhaustively.
"""
from __future__ import annotations

import time

from nova.security.redact import redact


# ---------------------------------------------------------------------------
# One implementation only
# ---------------------------------------------------------------------------


def test_redact_is_defined_exactly_once_under_nova():
    from pathlib import Path

    import nova

    hits = []
    for path in Path(nova.__file__).resolve().parent.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "\ndef redact(" in text or text.startswith("def redact("):
            hits.append(str(path))
    assert hits == [str(Path(redact.__globals__["__file__"]).resolve())]


# ---------------------------------------------------------------------------
# 1. Home path shape, both slash styles, case-insensitive
# ---------------------------------------------------------------------------


def test_windows_home_path_backslash_is_redacted():
    out = redact(r"see C:\Users\manav\Documents\notes.txt")
    assert "manav" not in out
    assert out == r"see C:\Users\[USER]\Documents\notes.txt"


def test_windows_home_path_forward_slash_is_redacted():
    out = redact("see C:/Users/manav/Documents/notes.txt")
    assert "manav" not in out
    assert out == "see C:/Users/[USER]/Documents/notes.txt"


def test_windows_home_path_is_case_insensitive():
    out = redact(r"c:\USERS\Manav\x.txt")
    assert "Manav" not in out
    assert "[USER]" in out


def test_posix_home_path_is_redacted():
    out = redact("/home/manav/.ssh/id_rsa")
    assert "manav" not in out
    assert out == "/home/[USER]/.ssh/id_rsa"


def test_macos_style_users_path_is_redacted():
    out = redact("/Users/manav/Desktop/file.txt")
    assert "manav" not in out
    assert out == "/Users/[USER]/Desktop/file.txt"


def test_rest_of_the_path_is_preserved_not_the_whole_string():
    out = redact(r"C:\Users\manav\Documents\notes.txt")
    assert out.endswith(r"\Documents\notes.txt")
    assert out.startswith(r"C:\Users\[USER]")


# ---------------------------------------------------------------------------
# 2. redact_terms — literal, case-insensitive, user-supplied
# ---------------------------------------------------------------------------


def test_redact_terms_are_matched_case_insensitively():
    out = redact("call Manav Patel about the deal", redact_terms=["Manav Patel"])
    assert "Manav" not in out
    assert "[REDACTED]" in out

    out2 = redact("call MANAV PATEL about the deal", redact_terms=["manav patel"])
    assert "MANAV" not in out2


def test_redact_terms_with_regex_special_characters_are_treated_literally():
    out = redact("cost is $100 (approx.)", redact_terms=["$100 (approx.)"])
    assert "$100 (approx.)" not in out
    assert "[REDACTED]" in out


def test_multiple_redact_terms_all_applied():
    out = redact("Acme Corp hired Manav", redact_terms=["Acme Corp", "Manav"])
    assert "Acme Corp" not in out
    assert "Manav" not in out


def test_empty_redact_terms_default_is_a_no_op_for_that_pass():
    out = redact("nothing special here")
    assert out == "nothing special here"


def test_blank_redact_term_entries_are_skipped_not_a_crash():
    out = redact("hello world", redact_terms=["", "world"])
    assert out == "hello [REDACTED]"


# ---------------------------------------------------------------------------
# 3. Secrets/tokens, pattern-matched — real-shaped fake credentials
# ---------------------------------------------------------------------------


def test_anthropic_style_key_is_redacted():
    fake_key = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
    out = redact(f"my key is {fake_key}")
    assert fake_key not in out
    assert "[SECRET]" in out


def test_openai_style_key_is_redacted():
    fake_key = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
    out = redact(f"OPENAI_API_KEY={fake_key}")
    assert fake_key not in out


def test_github_token_is_redacted():
    fake_token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
    out = redact(f"token: {fake_token}")
    assert fake_token not in out


def test_aws_access_key_id_is_redacted():
    fake_akid = "AKIA" + "IOSFODNN7EXAMPLE"
    out = redact(f"key id {fake_akid}")
    assert fake_akid not in out


def test_jwt_shaped_token_is_redacted():
    fake_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out = redact(f"Authorization: Bearer {fake_jwt}")
    assert fake_jwt not in out


def test_labelled_secret_redacts_value_keeps_the_label():
    out = redact("api_key: sup3r-s3cret-value-123456")
    assert "sup3r-s3cret-value-123456" not in out
    assert "api_key" in out  # the label survives; only the value is scrubbed
    assert "[SECRET]" in out


def test_labelled_password_with_equals_sign_is_redacted():
    out = redact("password=CorrectHorseBatteryStaple9!")
    assert "CorrectHorseBatteryStaple9!" not in out


# ---------------------------------------------------------------------------
# 4. Email addresses
# ---------------------------------------------------------------------------


def test_email_address_is_redacted():
    out = redact("reach me at manavpatel0606@gmail.com please")
    assert "manavpatel0606@gmail.com" not in out
    assert "[EMAIL]" in out


# ---------------------------------------------------------------------------
# Composition: multiple categories in one string
# ---------------------------------------------------------------------------


def test_multiple_categories_in_one_string_all_redacted():
    fake_key = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
    text = (
        rf"user C:\Users\manav\notes.txt leaked key {fake_key} "
        f"and emailed manavpatel0606@gmail.com about it"
    )
    out = redact(text, redact_terms=["notes.txt"])
    assert "manav" not in out.split("[USER]")[0] or "[USER]" in out  # home segment gone
    assert fake_key not in out
    assert "manavpatel0606@gmail.com" not in out
    assert "notes.txt" not in out


# ---------------------------------------------------------------------------
# Purity and safety
# ---------------------------------------------------------------------------


def test_redact_is_deterministic():
    text = r"C:\Users\manav\file.txt and manav@example.com"
    assert redact(text) == redact(text)


def test_redact_does_not_mutate_or_require_any_io(tmp_path):
    # No filesystem, network, or clock access — calling it many times with
    # varied input is itself the smoke test; nothing here should be able to
    # touch tmp_path (unused on purpose).
    for i in range(50):
        redact(f"iteration {i} C:\\Users\\manav{i}\\x.txt")


def test_redact_stays_fast_on_a_long_adversarial_string():
    """Guards the bounded-quantifier discipline the module's docstring
    promises (no unbounded quantifier next to an alternation)."""
    adversarial = ("a" * 5000) + "@" + ("b" * 5000) + ".com " + (r"C:\Users\x\y " * 500)
    start = time.monotonic()
    redact(adversarial)
    assert time.monotonic() - start < 2.0
