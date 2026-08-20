"""T2.4a — the Claude invocation adapter, standalone (`docs/M2_OPEN_BRAIN_PLAN.md`
T2.4, `docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md`).

This is **only** the piece that spawns `claude`, sends it a prompt, and
parses back a structured `ClaudeAnswer` / `ClaudeProposal` / `ClaudeFailure`.
It does **not** call `Router`, `Ladder`, or `SkillCtx` — that wiring is
blocked behind F46 (CONFIRM must show the resolved thing, not a friendly
name) and F50 (params must bind against the handler's real signature before
`authorize()` runs), per the lead's explicit scoping of this task. Nothing
in this module assumes those findings are fixed; the caller that eventually
wires this into the turn loop is responsible for treating `ClaudeProposal`
as untrusted input to the *real* binder/ladder, exactly like a router match
— this module proves nothing about whether `skill_name` names a real skill
or `params` binds against its handler. That validation is `SkillSpec.
invoke()`'s job, unchanged.

## Finding 1 (the load-bearing one): does the spawned process have tools?

**Decided by the lead (F44): no.** This module structurally removes
tool-use from the child rather than relying on the model's own judgment or
on the permission-check layer to refuse a bad request. Verified empirically
against a real subprocess, not configured and assumed:

1. **`--tools ""` structurally removes tool availability.** A/B test, same
   prompt ("create marker.txt with your Write tool"), same process,
   different flag only:
   - `--tools ""`: the model's final text claimed "I'll create the
     file..." — but **zero tool calls were attempted** (`permission_denials`
     empty, not "denied" — *never reached the permission layer at all*) and
     **no file appeared on disk.**
   - default tools (no `--tools` flag): the model **did** attempt a real
     `Write` tool call — visible as an entry in `permission_denials` naming
     `tool_name: "Write"` and the exact `file_path`/`content` it tried to
     write — which the headless permission system then denied (no
     `--dangerously-skip-permissions` passed, ever). No file appeared
     either, but *because a second, weaker layer caught it*, not because
     nothing was attempted.

   Same prompt, same process, only `--tools` differs — and the observable
   difference (an attempted-and-denied tool call vs. no attempt at all) is
   the actual behavioral proof that `--tools ""` removes the capability at
   the model's tool schema, one layer earlier than the permission system.

2. **A hostile/injected prompt asking the child to run Bash and Write real
   files** (`echo PWNED > pwned_by_bash.txt`, a Write-tool file, `whoami`
   redirected to a file) **produced zero new files in the working
   directory**, `permission_denials: []`, and a final text response
   correctly identifying the prompt as an injection attempt.

3. **The model's own self-report about its tools is not trustworthy
   evidence and must never be treated as such.** Asked directly "list every
   tool you have access to" under `--tools ""`, the model *fabricated* a
   plausible-looking list (Bash, PowerShell, Read, Edit, Write, Glob, Grep,
   WebFetch, WebSearch, TodoWrite, Task, NotebookEdit) — longer than the
   list it gave under default tools. This is why finding 1 above is proven
   by *observable side effects* (files on disk, `permission_denials`
   entries naming a real attempted tool) and never by asking the model what
   it thinks it can do.

`scripts/probe_claude_arm.py` reproduces all of this against the real CLI on
demand; it is not a pytest test (nova-tester owns `tests/`), but it is the
same methodology `docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md` finding 1 asked
for: "spawn the actual child with a prompt engineered to try to write a file
or run a shell command, and assert nothing happens on disk and nothing
runs."

## What else this module does, per the threat model's finding 6

- **Prompt text reaches the child on stdin, never argv** — via
  `Egress.release_for_process()`, the one sanctioned redact-then-release
  call, so the text is redacted and audited before it ever reaches
  `communicate(input=...)`.
- **`argv[0]` resolves through `find_cli()`** (`shutil.which`, verified to
  exist), never a raw config/parameter string — per
  `docs/SECURITY_SPEC.md` §3.2's reserved allowlist row.
- **Env discipline (F59), narrowly scoped by nova-security's ruling.**
  `docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md` finding 8 asks for
  `ANTHROPIC_API_KEY` / `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX`
  to be scrubbed from the child's environment, so nothing accidentally
  reroutes off Manav's Max-plan OAuth session onto metered API/Bedrock/
  Vertex billing. This module's first attempt skipped the scrub entirely
  because implementing it means reading `os.environ`, and `tests/
  test_source_hygiene.py::test_module_does_not_read_os_environ` is a
  zero-exemption, repo-wide static tripwire on exactly that. The lead
  proposed exempting this whole file; nova-security agreed with the
  reasoning (containing a child's environment is not the same category as
  reading config-by-environment) but rejected that scope as too wide —
  *"nothing would stop that file from later reading an unrelated var and
  using its value."* `_child_env()` is the ruling's actual shape: it touches
  only **presence/absence** of the closed, literal `_ENV_VARS_TO_SCRUB` set
  via `dict.pop(name, None)`, never reads or branches on what any variable
  is *set to*, and is the only function in this file that names
  `os.environ` at all — an unrelated var added here later is a visible,
  reviewable diff to one five-line function, not a capability the whole
  file already had. `tests/test_source_hygiene.py` gets a matching
  `ENVIRON_ALLOWLIST` entry (size-pinned at one) plus a structural test
  constraining *how* this file touches the environment — nova-tester's
  file, not edited here. Never `--bare` regardless: its own help text says
  it *forces* `ANTHROPIC_API_KEY`/`apiKeyHelper` auth and never reads
  OAuth/keychain, which would break the $0-recurring assumption outright.
  `--safe-mode` is used instead — disables CLAUDE.md/hooks/plugins/MCP/
  custom commands/skills while leaving normal OAuth auth and built-in tools
  (which `--tools ""` then removes) alone.
- **F61: the resolved CLI path is audited on every invocation.** `find_cli()`
  trusts PATH search order with no containment check — nova-security
  explicitly did not want an `open_app`-style curated-directory allowlist
  here (right for Program Files, wrong for an npm-installed tool a global
  `npm install` can relocate), and instead asked for the resolved path to be
  logged so a PATH-order swap (something else named `claude`/`claude.cmd`
  planted earlier on PATH) is visible after the fact rather than never.
  `ask()` records a `claude_arm.invoke` audit event with the resolved
  `cli_path` and `model` immediately after `find_cli()` succeeds, before
  `release_for_process()` or the spawn — via the caller-supplied `audit:
  AuditLog`, kept as its own parameter rather than reached through `egress`
  (which does not expose its internal `AuditLog` publicly), so this module
  adds no new surface to `nova/security/egress.py`.
- **Kill, not abandon, on timeout (F49).** The confirm channels' pattern of
  abandoning a `daemon=True` thread on timeout is correct for a blocked
  `readline()` with no side effects and wrong for a live child process.
  `_run_claude()` wraps the round trip in `asyncio.wait_for`; on timeout it
  calls `proc.kill()` immediately, then `taskkill /PID <pid> /T /F`
  unconditionally as a process-*tree* sweep (Windows: a plain `kill()` does
  not reliably reach grandchildren, though `--tools ""` should mean there
  are none). Verified against a real subprocess with a deliberately tiny
  timeout: `proc.kill()` alone was sufficient in practice (the PID was
  already gone by the time `taskkill` ran), but `proc.wait()` *after*
  `kill()` did not return within 5s even though the process was verifiably
  dead (`tasklist` confirmed) — a known Windows/`ProactorEventLoop`
  quirk, not evidence the kill failed. This module does not treat a
  post-kill `wait()` timeout as a kill failure; it logs and proceeds,
  because `taskkill`'s own exit code is the thing actually trusted to
  confirm the process is gone.
- **Never `--dangerously-skip-permissions` / `--allow-dangerously-skip-
  permissions`.** Not in the argv this module builds, ever — there is no
  parameter or code path that can add either flag.
- **A response that fails to parse as `answer(text)` or
  `propose(skill_name, params)` is a `ClaudeFailure`, never partially or
  best-effort acted on.** `--json-schema` constrains the model's own output
  to that exact shape (verified: even an injection-refusal comes back as
  `{"kind":"answer","text":"..."}`, not free text) but this module still
  independently re-validates the parsed JSON shape rather than trusting the
  schema flag alone — `--json-schema` is the model's constraint, not this
  process's.
- **No automatic retry.** One attempt per `ask()` call; a `ClaudeFailure`
  is the caller's signal to speak a plain failure, not to loop.
- **Missing binary is a `ClaudeFailure`, not a silent local fallback.**
  `find_cli()` raising is caught inside `ask()` and turned into
  `ClaudeFailure(reason="cli_not_found", ...)` before any subprocess is
  spawned — never a fallthrough to guessing with the router's second-best
  score (`docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md` finding 6's confidence-
  honesty point).

## What this module deliberately does not do

- Does not decide *when* to call Claude (router/confidence-floor logic —
  the caller's job, still blocked on the tier-floor mechanism, finding 2).
- Does not validate `skill_name` against the live registry or bind `params`
  against a handler's signature — `SkillSpec.invoke()` does that, unchanged,
  for every caller alike.
- Does not maintain conversation history across calls (`--no-session-
  persistence`, one-shot `-p` invocation every time) — `docs/
  THREAT_MODEL_CLAUDE_FALLTHROUGH.md` finding 5's "no conversation history"
  invariant, kept structurally rather than by discipline.
- Does not run `claude` in a mode with its own agentic loop against this
  repository or any other directory — no `--add-dir`, no working directory
  passed beyond the OS default, `--tools ""` removes every tool that would
  make a cwd relevant in the first place.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from nova.log import get_logger
from nova.security.audit import AuditLog
from nova.security.egress import Egress

_log = get_logger(__name__)

#: Round-trip wall-clock budget. The plan states Claude fallthrough "gets
#: its own latency budget" (T2.2) -- 30s is a starting placeholder, not a
#: measured figure; nothing in this module claims otherwise.
DEFAULT_TIMEOUT_S = 30.0

#: A small, fast model by default -- this is meant to be a low-latency
#: fallback path, not a deep-reasoning call. Callers may override.
DEFAULT_MODEL = "sonnet"

#: Wall-clock budget for the post-kill wait() -- see the module docstring's
#: F49 section for why a timeout here is not treated as "kill failed".
_POST_KILL_WAIT_S = 5.0

_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["answer", "propose"]},
        "text": {"type": "string"},
        "skill_name": {"type": "string"},
        "params": {"type": "object"},
    },
    "required": ["kind"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are NOVA's fallthrough brain. Respond ONLY with JSON matching the "
    'required schema. Use kind="answer" with a "text" field for a direct '
    'spoken answer -- including to explain a refusal or decline. Use '
    'kind="propose" with "skill_name" and "params" fields to request a call '
    "to one of the registered skills described in the user message. Never "
    "propose a skill_name that was not explicitly listed as available in "
    "the user message -- an unlisted skill_name will be refused by the "
    "caller regardless of what you propose, so proposing one accomplishes "
    "nothing. You have no tools of your own: you cannot read files, run "
    "commands, or access the network. Your only output is the answer/"
    "propose JSON."
)


class ClaudeCliNotFound(RuntimeError):
    """`claude` was not found on PATH. Raised by `find_cli()`, caught
    inside `ask()` and turned into a `ClaudeFailure` -- callers of `ask()`
    should not need to catch this directly."""


@dataclass(frozen=True)
class ClaudeAnswer:
    """Claude answered directly; nothing to authorize or invoke. Speak
    `text` and the turn ends -- exactly like a router match with no skill,
    per the plan."""

    text: str


@dataclass(frozen=True)
class ClaudeProposal:
    """Claude proposed a skill call. **Not pre-validated against the real
    registry or a handler's signature** -- the caller must run this through
    the same `SkillSpec.invoke()` / `Ladder.authorize()` path a router match
    goes through, with the Claude-origin floor applied (blocked on finding 2
    until the shared floor mechanism lands; out of scope for this module)."""

    skill_name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ClaudeFailure:
    """The call did not produce a usable answer or proposal. `reason` is a
    short machine-readable code (`cli_not_found`, `spawn_failed`, `timeout`,
    `process_error`, `model_error`, `unparseable_output`); `detail` is
    safe-ish diagnostic text for logs, not necessarily safe to speak
    verbatim to Manav -- the caller decides what to actually say."""

    reason: str
    detail: str = ""


ClaudeResult = ClaudeAnswer | ClaudeProposal | ClaudeFailure


def find_cli() -> str:
    """Resolve the `claude` executable to a real, existing path. Never a raw
    string from config -- `docs/SECURITY_SPEC.md` §3.2's reserved allowlist
    row requires this exact shape."""
    path = shutil.which("claude")
    if path is None:
        raise ClaudeCliNotFound("`claude` was not found on PATH")
    return path


#: F59, closed and literal -- the only three names `_child_env()` may ever
#: touch. Nothing else in this file reads `os.environ`.
_ENV_VARS_TO_SCRUB = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def _child_env() -> dict[str, str]:
    """F59 (narrower than this module's first attempt, per nova-security's
    ruling -- see module docstring's "Env discipline" section): a filtered
    copy of the parent's environment, built for exactly one purpose -- the
    `env=` argument to this module's one sanctioned spawn call. Touches only
    *presence/absence* of the closed, literal set above via `dict.pop(name,
    None)` -- never reads or branches on a value. This is deliberately
    narrower than "give this file general os.environ access": nothing here
    inspects what `ANTHROPIC_API_KEY` (or anything else) is *set to*, and no
    other line in this file touches `os.environ` at all -- an unrelated var
    added here later would be a visible, reviewable diff to this one
    function, not a capability the file already had.
    """
    env = dict(os.environ)
    for name in _ENV_VARS_TO_SCRUB:
        env.pop(name, None)
    return env


def _argv(cli_path: str, *, model: str) -> list[str]:
    return [
        cli_path,
        "-p",
        "--output-format", "json",
        "--tools", "",  # the load-bearing flag -- see module docstring finding 1
        "--safe-mode",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--model", model,
        "--json-schema", json.dumps(_RESULT_SCHEMA),
        "--system-prompt", _SYSTEM_PROMPT,
    ]


def _kill_tree(pid: int) -> None:
    """Unconditional process-tree sweep on Windows -- `Popen.kill()` alone
    does not reliably reach grandchildren. Best-effort: a failure here is
    logged, not raised, since `proc.kill()` (already called by the time this
    runs) is the primary kill and this is defense in depth on top of it."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=10, check=False,
        )
    except Exception as exc:  # never let cleanup itself raise into the caller
        _log.warning("taskkill for pid %s failed (%s) -- proc.kill() was still called", pid, exc)


