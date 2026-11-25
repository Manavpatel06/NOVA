"""Where NOVA keeps Manav's Anthropic API key: Windows Credential Manager
(generic credential "NOVA/anthropic-api-key"), encrypted for his Windows
account by DPAPI -- never in a file in the repo, never in config, never in
the log. Set it once with scripts/set_api_key.py.
"""
from __future__ import annotations

from nova.log import get_logger

_log = get_logger("brain.keys")
TARGET = "NOVA/anthropic-api-key"


def get_api_key() -> str | None:
    """The stored key, or None. Windows: Credential Manager. The hub (Linux,
    ADR 0026): data/anthropic_key, readable only by Manav's account (0600),
    written by `nova-hub apikey`; any looser permissions and it isn't used."""
    import sys

    if sys.platform != "win32":
        return _file_key()
    try:
        import win32cred  # pywin32
    except ImportError:
        return None
    try:
        cred = win32cred.CredRead(TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception:  # noqa: BLE001 -- "not found" is pywintypes.error 1168
        return None
    blob = cred.get("CredentialBlob") or b""
    key = blob.decode("utf-16-le") if isinstance(blob, bytes) else str(blob)
    key = key.strip().strip("\x00")
    return key or None


def set_api_key(key: str) -> None:
    import win32cred

    key = key.strip()
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        raise ValueError("that doesn't look like an Anthropic API key (it starts with sk-ant-)")
    win32cred.CredWrite({
        "Type": win32cred.CRED_TYPE_GENERIC,
        "TargetName": TARGET,
        "UserName": "nova",
        "CredentialBlob": key,
        "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        "Comment": "NOVA voice assistant -- Claude API key",
    }, 0)
    _log.info("Anthropic API key stored in Windows Credential Manager (%s)", TARGET)


def delete_api_key() -> None:
    import win32cred

    win32cred.CredDelete(TARGET, win32cred.CRED_TYPE_GENERIC, 0)


def key_path():
    from nova import DATA_DIR

    return DATA_DIR / "anthropic_key"


def _file_key() -> str | None:
    import os
    import stat

    path = key_path()
    try:
        st = path.stat()
    except OSError:
        return None
    if stat.S_IMODE(st.st_mode) & 0o077 or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        _log.warning("Not using %s: other accounts can read it (run: chmod 600 %s)", path, path)
        return None
    key = path.read_text(encoding="utf-8").strip()
    return key if key.startswith("sk-ant-") and not any(c.isspace() for c in key) else None


def set_file_key(key: str) -> None:
    """The hub's key file, created 0600 (nova-hub apikey)."""
    import os

    key = key.strip()
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        raise ValueError("that doesn't look like an Anthropic API key (it starts with sk-ant-)")
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    os.chmod(path, 0o600)
