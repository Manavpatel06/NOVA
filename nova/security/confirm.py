"""CONFIRM approval channels (M1 groundwork, docs/THREAT_MODEL.md §1.4,
docs/CONTRACTS.md T0.5's `Ladder`).

Why this exists now even though M0 ships zero CONFIRM skills: the retired
build's CONFIRM tier was quietly unapprovable for most of its life — the
approval dialog existed in code but was never once exercised against a
human clicking anything, because the dev box had no display, and by the
time anyone noticed, "build me an app" (the tier's headline feature) had
been unreachable for most of the project. `Ladder.authorize()` already
fails closed with no channel registered (`nova/security/ladder.py`); this
module is what M1 plugs into that seam so CONFIRM becomes *usable*, not
just safely inert.

Two implementations:

- `ScriptedConfirmChannel` — deterministic, no terminal, no clock. What
  nova-tester drives to prove the ladder's CONFIRM branches (approve,
  decline, timeout, broken-channel) without a human anywhere near the loop.
- `ConsoleConfirmChannel` — the honest starting point that actually runs on
  this machine today: prints what's being asked, reads a real line from
  stdin with a real wall-clock timeout. A tray/dialog channel arrives with
  M1's tray work and drops in beside this one without the ladder changing
  at all — both are just objects with an async `request(skill, params)`
  method (`nova.security.ladder.ConfirmChannel`, a `Protocol` — no
  inheritance required, structural typing only).

**What the prompt shows** (docs/THREAT_MODEL.md §2.2.6, nova-security's
recommendation, adopted by the lead): consent has to be to a specific
disclosure, not a generic "allow this?" toggle. Both channels here render a
*redacted preview* of `params` — every string value (recursively, through
nested dicts/lists) passed through `nova.security.redact.redact()` before
it is ever shown — so what a human approves is the actual thing that would
be disclosed, not a category. This is a floor, not a ceiling: a skill whose
real egress payload is assembled *inside* its handler (e.g. by reading a
file after CONFIRM is granted) is responsible for its own additional
disclosure at that point; `params` is only what the ladder can see before
the handler runs.

**Timeout vs decline**: a channel that can't get a clear answer within its
own time budget raises `nova.security.ladder.ConfirmTimeout` rather than
returning `False`, so the ladder audits `"confirm_timed_out"` distinctly
from `"confirm_declined"` — the two mean different things to whoever reads
the audit log later.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any

from nova.log import get_logger
from nova.security.ladder import ConfirmTimeout
from nova.security.redact import redact

if TYPE_CHECKING:
    from nova.skills.registry import SkillSpec

logger = get_logger("security.confirm")

#: What counts as an explicit approval on the console channel. Anything else
#: (including empty input, EOF, or a stray keypress) is a decline — an
#: unclear answer must never read as approval.
_APPROVE_WORDS = {"y", "yes"}


def _redact_preview(value: Any, redact_terms: tuple[str, ...]) -> Any:
    """Recursively redact every string leaf in `value` (dicts/lists/tuples
    walked, everything else passed through as-is) so a CONFIRM prompt can
    show the actual params a skill was invoked with, safely."""
    if isinstance(value, str):
        return redact(value, redact_terms=redact_terms)
    if isinstance(value, dict):
        return {k: _redact_preview(v, redact_terms) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_preview(v, redact_terms) for v in value)
    return value


def format_confirm_prompt(skill: "SkillSpec", params: dict[str, Any], *, redact_terms: tuple[str, ...] = ()) -> str:
    """The exact text both channels show — one shared implementation so a
    console prompt and a future tray/dialog prompt never drift apart on
    what counts as 'the actual thing being disclosed'."""
    preview = _redact_preview(params, redact_terms)
    return (
        f"NOVA wants to run: {skill.name} (tier=CONFIRM)\n"
        f"  with: {preview!r}\n"
        f"This is the redacted text/params that would actually be used — nothing more is disclosed."
    )


# ---------------------------------------------------------------------------
# ScriptedConfirmChannel — for nova-tester and CI, no human, no clock.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmCall:
    """One recorded call to a channel's request() — what nova-tester asserts
    against to prove the channel (and, upstream, the ladder) was actually
    invoked, not just configured."""

    skill_name: str
    params: dict[str, Any]


class ScriptedConfirmChannel:
    """Deterministic `ConfirmChannel`: never touches a real terminal or a
    real clock. Configured with a script — a sequence of outcomes returned
    in order, one per call. An outcome is `True` (approve), `False`
    (decline), the sentinel `TIMEOUT` (raises `ConfirmTimeout`), or a live
    exception instance (raised as-is, simulating a broken channel).

    Every call is recorded in `.calls` regardless of outcome, so a test can
    assert the ladder actually reached the channel with the right
    skill/params — not just that the final `Decision` looked right, which
    could pass even if the seam were unwired entirely.
    """

    TIMEOUT = object()  # sentinel — distinct from any real bool/exception value

    def __init__(self, script: list[bool | BaseException | object]) -> None:
        self._script = list(script)
        self._pos = 0
        self.calls: list[ConfirmCall] = []

    async def request(self, skill: "SkillSpec", params: dict[str, Any]) -> bool:
        self.calls.append(ConfirmCall(skill_name=skill.name, params=dict(params)))
        if self._pos >= len(self._script):
            raise RuntimeError(
                f"ScriptedConfirmChannel: script exhausted after {self._pos} call(s) "
                f"— add more entries if the test expects another request()"
            )
        outcome = self._script[self._pos]
        self._pos += 1

        if outcome is self.TIMEOUT:
            raise ConfirmTimeout(f"ScriptedConfirmChannel: scripted timeout for skill '{skill.name}'")
        if isinstance(outcome, BaseException):
            raise outcome
        return bool(outcome)


# ---------------------------------------------------------------------------
# ConsoleConfirmChannel — real stdin/stdout, works on this machine today.
# ---------------------------------------------------------------------------


class ConsoleConfirmChannel:
    """The honest starting point: prints the redacted request to a stream
    (real stdout by default) and reads one line from a stream (real stdin
    by default) with a real wall-clock timeout.

    Known limitation, documented rather than hidden: reading a line is a
    blocking call, run on a dedicated `daemon=True` thread — deliberately
    *not* `loop.run_in_executor(None, ...)`, whose default
    `ThreadPoolExecutor` registers an `atexit` hook that joins every worker
    thread before the interpreter is allowed to exit. Tried that first: a
    timed-out confirm left a thread blocked on `readline()` forever, and the
    whole process hung on shutdown waiting for it — a real, reproduced hang,
    not a hypothetical. A daemon thread is abandoned at interpreter exit
    instead of joined, so a timeout here costs one dangling thread (whose
    eventual stray input is silently dropped), never a frozen process. A
    future tray/dialog channel (M1) doesn't share this limitation at all,
    since it isn't reading a blocking stream.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 60.0,
        redact_terms: tuple[str, ...] = (),
        stream_out: IO[str] | None = None,
        stream_in: IO[str] | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._redact_terms = redact_terms
        self._out = stream_out if stream_out is not None else sys.stdout
        self._in = stream_in if stream_in is not None else sys.stdin

    async def request(self, skill: "SkillSpec", params: dict[str, Any]) -> bool:
        prompt = format_confirm_prompt(skill, params, redact_terms=self._redact_terms)
        self._out.write(f"\n{prompt}\nApprove? [y/N] (auto-deny in {self._timeout_s:.0f}s): ")
        self._out.flush()

        loop = asyncio.get_running_loop()
        result: asyncio.Future[str] = loop.create_future()

        def _read_one_line() -> None:
            try:
                line = self._in.readline()
            except Exception as exc:  # noqa: BLE001 — hand any read failure back to the awaiting side
                loop.call_soon_threadsafe(_settle_exception, result, exc)
                return
            loop.call_soon_threadsafe(_settle_result, result, line)

        threading.Thread(target=_read_one_line, daemon=True, name="nova-confirm-console-readline").start()

        try:
            line = await asyncio.wait_for(result, timeout=self._timeout_s)
        except asyncio.TimeoutError as exc:
            self._out.write("\n(no response in time — denying)\n")
            self._out.flush()
            raise ConfirmTimeout(
                f"ConsoleConfirmChannel: no answer for skill '{skill.name}' within {self._timeout_s:.0f}s"
            ) from exc

        answer = line.strip().lower()
        approved = answer in _APPROVE_WORDS
        self._out.write(f"-> {'approved' if approved else 'declined'}\n")
        self._out.flush()
        return approved


def _settle_result(fut: "asyncio.Future[str]", value: str) -> None:
    # The event-loop thread's side of the handoff. `wait_for`'s timeout may
    # already have cancelled `fut` by the time the reader thread finishes —
    # guard against "InvalidStateError: invalid state" on a done future.
    if not fut.done():
        fut.set_result(value)


def _settle_exception(fut: "asyncio.Future[str]", exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
