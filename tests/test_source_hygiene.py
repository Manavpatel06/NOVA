"""CLAUDE.md constitutional rule, enforced as a static tripwire:

    'no module reads os.environ or hardcodes a path — everything comes from
    Config' / nova/__init__.py: 'nova/__init__.py is the only place paths
    live.'

This is a regression guard, not a substitute for the dynamic
test_config.py::test_load_config_never_reads_os_environ test — a source
scan catches *any* module (not just config.py) reaching for os.environ or
sprinkling a raw "D:\\NOVA" / "C:\\..." literal outside the one file that's
allowed to (nova/__init__.py, which docs/CONTRACTS.md and its own docstring
designate as "the one place that knows where things live on disk").

F59 (nova-security's ruling): `nova/brain/claude_arm.py` earns a second,
narrowly-scoped exception to the os.environ rule — never "may read
os.environ", but "may build a filtered copy of the process environment, for
the sole purpose of the one sanctioned `env=` argument in that file,
touching only presence/absence of a closed, literal 3-name set
(ANTHROPIC_API_KEY / CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX) —
never a variable's *value*, never any other use of the result." The
allowlist below grants the exemption; the structural tests after it pin
*how*, not just *that* — same "not just that, but how" discipline
test_no_network_or_process_spawn.py already applies to open_app.py's Popen
call and gpu.py's argv.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NOVA_PKG = REPO_ROOT / "nova"

# nova/__init__.py is the sanctioned exception: it *is* the path registry.
_PATH_LITERAL_EXEMPT = {NOVA_PKG / "__init__.py"}

# F59: exactly one entry, same size-pinned-and-justified shape as
# TRANSPORT_ALLOWLIST/PROCESS_ALLOWLIST in test_no_network_or_process_spawn.py.
ENVIRON_ALLOWLIST = {"brain/claude_arm.py"}


def _all_nova_source_files() -> list[Path]:
    if not NOVA_PKG.is_dir():
        return []
    return sorted(NOVA_PKG.rglob("*.py"))


def _find_environ_reads(tree: ast.AST) -> list[str]:
    """Find `os.environ`, `os.getenv(...)`, and bare `environ`/`getenv`
    (from-imported) usages anywhere in the module."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            hits.append(f"os.environ at line {node.lineno}")
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name == "getenv":
                hits.append(f"getenv(...) at line {node.lineno}")
    return hits


def _rel(path: Path) -> str:
    return str(path.relative_to(NOVA_PKG)).replace("\\", "/")


@pytest.mark.parametrize("path", _all_nova_source_files(), ids=lambda p: str(p.relative_to(NOVA_PKG)))
def test_module_does_not_read_os_environ(path):
    if _rel(path) in ENVIRON_ALLOWLIST:
        pytest.skip("F59: nova/brain/claude_arm.py has a narrowly-scoped, structurally-tested exemption below")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = _find_environ_reads(tree)
    assert not hits, f"{path} reads os.environ, which only Config may do: {hits}"


# ---------------------------------------------------------------------------
# F59: the ENVIRON_ALLOWLIST exemption — sized, justified, and constrained
# to exactly the shape nova-security's ruling described.
# ---------------------------------------------------------------------------


def test_environ_allowlist_is_exactly_one_named_file():
    assert len(ENVIRON_ALLOWLIST) == 1
    for rel in ENVIRON_ALLOWLIST:
        assert (NOVA_PKG / rel).is_file(), f"environ allowlist entry {rel} does not exist"


def test_allowlisted_environ_file_actually_reads_os_environ():
    """The exemption should be earning its keep, not a stale grant — same
    check TRANSPORT_ALLOWLIST/PROCESS_ALLOWLIST already get."""
    for rel in ENVIRON_ALLOWLIST:
        path = NOVA_PKG / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert _find_environ_reads(tree), (
            f"{rel} is on the environ allowlist but doesn't read os.environ — is the exemption still needed?"
        )


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_claude_arm_environ_read_happens_in_exactly_one_place_inside_child_env():
    """nova-security's ruling names the shape precisely: os.environ is
    touched nowhere in the file except inside one named function. A read
    anywhere else (module scope, a different helper, an f-string debug
    line someone adds later) is exactly the widening the ruling refused to
    pre-approve."""
    path = NOVA_PKG / "brain" / "claude_arm.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    child_env = _find_function(tree, "_child_env")
    assert child_env is not None, "expected a _child_env() function in nova/brain/claude_arm.py"

    whole_file_hits = _find_environ_reads(tree)
    assert len(whole_file_hits) == 1, (
        f"expected exactly one os.environ/getenv read in the whole file, found: {whole_file_hits}"
    )

    inside_child_env = _find_environ_reads(ast.Module(body=child_env.body, type_ignores=[]))
    assert len(inside_child_env) == 1, (
        "the one os.environ read in the file is not inside _child_env() — "
        f"whole-file hits: {whole_file_hits}"
    )