def _parse_output(raw: bytes) -> ClaudeResult:
    """Parse `claude -p --output-format json`'s stdout. Two JSON layers:
    the CLI's own envelope (is_error, result, ...), and `result` itself is a
    *string* containing the schema-constrained JSON this module actually
    cares about. Any failure to parse at either layer, or a shape that
    doesn't match `_RESULT_SCHEMA`'s two valid `kind`s, is a `ClaudeFailure`
    -- never partially interpreted."""
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return ClaudeFailure(reason="unparseable_output", detail=f"envelope did not parse as JSON: {exc}")

    if not isinstance(envelope, dict):
        return ClaudeFailure(reason="unparseable_output", detail="envelope JSON is not an object")

    if envelope.get("is_error"):
        return ClaudeFailure(reason="model_error", detail=str(envelope.get("result"))[:500])

    result_raw = envelope.get("result")
    if not isinstance(result_raw, str):
        return ClaudeFailure(reason="unparseable_output", detail="envelope has no string 'result' field")

    try:
        payload = json.loads(result_raw)
    except json.JSONDecodeError as exc:
        return ClaudeFailure(reason="unparseable_output", detail=f"'result' was not JSON: {exc}")

    if not isinstance(payload, dict):
        return ClaudeFailure(reason="unparseable_output", detail="'result' JSON is not an object")

    kind = payload.get("kind")
    if kind == "answer":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return ClaudeFailure(reason="unparseable_output", detail="kind=answer missing non-empty 'text'")
        return ClaudeAnswer(text=text)

    if kind == "propose":
        skill_name = payload.get("skill_name")
        params = payload.get("params")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return ClaudeFailure(reason="unparseable_output", detail="kind=propose missing non-empty 'skill_name'")
        if not isinstance(params, dict):
            return ClaudeFailure(reason="unparseable_output", detail="kind=propose missing object 'params'")
        return ClaudeProposal(skill_name=skill_name, params=params)

    return ClaudeFailure(reason="unparseable_output", detail=f"unrecognized kind={kind!r}")


