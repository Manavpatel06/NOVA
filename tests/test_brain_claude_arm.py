"""Contract: nova/brain/claude_arm.py (T2.4a, docs/M2_OPEN_BRAIN_PLAN.md T2.4,
docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md) -- F60.

Every other file on the process-spawn allowlist (test_no_network_or_process_
spawn.py) has a same-file AST test pinning its load-bearing property:
gpu.py's literal argv[0]/query, open_app.py's and add_app.py's argv-list-
with-shell=False. claude_arm.py had none -- the only verification was
scripts/probe_claude_arm.py, a manual script requiring a real CLI, real
auth, and a human to run it. Nothing in CI would have caught a future edit
that widens --tools past "" (the single flag standing between "Claude
proposes" and "Claude executes"), adds a --dangerously-skip-permissions
spelling, or moves the prompt onto argv instead of stdin (argv is visible
in process listings and shell history in a way stdin isn't -- and the
prompt is attacker-influenceable content).

Four properties pinned here, per the lead's/nova-security's explicit scope:
  1. `--tools` is always the literal empty string in `_argv()`.
  2. None of the dangerous-skip-permissions spellings appear anywhere in
     the module's actual CODE (deliberately excluding docstrings, which
     document the ban by naming the very strings they forbid -- a naive
     whole-file substring scan would false-positive on the module's own
     docstring).
  3. `cli_path` traces to `find_cli()`'s return value, never a literal or
     config string -- and `_argv()` has no prompt-shaped parameter at all,
     so it structurally cannot put prompt text into argv even by accident.
  4. A mocked-subprocess, mocked-egress test proving the bytes handed to
     `communicate(input=...)` are `egress.release_for_process()`'s return
     value specifically (not the raw prompt), and that neither the raw nor
     the redacted prompt text ever appears in argv.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import nova.brain.claude_arm as claude_arm_mod
from nova.security.audit import AuditLog

NOVA_ROOT = Path(claude_arm_mod.__file__).resolve().parent.parent
MODULE_PATH = NOVA_ROOT / "brain" / "claude_arm.py"


def _tree() -> ast.AST:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """id() of every ast.Constant that is a docstring (the first statement
    of the module, or of any function/class body) -- these are excluded
    from the dangerous-flag-spelling scan below, since this module's own
    docstring legitimately names the exact strings it forbids, in prose
    explaining why they're forbidden."""
    ids: set[int] = set()
    candidates: list[ast.AST] = [tree] + [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for node in candidates:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            ids.add(id(body[0].value))
    return ids


# ---------------------------------------------------------------------------
# 1. --tools is always the literal empty string
# ---------------------------------------------------------------------------


def test_tools_flag_is_always_the_literal_empty_string_in_argv():
    tree = _tree()
    argv_fn = _find_function(tree, "_argv")
    assert argv_fn is not None, "expected a _argv() function in nova/brain/claude_arm.py"

    return_lists = [
        node for node in ast.walk(argv_fn)
        if isinstance(node, ast.List)
    ]
    assert return_lists, "expected _argv() to return a literal list"

    found_tools_flag = False
    for lst in return_lists:
        elts = lst.elts
        for i, elt in enumerate(elts):
            if isinstance(elt, ast.Constant) and elt.value == "--tools":
                found_tools_flag = True
                assert i + 1 < len(elts), "'--tools' is the last element -- no value follows it"
                value = elts[i + 1]
                assert isinstance(value, ast.Constant) and value.value == "", (
                    f"'--tools' is followed by {ast.dump(value)}, not the literal empty string -- "
                    "this is the single flag standing between 'Claude proposes' and 'Claude executes'"
                )
    assert found_tools_flag, "'--tools' does not appear in _argv()'s returned list at all"


# ---------------------------------------------------------------------------
# 2. No dangerous-skip-permissions spelling anywhere in actual code
# ---------------------------------------------------------------------------


_DANGEROUS_SPELLINGS = [
    "dangerously-skip-permissions",
    "bypassPermissions",
    "dontAsk",
    "yolo",
]


def test_no_dangerous_skip_permissions_flag_spelling_anywhere_in_code():
    tree = _tree()
    docstring_ids = _docstring_node_ids(tree)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids:
            lowered = node.value.lower()
            for spelling in _DANGEROUS_SPELLINGS:
                if spelling.lower() in lowered:
                    offenders.append(f"{node.value!r} at line {node.lineno} contains {spelling!r}")
    assert not offenders, (
        f"claude_arm.py's actual code (not docstrings) contains a skip-permissions "
        f"spelling: {offenders}"
    )


def test_the_dangerous_spelling_scan_is_not_vacuous_it_really_excludes_only_docstrings():
    """Guard the guard: the module docstring DOES legitimately contain
    these exact strings (documenting the ban) -- if the exclusion were
    broken (e.g. matching too much, silently skipping real code), this
    test would not distinguish that from a correctly-scoped exclusion. It
    at least proves the docstring itself is not what's saving the test
    above: the module source, read as raw text, really does contain these
    substrings somewhere (in the docstring), while the code-only scan
    still passes -- so the exclusion is doing real work, not vacuously
    finding nothing because the strings aren't in the file at all."""
    raw_text = MODULE_PATH.read_text(encoding="utf-8")
    assert "dangerously-skip-permissions" in raw_text, (
        "expected the module docstring to name this flag while documenting the ban -- "
        "if it no longer does, the test above may be passing vacuously"
    )


# ---------------------------------------------------------------------------
# 3. cli_path traces to find_cli(), never a literal/config string; _argv()
# has no prompt-shaped parameter at all.
# ---------------------------------------------------------------------------


def test_cli_path_traces_to_find_cli_never_a_literal_or_config_string():
    tree = _tree()
    ask_fn = _find_function(tree, "ask")
    assert ask_fn is not None

    assignments = [
        node for node in ast.walk(ask_fn)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "cli_path"
    ]
    assert assignments, "expected an assignment to `cli_path` inside ask()"
    for assign in assignments:
        assert (
            isinstance(assign.value, ast.Call)
            and isinstance(assign.value.func, ast.Name)
            and assign.value.func.id == "find_cli"
            and not assign.value.args
            and not assign.value.keywords
        ), f"cli_path is assigned from {ast.dump(assign.value)}, not a bare find_cli() call"

    argv_calls = [
        node for node in ast.walk(ask_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_argv"
    ]
    assert argv_calls, "expected a call to _argv(...) inside ask()"
    for call in argv_calls:
        assert call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "cli_path", (
            "_argv() is not called with the cli_path variable (traced to find_cli()) as its first argument"
        )


def test_argv_builder_has_no_prompt_shaped_parameter():
    """_argv() takes only cli_path and model -- there is structurally no
    parameter through which prompt text could enter argv, even by a
    future accidental edit that tries to pass one in."""
    tree = _tree()
    argv_fn = _find_function(tree, "_argv")
    assert argv_fn is not None
    param_names = {a.arg for a in argv_fn.args.args} | {a.arg for a in argv_fn.args.kwonlyargs}
    assert param_names == {"cli_path", "model"}, (
        f"_argv() has parameters {param_names}, expected exactly {{'cli_path', 'model'}} -- "
        "an added parameter here is worth scrutiny: prompt text must only ever reach the "
        "child via stdin, never via argv"
    )


# ---------------------------------------------------------------------------
# 4. Behavioral: the prompt reaches the child on stdin as
# egress.release_for_process()'s return value, and neither the raw nor the
# redacted prompt ever appears in argv. Mocked subprocess + mocked egress,
# real AuditLog, real ask()/_argv()/find_cli() logic.
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0, pid: int = 4242):
        self._stdout = stdout
        self.returncode = returncode
        self.pid = pid
        self.communicate_calls: list[bytes | None] = []

    async def communicate(self, input: bytes | None = None):
        self.communicate_calls.append(input)
        return self._stdout, b""


class _FakeEgress:
    def __init__(self, redacted: str):
        self._redacted = redacted
        self.calls: list[tuple[str, str]] = []

    def release_for_process(self, purpose: str, text: str) -> str:
        self.calls.append((purpose, text))
        return self._redacted


_ANSWER_ENVELOPE = b'{"is_error": false, "result": "{\\"kind\\": \\"answer\\", \\"text\\": \\"ok\\"}"}'


async def test_prompt_reaches_child_via_stdin_as_egress_redacted_text_never_via_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_arm_mod, "find_cli", lambda: r"C:\fake\claude.exe")

    captured: dict = {}
    fake_proc = _FakeProc(stdout=_ANSWER_ENVELOPE)

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr(claude_arm_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    secret_prompt = r"the real prompt, with a secret path C:\Users\manav\private\file.txt in it"
    redacted_marker = "REDACTED_PROMPT_MARKER_xyz"
    fake_egress = _FakeEgress(redacted_marker)
    audit = AuditLog(path=tmp_path / "audit.log")

    result = await claude_arm_mod.ask(secret_prompt, egress=fake_egress, audit=audit)

    assert isinstance(result, claude_arm_mod.ClaudeAnswer)
    assert result.text == "ok"

    # egress.release_for_process() was called with the RAW prompt, exactly once
    assert fake_egress.calls == [("claude_fallthrough", secret_prompt)]

    # stdin carries the REDACTED text, encoded -- not the raw prompt
    assert fake_proc.communicate_calls == [redacted_marker.encode("utf-8")]

    # argv never contains the raw prompt OR the redacted text, anywhere
    argv_joined = " ".join(str(a) for a in captured["argv"])
    assert secret_prompt not in argv_joined
    assert redacted_marker not in argv_joined

    # kwargs to create_subprocess_exec never carry the prompt either (e.g.
    # accidentally passed via cwd, env, or some other keyword)
    kwargs_str = str(captured["kwargs"])
    assert secret_prompt not in kwargs_str
    assert redacted_marker not in kwargs_str


async def test_missing_cli_never_spawns_and_reports_cli_not_found(monkeypatch, tmp_path):
    """finding 6's confidence-honesty point: absence of the binary is a
    plain ClaudeFailure, never a silent spawn attempt or a crash."""
    def raise_not_found():
        raise claude_arm_mod.ClaudeCliNotFound("not on PATH")

    monkeypatch.setattr(claude_arm_mod, "find_cli", raise_not_found)

    spawn_called = False

    async def fake_create_subprocess_exec(*argv, **kwargs):
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("should never be reached -- find_cli() failed first")

    monkeypatch.setattr(claude_arm_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    audit = AuditLog(path=tmp_path / "audit.log")

    result = await claude_arm_mod.ask("anything", egress=_FakeEgress("x"), audit=audit)

    assert isinstance(result, claude_arm_mod.ClaudeFailure)
    assert result.reason == "cli_not_found"
    assert spawn_called is False
    assert audit.verify().entries == 0  # F61's invoke record never fires -- nothing was resolved to audit
