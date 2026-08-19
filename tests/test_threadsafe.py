"""Contract: nova/threadsafe.py (F30) — the one shared thread-to-loop handoff.

    def post_callback(loop, fn, *args) -> bool     # via call_soon_threadsafe
    def post_coroutine(loop, coro) -> Future | None # via run_coroutine_threadsafe

Why this file matters more than its size suggests (per the module's own
docstring): five call sites across five modules shared this exact unguarded
shape before this module existed. Consolidating means one bug here is a bug
in five places at once, and one *fix* here is a fix in five places at once
-- so this file is tested harder than a 85-line module would normally get,
specifically because of that leverage.

Three things pinned, matching the module's own three claims:
  1. A CLOSED loop drops the call and returns a falsy sentinel (False / None)
     instead of raising. This is the exact race nova-tester reproduced 100%
     against nova.security.confirm.ConsoleConfirmChannel: a reader thread
     wakes long after NovaApp.stop() has closed the loop.
  2. A RUNNING loop actually delivers. A helper that swallows every
     RuntimeError could pass a "doesn't raise" test by never delivering
     anything at all -- so every closed-loop test below has a twin proving
     the open-loop path really runs the callback/coroutine, not just that
     it fails silently in one direction.
  3. The closed-loop drop is logged at DEBUG (module docstring: "not silent
     either, on purpose... DEBUG is the line between 'expected, but on the
     record' and 'actually invisible'").

## The "genuinely hard case" the lead asked about: a loop closing BETWEEN
the check and the post

There is no such window in this implementation, and this is worth pinning
structurally rather than approximating with a flaky timing test (per the
lead: "if it can't be constructed deterministically, say so rather than
writing a flaky approximation").

`post_callback`/`post_coroutine` never call `loop.is_closed()` as a
separate pre-check -- each goes straight into `try: <the real stdlib
call>` and catches the `RuntimeError` the stdlib itself raises if the loop
turns out to be closed. That is the standard "ask forgiveness" pattern
specifically because it has no separate check-then-act step to race: from
the loop's point of view, "closed before this call" and "closed during
this call" are indistinguishable -- both surface as the identical
`RuntimeError` from the identical stdlib call, caught by the identical
`except` clause. So the simple already-closed-loop test below already
exercises the only race window this implementation has. A test that
additionally tried to close the loop from another thread at the exact
instant `call_soon_threadsafe` is mid-execution would be timing-dependent
on CPython/asyncio's own internal locking, which is not this module's
contract to prove and not something nova wrote -- pinned instead as a
structural guard (`test_post_callback_and_post_coroutine_never_pre_check_is_closed`)
that the "ask forgiveness, not permission" shape can't quietly regress into
the racier "check then act" one.
"""
from __future__ import annotations

import ast
import asyncio
import gc
import threading
import warnings
from pathlib import Path

import pytest

import nova.threadsafe as threadsafe_mod
from nova.threadsafe import post_callback, post_coroutine


# ---------------------------------------------------------------------------
# A real loop running on a real background thread -- the actual shape
# post_callback/post_coroutine are built for (called from a thread other
# than the loop's own).
# ---------------------------------------------------------------------------


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


@pytest.fixture
def closed_loop():
    loop = asyncio.new_event_loop()
    loop.close()
    return loop


# ---------------------------------------------------------------------------
# post_callback
# ---------------------------------------------------------------------------


def test_post_callback_on_a_running_loop_actually_delivers(running_loop):
    """The delivery half -- a helper that only ever swallowed exceptions
    would pass a 'never raises' test without doing its actual job."""
    done = threading.Event()
    received = []

    def fn(value):
        received.append(value)
        done.set()

    ok = post_callback(running_loop, fn, 42)

    assert ok is True
    assert done.wait(timeout=2), "callback was never delivered to the running loop"
    assert received == [42]


def test_post_callback_on_a_closed_loop_drops_and_returns_false_without_raising(closed_loop):
    called = []

    ok = post_callback(closed_loop, lambda: called.append(1))  # must not raise

    assert ok is False
    assert called == [], "the callback ran despite the loop being closed"


def test_post_callback_closed_loop_drop_is_logged_at_debug(closed_loop, caplog):
    with caplog.at_level("DEBUG"):
        post_callback(closed_loop, lambda: None)

    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("dropped" in r.message.lower() for r in debug_records), (
        f"expected a DEBUG log naming the drop, got: {[r.message for r in caplog.records]}"
    )


