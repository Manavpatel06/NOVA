"""Structured logging for NOVA.

Every module gets its logger via get_logger(__name__)-style calls. Safe to
call before config is loaded: this module reads only the path constants in
nova/__init__.py, never Config, so import order never matters.
"""

from __future__ import annotations

import logging
import sys

from nova import DATA_DIR, LOG_PATH

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_root_configured = False


def _configure_root() -> None:
    """Attach the file + console handlers to the shared 'nova' logger tree.

    Idempotent: safe to call from every get_logger() invocation.
    """
    global _root_configured
    if _root_configured:
        return

    # data/ is gitignored and may not exist yet on a fresh checkout.
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("nova")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    _root_configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the 'nova' tree, configured with file + console
    handlers on first use.

    `name` is typically a module's __name__ (e.g. "nova.audio.capture") or a
    short component name (e.g. "bus") — both are normalized to live under
    "nova." so they share the handlers configured above.
    """
    _configure_root()
    if name == "nova" or name.startswith("nova."):
        return logging.getLogger(name)
    return logging.getLogger(f"nova.{name}")
