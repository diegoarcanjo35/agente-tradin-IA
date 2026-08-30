"""Runtime proof that the AI Shadow module cannot reach execution or exchange
credentials. Used by tests/test_ai_shadow.py::test_ai_module_has_no_execution_import
so the boundary is enforced by code inspection, not just convention.
"""
from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_IMPORT_PREFIXES = ("app.execution", "pybit")
FORBIDDEN_NAME_SUBSTRINGS = ("bybit_api_key", "bybit_api_secret")


def scan_module_source(path: Path) -> list[str]:
    """Return a list of violations found in the given source file: forbidden
    imports of the execution layer or exchange SDK, or references to
    credential field names."""
    source = path.read_text(encoding="utf-8")
    violations: list[str] = []

    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"forbidden import from: {module}")

    lowered = source.lower()
    for needle in FORBIDDEN_NAME_SUBSTRINGS:
        if needle in lowered:
            violations.append(f"forbidden credential reference: {needle}")

    return violations


def scan_ai_shadow_package(package_dir: Path) -> dict[str, list[str]]:
    """Scans every module in the package except this guard itself, which
    necessarily mentions the forbidden names in order to detect them."""
    results: dict[str, list[str]] = {}
    for py_file in sorted(package_dir.glob("*.py")):
        if py_file.name == "guard.py":
            continue
        violations = scan_module_source(py_file)
        if violations:
            results[py_file.name] = violations
    return results
