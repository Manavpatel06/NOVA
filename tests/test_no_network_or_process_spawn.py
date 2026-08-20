"""Contract: docs/SECURITY_SPEC.md §3, docs/THREAT_MODEL.md §1.1/§1.2/§2.2 —
the rescoped "no socket, no unsanctioned process spawn" AST test.

The retired build's version of this test (`_OLD_ARCHIVE/tests/
test_no_direct_network.py`) only walked for transport-family imports
(socket, requests, urllib, ...). Its TRANSPORTS set never included
`subprocess`, and seven files under the retired `nova/` imported it freely —
the promise stayed green over a hole you could drive a process through
(os.startfile("http://evil") opens a browser; a UNC path triggers an SMB
fetch; neither goes through Egress or gets caught by a socket-only scan).

This test is rescoped and renamed to enforce what CLAUDE.md actually
promises: "no other file opens a socket **or spawns a process capable of
one**." Two separate, individually-justified, size-pinned allowlists — a
transport exemption is a different kind of trust than a process-spawn
exemption, and conflating them would let a transport-only file spawn
processes for free.

Scoped to `nova/` (the shipped package) per docs/CONTRACTS.md's promise
("ONE sanctioned module") — `scripts/` holds developer tooling outside that
promise (e.g. scripts/probe_gpu.py, which legitimately shells out for
diagnostics) and isn't named anywhere in docs/SECURITY_SPEC.md's allowlist
tables, so it's out of scope here rather than silently assumed clean.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import nova

NOVA_ROOT = Path(nova.__file__).resolve().parent

# --- §3.1: transport imports ------------------------------------------------

TRANSPORT_MODULES = {
    "socket", "ssl", "urllib", "http", "httpcore", "httplib2", "httpx",
    "pycurl", "requests", "socketserver", "urllib3", "websocket", "websockets",
    "aiohttp",
}
TRANSPORT_ALLOWLIST = {"security/egress.py"}

# --- §3.2: process spawn -----------------------------------------------------

_OS_BANNED_ATTRS = {
    "system", "popen", "startfile",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
}
_ASYNCIO_BANNED_ATTRS = {"create_subprocess_exec", "create_subprocess_shell"}
PROCESS_ALLOWLIST = {"stt/gpu.py", "skills/open_app.py", "brain/claude_arm.py", "skills/add_app.py"}


def _rel(path: Path) -> str:
    return str(path.relative_to(NOVA_ROOT)).replace("\\", "/")


def _all_nova_files() -> list[Path]:
    return sorted(NOVA_ROOT.rglob("*.py"))


def _root_import_name(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if node.module:
        return [node.module.split(".")[0]]
    return []


def _find_transport_imports(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in _root_import_name(node):
                if name in TRANSPORT_MODULES:
                    hits.append(f"{name} (line {node.lineno})")
    return hits


def _find_process_spawn(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "subprocess":
                    hits.append(f"import subprocess (line {node.lineno})")
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "subprocess":
                hits.append(f"from subprocess import ... (line {node.lineno})")
            if node.module == "os":
                for alias in node.names:
                    if alias.name in _OS_BANNED_ATTRS:
                        hits.append(f"from os import {alias.name} (line {node.lineno})")
            if node.module == "asyncio":
                for alias in node.names:
                    if alias.name in _ASYNCIO_BANNED_ATTRS:
                        hits.append(f"from asyncio import {alias.name} (line {node.lineno})")
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                if node.value.id == "os" and node.attr in _OS_BANNED_ATTRS:
                    hits.append(f"os.{node.attr} (line {node.lineno})")
                if node.value.id == "asyncio" and node.attr in _ASYNCIO_BANNED_ATTRS:
                    hits.append(f"asyncio.{node.attr} (line {node.lineno})")
    return hits


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ---------------------------------------------------------------------------
# Allowlist shape: pinned sizes, files must exist
# ---------------------------------------------------------------------------


def test_transport_allowlist_is_exactly_one_named_file():
    assert len(TRANSPORT_ALLOWLIST) == 1
    for rel in TRANSPORT_ALLOWLIST:
        assert (NOVA_ROOT / rel).is_file(), f"transport allowlist entry {rel} does not exist"


def test_process_spawn_allowlist_is_exactly_four_named_files_today():
    """Four earn it per docs/SECURITY_SPEC.md §3.2. The third,
    `nova/brain/claude_arm.py` (T2.4a), was the reserved slot — not
    pre-approved before the file existed, added the moment it landed, per
    that section's own reserved-row text ("Not added until M4 actually
    lands the file"). The fourth, `nova/skills/add_app.py` (T2.3a), earns
    it the same way `open_app.py` does — see
    test_add_app_popen_uses_an_argv_list_and_shell_false and
    test_add_app_popen_target_is_realpath_resolved_before_the_call below,
    which pin the exact argv shape rather than trusting the module
    docstring's claim. A change to this number must be a conscious edit to
    this test, not a silently-growing set."""
    assert len(PROCESS_ALLOWLIST) == 4
    for rel in PROCESS_ALLOWLIST:
        assert (NOVA_ROOT / rel).is_file(), f"process-spawn allowlist entry {rel} does not exist"


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------


def test_no_file_outside_the_transport_allowlist_imports_a_transport():
    offenders = {}
    for path in _all_nova_files():
        rel = _rel(path)
        if rel in TRANSPORT_ALLOWLIST:
            continue
        hits = _find_transport_imports(_parse(path))
        if hits:
            offenders[rel] = hits
    assert not offenders, f"transport imports outside the allowlist: {offenders}"


def test_no_file_outside_the_process_spawn_allowlist_spawns_a_process():
    offenders = {}
    for path in _all_nova_files():
        rel = _rel(path)
        if rel in PROCESS_ALLOWLIST:
            continue
        hits = _find_process_spawn(_parse(path))
        if hits:
            offenders[rel] = hits
    assert not offenders, f"process-spawn calls outside the allowlist: {offenders}"


def test_allowlisted_transport_file_actually_uses_a_transport():
    """The allowlist entry should be earning its keep, not a stale grant —
    if egress.py stops needing urllib, this exemption should shrink with
    it."""
    path = NOVA_ROOT / "security" / "egress.py"
    assert _find_transport_imports(_parse(path)), (
        "nova/security/egress.py is on the transport allowlist but doesn't "
        "import a transport module — is the exemption still needed?"
    )


def test_allowlisted_process_spawn_files_actually_spawn_a_process():
    for rel in PROCESS_ALLOWLIST:
        path = NOVA_ROOT / rel
        assert _find_process_spawn(_parse(path)), (
            f"{rel} is on the process-spawn allowlist but doesn't spawn "
            f"anything — is the exemption still needed?"
        )


# ---------------------------------------------------------------------------
# nova/stt/gpu.py: literal argv[0], never a variable, never shell=True
# ---------------------------------------------------------------------------


def _find_subprocess_run_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            calls.append(node)
    return calls


def test_gpu_py_nvidia_smi_argv0_is_a_literal_string_not_a_variable():
    path = NOVA_ROOT / "stt" / "gpu.py"
    calls = _find_subprocess_run_calls(_parse(path))
    assert calls, "expected at least one subprocess.run(...) call in nova/stt/gpu.py"

    for call in calls:
        assert call.args, "subprocess.run(...) called with no positional argv list"
        argv = call.args[0]
        assert isinstance(argv, ast.List), "argv to subprocess.run must be a literal list, not a bare string"
        assert argv.elts, "argv list is empty"
        first = argv.elts[0]
        assert isinstance(first, ast.Constant) and first.value == "nvidia-smi", (
            f"argv[0] must be the literal string 'nvidia-smi', got {ast.dump(first)}"
        )


def test_gpu_py_nvidia_smi_never_uses_shell_true():
    path = NOVA_ROOT / "stt" / "gpu.py"
    for call in _find_subprocess_run_calls(_parse(path)):
        for kw in call.keywords:
            if kw.arg == "shell":
                assert isinstance(kw.value, ast.Constant) and kw.value.value is False


def test_gpu_py_run_nvidia_smi_is_always_called_with_a_literal_query_string():
    """docs/SECURITY_SPEC.md §3.2: every call site's `query` argument must be
    a literal string, not built from anything outside the module."""
    path = NOVA_ROOT / "stt" / "gpu.py"
    tree = _parse(path)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_nvidia_smi"
    ]
    assert calls, "expected at least one _run_nvidia_smi(...) call site"
    for call in calls:
        assert call.args, "_run_nvidia_smi called with no query argument"
        assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str), (
            f"_run_nvidia_smi's query argument must be a literal string, got {ast.dump(call.args[0])}"
        )


# ---------------------------------------------------------------------------
# nova/skills/open_app.py: argv list, shell=False, validation precedes spawn
# ---------------------------------------------------------------------------


def _find_popen_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            calls.append(node)
    return calls


def test_open_app_popen_uses_an_argv_list_and_shell_false():
    path = NOVA_ROOT / "skills" / "open_app.py"
    calls = _find_popen_calls(_parse(path))
    assert calls, "expected a subprocess.Popen(...) call in nova/skills/open_app.py"

    for call in calls:
        assert call.args and isinstance(call.args[0], ast.List), (
            "Popen's first argument must be a literal argv list, never a bare string"
        )
        shell_kwargs = [kw for kw in call.keywords if kw.arg == "shell"]
        assert shell_kwargs, "Popen call must pass shell= explicitly"
        assert shell_kwargs[0].value.value is False


def test_open_app_popen_call_is_not_the_first_statement_in_its_function():
    """Minimal but real: catches a validator that got deleted entirely, even
    though it can't prove the validator is *correct* (see
    test_skills_open_app.py for behavioral proof of that)."""
    path = NOVA_ROOT / "skills" / "open_app.py"
    tree = _parse(path)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            popen_calls = _find_popen_calls(node)
            if not popen_calls:
                continue
            target = popen_calls[0]
            index = None
            for i, stmt in enumerate(node.body):
                if any(n is target for n in ast.walk(stmt)):
                    index = i
                    break
            assert index is not None, "could not locate the statement containing the Popen call"
            assert index > 0, (
                f"Popen(...) is the first statement in {node.name}() — no validation precedes it"
            )
            return
    pytest.fail("no async function containing a Popen call was found in open_app.py")


# ---------------------------------------------------------------------------
# nova/skills/add_app.py: the fourth allowlist entry (T2.3a). Same argv
# list + shell=False shape open_app.py already earns its entry with, plus
# one property specific to this module: the module docstring claims the
# Popen call launches the THIRD, freshest resolution (`final_resolved`,
# re-verified immediately before both the write and the launch) rather
# than an earlier one computed before the CONFIRM dialog was shown — pin
# that structurally, not just "some resolved path was used".
# ---------------------------------------------------------------------------


def test_add_app_popen_uses_an_argv_list_and_shell_false():
    path = NOVA_ROOT / "skills" / "add_app.py"
    calls = _find_popen_calls(_parse(path))
    assert calls, "expected a subprocess.Popen(...) call in nova/skills/add_app.py"

    for call in calls:
        assert call.args and isinstance(call.args[0], ast.List), (
            "Popen's first argument must be a literal argv list, never a bare string"
        )
        shell_kwargs = [kw for kw in call.keywords if kw.arg == "shell"]
        assert shell_kwargs, "Popen call must pass shell= explicitly"
        assert shell_kwargs[0].value.value is False


def test_add_app_popen_call_is_not_the_first_statement_in_its_function():
    """Same minimal-but-real check as open_app's: a validator/rollback path
    that got deleted entirely must not go unnoticed just because Popen is
    still called somewhere."""
    path = NOVA_ROOT / "skills" / "add_app.py"
    tree = _parse(path)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            popen_calls = _find_popen_calls(node)
            if not popen_calls:
                continue
            target = popen_calls[0]
            index = None
            for i, stmt in enumerate(node.body):
                if any(n is target for n in ast.walk(stmt)):
                    index = i
                    break
            assert index is not None, "could not locate the statement containing the Popen call"
            assert index > 0, (
                f"Popen(...) is the first statement in {node.name}() — no validation precedes it"
            )
            return
    pytest.fail("no async function containing a Popen call was found in add_app.py")


def test_add_app_popen_target_is_the_freshest_third_resolution_not_an_earlier_one():
    """The module docstring's point 3 claims resolution happens three
    times (display, post-decision, pre-write) and that the Popen call uses
    the LAST of those (`final_resolved`), never a value computed earlier in
    the same call — the whole point of re-resolving immediately before the
    write is defeated if the launch then uses a stale variable instead.
    Pinned structurally: the sole positional argument passed to `str(...)`
    inside Popen's argv list must be a bare Name, and that name must not be
    the earlier `resolved` variable that add_app() also has in scope."""
    path = NOVA_ROOT / "skills" / "add_app.py"
    calls = _find_popen_calls(_parse(path))
    assert calls, "expected a subprocess.Popen(...) call in nova/skills/add_app.py"

    call = calls[0]
    argv = call.args[0]
    assert isinstance(argv, ast.List) and len(argv.elts) == 1
    wrapped = argv.elts[0]
    assert (
        isinstance(wrapped, ast.Call)
        and isinstance(wrapped.func, ast.Name)
        and wrapped.func.id == "str"
        and len(wrapped.args) == 1
    ), "expected Popen([str(<something>)], ...)"
    inner = wrapped.args[0]
    assert isinstance(inner, ast.Name), f"expected a bare variable name, got {ast.dump(inner)}"
    assert inner.id == "final_resolved", (
        f"Popen launches {inner.id!r}, not the freshest pre-write resolution "
        "('final_resolved') the module docstring promises"
    )
    assert inner.id != "resolved", (
        "Popen launches the FIRST resolution, computed before the CONFIRM "
        "decision — the whole point of re-resolving before the write is "
        "defeated if the launch doesn't use that fresh value"
    )
