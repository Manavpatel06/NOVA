#!/usr/bin/env python
"""Store your Anthropic API key for NOVA (Windows Credential Manager).

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\set_api_key.py

The key is typed hidden, checked with one tiny request to Claude, and kept
encrypted for your Windows account. It is never written to a file or log.
Run with --remove to delete it.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nova.brain import keys  # noqa: E402


def main() -> int:
    if "--remove" in sys.argv:
        keys.delete_api_key()
        print("Removed.")
        return 0
    print("Paste your Anthropic API key (from console.anthropic.com > API keys).")
    print("It won't show while you paste. Then press Enter.")
    key = getpass.getpass("API key: ")
    try:
        keys.set_api_key(key)
    except ValueError as exc:
        print(f"Not saved: {exc}")
        return 1
    print("Saved. Checking it with Claude...")
    from nova.brain.agent import check_key

    ok, message = check_key()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