async def ask(
    prompt: str,
    *,
    egress: Egress,
    audit: AuditLog,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
) -> ClaudeResult:
    """Send `prompt` to `claude` and return one of `ClaudeAnswer` /
    `ClaudeProposal` / `ClaudeFailure`. Never raises for an ordinary failure
    mode (missing binary, timeout, garbage output) -- those all come back as
    `ClaudeFailure` so the caller has one shape to handle, per finding 6's
    "fail loudly and closed, never into local guessing" requirement.
    `egress.release_for_process()` exceptions (a write failure in the audit
    log) DO propagate uncaught, matching `Egress`'s own documented
    discipline ("the caller must not spawn the child on this exception").

    `prompt` should already contain everything Claude needs to decide
    between answer/propose -- the transcript and the registered skill
    names/descriptions (`docs/M2_OPEN_BRAIN_PLAN.md` T2.4's "minimal
    context" list) -- composing that is the caller's job, not this
    function's; this module only owns the output-format contract (the
    system prompt + JSON schema above).

    `audit` is separate from `egress` (which does not expose its internal
    `AuditLog` publicly) -- used only for F61's `claude_arm.invoke` record
    below, naming the resolved `cli_path` so a PATH-order substitution is
    reviewable after the fact. Same "raises uncaught" discipline as
    `egress.release_for_process()`: a write failure here must not be
    swallowed into "the audit trail is fine, actually."
    """
    try:
        cli_path = find_cli()
    except ClaudeCliNotFound as exc:
        _log.error("Claude fallthrough: %s", exc)
        return ClaudeFailure(reason="cli_not_found", detail=str(exc))

    # F61: audit the resolved path on every invocation, before anything
    # else -- find_cli() trusts PATH search order with no containment
    # check (deliberately: an open_app-style curated-directory allowlist is
    # wrong for an npm-installed tool), so this is what turns an invisible
    # PATH substitution into a reviewable one. Raises uncaught on a write
    # failure, same discipline as release_for_process() below.
    audit.record({"kind": "claude_arm.invoke", "cli_path": cli_path, "model": model})

    # Redact + audit BEFORE spawning -- Egress.release_for_process()'s own
    # docstring: "the caller must not spawn the child on this exception."
    # Any exception here propagates uncaught, on purpose.
    released = egress.release_for_process("claude_fallthrough", prompt)

    argv = _argv(cli_path, model=model)
    try:
        # F59: env= is a filtered copy (_child_env()), not None/omitted --
        # see that function's docstring for the exact, narrow scope
        # nova-security ruled on (presence/absence of three literal names,
        # never a value, nothing else in this file touches os.environ).
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )
    except OSError as exc:
        _log.error("Claude fallthrough: failed to spawn %r (%s)", cli_path, exc)
        return ClaudeFailure(reason="spawn_failed", detail=str(exc))

    t0 = time.monotonic()
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=released.encode("utf-8")), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        _log.error(
            "Claude fallthrough: round trip exceeded %.1fs (F49: kill, not abandon) -- killing pid %s",
            timeout_s, proc.pid,
        )
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_POST_KILL_WAIT_S)
        except asyncio.TimeoutError:
            # A known Windows/ProactorEventLoop quirk: wait() can fail to
            # return after kill() even though the process is verifiably
            # dead. Not treated as "kill failed" -- see module docstring.
            _log.warning(
                "Claude fallthrough: proc.wait() after kill() did not return within %.1fs "
                "(pid %s) -- proceeding; taskkill below is unconditional regardless",
                _POST_KILL_WAIT_S, proc.pid,
            )
        _kill_tree(proc.pid)
        return ClaudeFailure(reason="timeout", detail=f"exceeded {timeout_s:.1f}s (waited {elapsed:.1f}s)")

    if proc.returncode != 0:
        return ClaudeFailure(
            reason="process_error",
            detail=f"exit {proc.returncode}: {err.decode(errors='replace')[:500]}",
        )

    return _parse_output(out)
