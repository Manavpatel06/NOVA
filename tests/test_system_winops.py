"""nova.system.winops -- the validation that runs before anything reaches Windows."""
from __future__ import annotations

import pytest

from nova.system import winops


@pytest.mark.parametrize("url", [
    "https://www.google.com/search?q=cats",
    "https://github.com",
    "https://youtube.com/results?search_query=lofi%20beats",
])
def test_safe_https_url_accepts_plain_https(url):
    assert winops.safe_https_url(url) == url


@pytest.mark.parametrize("url", [
    "http://google.com", "file:///C:/Windows", "https://user:pw@evil.com", "https://127.0.0.1/",
    "https://localhost/", "https://printer.local/", "\\\\server\\share", "https://evil.com\\@x",
    "https://exa mple.com", "javascript:alert(1)", "https://evil.com:8080/", "ms-settings:display",
    "https://evil.com/\npath", "",
])
def test_safe_https_url_refuses_everything_else(url):
    assert winops.safe_https_url(url) is None


def test_clean_arg_refuses_control_chars_and_long_values():
    assert winops._clean_arg("LEVEL", 40) == "40"
    with pytest.raises(ValueError):
        winops._clean_arg("APP", "a\nb")
    with pytest.raises(ValueError):
        winops._clean_arg("APP", "x" * 400)
    with pytest.raises(ValueError):
        winops._clean_arg("bad name", "x")


def test_everything_raises_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(winops.sys, "platform", "linux")
    for call in (lambda: winops.powershell("Get-Date"), winops.lock_workstation, winops.sleep_computer,
                 lambda: winops.press_keys("volume_up"), lambda: winops.open_url("https://github.com"),
                 lambda: winops.run_program("shutdown.exe", "/a")):
        with pytest.raises(winops.WinOpsUnavailable):
            call()


def test_run_program_refuses_paths(monkeypatch):
    monkeypatch.setattr(winops.sys, "platform", "win32")
    for bad in ("..\\evil.exe", "C:\\x\\y.exe", "cmd", "a b.exe"):
        with pytest.raises(ValueError):
            winops.run_program(bad)


@pytest.mark.parametrize("url", [
    "https://0x7f.0x0.0x0.0x1/", "https://0x7f.1/", "https://foo.0x1/", "https://router.192/admin",
])
def test_safe_https_url_refuses_hosts_a_browser_would_read_as_ipv4(url):
    """Security review 2026-10-15: browsers parse a host whose last label is
    decimal or 0x-hex as an IPv4 address ("0x7f.0x0.0x0.0x1" -> 127.0.0.1),
    which slipped past the digits-and-dots check."""
    assert winops.safe_https_url(url) is None


def test_safe_https_url_still_accepts_hex_looking_names_that_are_not_last():
    assert winops.safe_https_url("https://0xdeadbeef.example.com/") == "https://0xdeadbeef.example.com/"
