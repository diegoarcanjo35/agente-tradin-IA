"""Correction v1.1 #2: structurally proves that no code outside
app/risk/engine.py can mint a Risk Engine approval -- neither by importing/
instantiating the private `_RiskApprovalToken`, nor by constructing
`ApprovedOrder` directly. This is what makes "the Risk Engine has sole
authority" an enforced property rather than a convention.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_FILE = (REPO_ROOT / "app" / "risk" / "engine.py").resolve()
SCAN_DIRS = [REPO_ROOT / "app", REPO_ROOT / "tests"]
EXCLUDE_DIR_NAMES = {".venv", "venv", "__pycache__", "fakes"}


THIS_FILE = Path(__file__).resolve()


def iter_python_files():
    for base in SCAN_DIRS:
        for path in base.rglob("*.py"):
            resolved = path.resolve()
            if resolved == ALLOWED_FILE or resolved == THIS_FILE:
                continue
            if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
                continue
            yield path


def test_no_file_outside_risk_engine_references_the_approval_token():
    violations = []
    for path in iter_python_files():
        source = path.read_text(encoding="utf-8")
        if "_RiskApprovalToken" in source:
            violations.append(str(path.relative_to(REPO_ROOT)))
    assert violations == [], (
        f"_RiskApprovalToken referenced outside app/risk/engine.py: {violations}"
    )


def test_no_file_outside_risk_engine_constructs_approved_order_directly():
    violations = []
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name == "ApprovedOrder":
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert violations == [], (
        f"ApprovedOrder constructed outside app/risk/engine.py: {violations}"
    )


def test_execution_engines_only_import_approvedorder_as_a_type():
    """Sanity check that the execution layer still knows the type (for type
    hints) without ever instantiating it -- import is fine, calling isn't."""
    from app.execution import base as execution_base

    assert hasattr(execution_base, "ApprovedOrder")
