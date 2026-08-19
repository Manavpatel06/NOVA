"""Thread -> asyncio-loop handoff, guarded against a closed loop (F30).

Every module that owns a background OS thread (a Win32 hook, a blocking
`readline()`/`MessageBox()` call, a `sounddevice` callback, a `Timer`) needs
to hand a result back to the asyncio loop that's waiting for it.
`loop.call_soon_threadsafe()` and `asyncio.run_coroutine_threadsafe()` are
the two sanctioned ways to do that — and both raise `RuntimeError` if the
loop has already closed by the time the background thread gets around to
calling them.

That is not a hypothetical. nova-tester reproduced it 100%: a
`ConsoleConfirmChannel` reader thread wakes ~5s after `NovaApp.stop()` has
already closed the event loop and calls `loop.call_soon_threadsafe(...)`,
which raises **inside that call itself** — before `_settle_result`/
`_settle_exception`'s own `if not fut.done()` guard is ever reached. Two
different races share this code (a future that resolved before the
background thread finishes, and a loop that closed before it finishes); the
done-check only ever covered the first.

Six call sites across five modules had this exact unguarded (or
inconsistently-guarded) shape before this module existed:
`nova.audio.player`, `nova.audio.wake`, `nova.interfaces.hotkey`,
`nova.interfaces.tray`, `nova.security.confirm` (x4). Some already caught
the resulting `RuntimeError` via a broad `except Exception`; none
distinguished "the loop is closed — this is expected shutdown noise" from
"something is actually broken," and none would have survived a second
reviewer asking "does this specific call ever run after the loop it targets
has closed?" the way `nova.security.confirm` did not. One source of truth
(CLAUDE.md) means one place that distinction lives now, not five call sites
each free to re-decide it slightly differently.

**Log level**: a closed-loop drop is logged at DEBUG, not WARNING. It is the
*expected* outcome of a background thread outliving the asyncio loop during
shutdown — every channel/module here already documents "stray input after
the request has settled is silently dropped" as correct behavior, not a
bug, so escalating its emergent cause to WARNING would just be re-describing
the same expected event twice at two severities. But it is not silent
either, on purpose: a fully mute drop is exactly how a *different*, real bug
would hide behind "oh, that's just the known shutdown race" later. DEBUG is
the line between "expected, but on the record" and "actually invisible."
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from nova.log import get_logger

_log = get_logger(__name__)


def post_callback(loop: asyncio.AbstractEventLoop, fn: Callable[..., Any], *args: Any) -> bool:
    """Thread-safe: schedule `fn(*args)` to run on `loop`'s own thread via
    `call_soon_threadsafe`. Call from any thread other than `loop`'s own.

    Returns True if the callback was scheduled, False if `loop` was already
    closed (dropped — see module docstring for the DEBUG-level rationale).
    """
    try:
        loop.call_soon_threadsafe(fn, *args)
        return True
    except RuntimeError as exc:
        _log.debug("post_callback: dropped (event loop is closed): %s", exc)
        return False


def post_coroutine(
    loop: asyncio.AbstractEventLoop, coro: "Coroutine[Any, Any, Any]"
) -> "asyncio.Future[Any] | None":
    """Thread-safe: schedule `coro` to run on `loop` via
    `run_coroutine_threadsafe`. Call from any thread other than `loop`'s own.

    Returns the resulting `Future`, or None if `loop` was already closed
    (dropped — see module docstring). Closes `coro` itself on that path so
    Python doesn't separately warn "coroutine was never awaited" on top of
    the drop this module already logged.
    """
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError as exc:
        _log.debug("post_coroutine: dropped (event loop is closed): %s", exc)
        coro.close()
        return None