def test_claude_arm_child_env_never_branches_on_or_reads_out_a_value():
    """'Touches only presence/absence ... never a variable's value' — pinned
    structurally: no conditional/comparison logic exists inside the
    function body at all (there is nothing to branch ON if no value is
    ever inspected), and the only method calls made on anything inside the
    function are `.pop(...)` — never `.get`, a subscript read (`env[...]`),
    or anything that would hand a value back out to be inspected."""
    path = NOVA_PKG / "brain" / "claude_arm.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    child_env = _find_function(tree, "_child_env")
    assert child_env is not None

    for node in ast.walk(child_env):
        assert not isinstance(node, (ast.If, ast.IfExp, ast.Compare)), (
            f"_child_env() branches on or compares a value at line {node.lineno} — "
            "the ruling permits only unconditional presence/absence pops"
        )
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "env"
        ):
            # Scoped to subscripting the local `env` dict specifically --
            # NOT the function's own `-> dict[str, str]` return annotation,
            # which is also an ast.Subscript(ctx=Load) but isn't a value
            # read at all.
            pytest.fail(f"_child_env() reads a value via subscript at line {node.lineno} (env[...])")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr == "pop", (
                f"_child_env() calls .{node.func.attr}(...) at line {node.lineno} — "
                "only .pop(name, None) is permitted by the ruling"
            )


def test_claude_arm_child_env_result_is_used_only_as_the_spawn_calls_env_kwarg():
    """The filtered copy _child_env() builds must not be assigned somewhere
    it could be logged, inspected, or otherwise used for anything besides
    the one sanctioned `env=` argument — called exactly once in the file,
    and that one call site is directly the value of an `env=` keyword
    argument, never bound to a variable first."""
    path = NOVA_PKG / "brain" / "claude_arm.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    call_sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_child_env"
    ]
    # 2026-10-15: two sanctioned spawns now -- ask() (one-shot) and
    # ClaudeSession.start() (the Max-plan brain). The rule is unchanged:
    # EVERY call site is directly an env= keyword value, never bound first.
    assert 1 <= len(call_sites) <= 2, f"expected one or two _child_env() call sites, found {len(call_sites)}"

    env_values = {
        id(kw.value)
        for node in ast.walk(tree) if isinstance(node, ast.Call)
        for kw in node.keywords if kw.arg == "env"
    }
    for site in call_sites:
        assert id(site) in env_values, (
            f"_child_env() at line {site.lineno} is not directly the env= keyword argument of a spawn call — "
            "it may be getting assigned to a variable first, which the ruling doesn't permit"
        )


@pytest.mark.parametrize("path", _all_nova_source_files(), ids=lambda p: str(p.relative_to(NOVA_PKG)))
def test_module_does_not_hardcode_a_drive_absolute_path(path):
    if path in _PATH_LITERAL_EXEMPT:
        pytest.skip("nova/__init__.py is the designated single source of truth for paths")
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if len(v) >= 3 and v[1] == ":" and v[2] in "\\/" and v[0].isalpha():
                offenders.append(f"{v!r} at line {node.lineno}")
    assert not offenders, f"{path} hardcodes an absolute drive path: {offenders}"


def test_nova_package_exists_and_was_actually_scanned():
    """Guard the guard: if nova/ is empty/missing, the parametrized tests
    above silently collect zero cases and 'pass' having checked nothing."""
    files = _all_nova_source_files()
    assert len(files) >= 5, (
        f"expected several nova/*.py files to scan, found {len(files)}: {files}"
    )