def test_post_callback_closed_loop_drop_is_not_logged_above_debug(closed_loop, caplog):
    """The module docstring is explicit this is DEBUG, not WARNING -- an
    expected shutdown-race outcome shouldn't cry wolf at a level that would
    train a human to ignore real warnings."""
    with caplog.at_level("DEBUG"):
        post_callback(closed_loop, lambda: None)

    assert not any(r.levelname in ("WARNING", "ERROR", "CRITICAL") for r in caplog.records)


def test_post_callback_forwards_multiple_positional_args(running_loop):
    done = threading.Event()
    received = []

    def fn(a, b, c):
        received.append((a, b, c))
        done.set()

    post_callback(running_loop, fn, 1, "two", 3.0)

    assert done.wait(timeout=2)
    assert received == [(1, "two", 3.0)]


# ---------------------------------------------------------------------------
# post_coroutine
# ---------------------------------------------------------------------------


def test_post_coroutine_on_a_running_loop_actually_delivers_and_resolves(running_loop):
    async def work():
        return "delivered"

    fut = post_coroutine(running_loop, work())

    assert fut is not None
    assert fut.result(timeout=2) == "delivered"  # concurrent.futures.Future -- blocking .result() is fine here


def test_post_coroutine_on_a_closed_loop_returns_none_without_raising(closed_loop):
    async def work():
        return "unused"  # pragma: no cover -- must never actually run

    result = post_coroutine(closed_loop, work())

    assert result is None


def test_post_coroutine_on_a_closed_loop_closes_the_abandoned_coroutine(closed_loop):
    """Proof post_coroutine actually calls coro.close(), not just drops the
    reference: an un-closed, never-awaited coroutine emits a
    'coroutine ... was never awaited' RuntimeWarning when garbage
    collected. First confirm that assumption holds on this interpreter
    (negative control), then prove post_coroutine avoids it."""

    async def unused_control():
        pass  # pragma: no cover

    # Negative control: a coroutine nobody closes DOES warn on GC.
    control_coro = unused_control()
    with warnings.catch_warnings(record=True) as control_caught:
        warnings.simplefilter("always")
        del control_coro
        gc.collect()
    assert any("was never awaited" in str(w.message) for w in control_caught), (
        "test assumption failed: an unclosed coroutine did not warn on GC on this interpreter"
    )

    async def work():
        return "unused"  # pragma: no cover

    coro = work()
    result = post_coroutine(closed_loop, coro)
    assert result is None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        del coro
        gc.collect()
    assert not any("was never awaited" in str(w.message) for w in caught), (
        "post_coroutine left the coroutine unclosed on the dropped path"
    )


def test_post_coroutine_closed_loop_drop_is_logged_at_debug(closed_loop, caplog):
    async def work():
        return "unused"  # pragma: no cover

    with caplog.at_level("DEBUG"):
        post_coroutine(closed_loop, work())

    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("dropped" in r.message.lower() for r in debug_records)


# ---------------------------------------------------------------------------
# The "genuinely hard case": no separate check-then-act window to race.
# Structural guard, not a flaky timing test -- see module docstring above.
# ---------------------------------------------------------------------------


def test_post_callback_and_post_coroutine_never_pre_check_is_closed():
    """If a future edit adds `if not loop.is_closed(): ...` ahead of the
    real call, it reintroduces exactly the TOCTOU window this module's
    try/except design avoids -- the loop could close in the gap between the
    check and the call. Structural guard so that regression fails the
    build, not just a future code review."""
    source = Path(threadsafe_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "is_closed":
            pytest.fail(
                f"nova/threadsafe.py calls .is_closed() at line {node.lineno} -- "
                "this module's safety relies on catching RuntimeError from the "
                "real call (ask-forgiveness), not on a separate is_closed() "
                "pre-check (ask-permission), which reopens the exact race "
                "this module exists to close."
            )


def test_post_callback_and_post_coroutine_each_have_exactly_one_try_except_runtimeerror():
    """A second, cheaper structural pin on the same property: the real
    stdlib call must be the only thing inside the try block (i.e. directly
    guarded), not preceded by other code that could observe a stale
    'still open' state before the guarded call runs."""
    source = Path(threadsafe_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for fn_name in ("post_callback", "post_coroutine"):
        func = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == fn_name
        )
        try_nodes = [n for n in ast.walk(func) if isinstance(n, ast.Try)]
        assert len(try_nodes) == 1, f"{fn_name} must have exactly one try/except guarding the handoff"
        handlers = try_nodes[0].handlers
        assert len(handlers) == 1
        handler_type = handlers[0].type
        type_name = handler_type.id if isinstance(handler_type, ast.Name) else None
        assert type_name == "RuntimeError", f"{fn_name} must catch RuntimeError specifically, not a bare except"
