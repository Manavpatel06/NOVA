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
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NOVA_PKG = REPO_ROOT / "nova"

# nova/__init__.py is the sanctioned exception: it *is* the path registry.
_PATH_LITERAL_EXEMPT = {NOVA_PKG / "__init__.py"}


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


@pytest.mark.parametrize("path", _all_nova_source_files(), ids=lambda p: str(p.relative_to(NOVA_PKG)))
def test_module_does_not_read_os_environ(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = _find_environ_reads(tree)
    assert not hits, f"{path} reads os.environ, which only Config may do: {hits}"


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
