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
    """The stored key, or None (not set, or not on Windows)."""
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
