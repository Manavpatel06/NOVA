"""Contract: docs/CONTRACTS.md T0.1 — nova.log.get_logger.

    def get_logger(name: str) -> logging.Logger    # structured, writes data/nova.log

Rules under test:
  - get_logger() works before config is loaded (nova/log.py's own docstring:
    "reads only the path constants in nova/__init__.py, never Config, so
    import order never matters");
  - messages actually land in data/nova.log;
  - repeated calls don't stack duplicate handlers.

data/nova.log's path is not injectable (hardcoded via nova/__init__.py by
contract design — get_logger(name) takes no path parameter), so the
file-writing tests necessarily touch the real, gitignored data/nova.log
rather than a temp file. That's consistent with how every other module will
use it in production.

Two tests here use subprocess isolation (a fresh interpreter) rather than
in-process mocking: pytest imports every test module during collection, so
by the time any test body runs, nova.config and every nova.* logger have
already been imported at least once in this process — which would make an
in-process "works before config is loaded" test trivially pass regardless of
whether the contract actually holds. A subprocess is the only way to observe
a genuinely fresh import order.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import uuid

from nova import LOG_PATH
from nova.log import get_logger


def _tail_contains(path, marker: str) -> bool:
    if not path.exists():
        return False
    return marker in path.read_text(encoding="utf-8", errors="replace")


def test_get_logger_returns_a_logging_logger():
    lg = get_logger("smoke_test_component")
    assert isinstance(lg, logging.Logger)


def test_get_logger_prefixes_bare_names_under_nova():
    lg = get_logger("some_component")
    assert lg.name == "nova.some_component"


def test_get_logger_does_not_double_prefix_already_qualified_names():
    lg = get_logger("nova.audio.capture")
    assert lg.name == "nova.audio.capture"


def test_get_logger_top_level_nova_name_is_unchanged():
    lg = get_logger("nova")
    assert lg.name == "nova"


def test_message_actually_lands_in_data_nova_log():
    marker = f"NOVA_TEST_MARKER_{uuid.uuid4().hex}"
    lg = get_logger("log_write_test")
    lg.warning(marker)
    for handler in lg.handlers or logging.getLogger("nova").handlers:
        handler.flush()
    assert _tail_contains(LOG_PATH, marker), (
        f"expected {marker!r} to appear in {LOG_PATH}, it did not"
    )


def test_repeated_get_logger_calls_do_not_stack_duplicate_handlers():
    """A common logging-setup bug: calling the configuring function on every
    get_logger() invocation and re-attaching handlers each time. Assert the
    'nova' root logger's handler count is stable across repeated calls."""
    get_logger("first_call")
    before = len(logging.getLogger("nova").handlers)
    for i in range(10):
        get_logger(f"repeat_{i}")
    after = len(logging.getLogger("nova").handlers)
    assert after == before, (
        f"handler count grew from {before} to {after} after repeated get_logger() calls"
    )


def test_get_logger_works_in_a_process_that_has_never_imported_config():
    """The core 'safe to call before config is loaded' promise: importing
    and using nova.log must not require, and must not transitively trigger,
    an import of nova.config."""
    marker = f"NOVA_PRECONFIG_MARKER_{uuid.uuid4().hex}"
    script = (
        "import sys\n"
        "assert 'nova.config' not in sys.modules\n"
        "from nova.log import get_logger\n"
        "assert 'nova.config' not in sys.modules, "
        "'importing nova.log pulled in nova.config'\n"
        "lg = get_logger('preconfig_probe')\n"
        f"lg.warning({marker!r})\n"
        "assert 'nova.config' not in sys.modules, "
        "'using get_logger() pulled in nova.config'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(LOG_PATH.parent.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout
    assert _tail_contains(LOG_PATH, marker)


def test_stt_module_loggers_reach_data_nova_log_in_isolation():
    """nova.stt.gpu / nova.stt.transcriber log via bare
    `logging.getLogger("nova.stt.*")` rather than `nova.log.get_logger`. In
    THIS test process that's invisible, because collecting test_bus.py /
    test_config.py already triggered nova.log._configure_root() and
    attached handlers to the shared "nova" logger before this test body
    runs — so nova.stt.gpu's records ride along by ordinary logger
    hierarchy propagation regardless of whether it ever called
    get_logger() itself.

    A fresh process that imports *only* nova.stt (never nova.log, nova.bus,
    nova.audio, or anything else that calls get_logger()) is the only way
    to see the real behavior: verified directly by hand before writing this
    assertion — in isolation, nova.stt.gpu's WARNING never reaches
    data/nova.log at all, because the "nova" logger has no handlers and
    nova.stt.gpu never asked nova.log to add any.

    This is expected to be RED: not because nova.stt hasn't been built, but
    because it doesn't route through the contract's centralized logging
    module. See report for the recommendation (use nova.log.get_logger in
    nova/stt/gpu.py and nova/stt/transcriber.py like nova/audio/*.py does).
    """
    marker = f"NOVA_STT_ISOLATED_MARKER_{uuid.uuid4().hex}"
    script = (
        "import sys\n"
        "assert 'nova.log' not in sys.modules\n"
        "import nova.stt.gpu as g\n"
        f"g.logger.warning({marker!r})\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(LOG_PATH.parent.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert _tail_contains(LOG_PATH, marker), (
        "nova.stt.gpu's logger did not write to data/nova.log when nova.log "
        "was never imported by anything else in the process — its warnings "
        "are silently dropped (beyond a stderr lastResort print) in any "
        "process that only ever touches nova.stt. Every module should log "
        "via nova.log.get_logger(__name__), matching nova/audio/*."
    )
