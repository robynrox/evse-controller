"""The control module mutates module-level state from its loop.

A missed ``global`` declaration turns an assignment into a local, so the
first read raises ``UnboundLocalError``.  That is exactly what stopped the
one-second idle-OCPP watcher from arming a handoff: the error was caught and
logged each second, and OCPP never switched.  Importing the controller would
start hardware threads, so the check reads the source instead.
"""

import ast
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "smart_evse_controller.py"
)


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.add(node.target.id)
    return names


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _missing_globals(tree: ast.Module, name: str) -> set[str]:
    function = _function(tree, name)
    declared = {
        item
        for node in ast.walk(function)
        if isinstance(node, ast.Global)
        for item in node.names
    }
    assigned = {
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    parameters = {arg.arg for arg in function.args.args}
    return (
        assigned & _module_level_names(tree)
    ) - declared - parameters


@pytest.mark.parametrize("name", ["_check_idle_ocpp", "main"])
def test_control_functions_declare_the_module_state_they_assign(name):
    tree = ast.parse(SOURCE.read_text())

    missing = _missing_globals(tree, name)

    assert not missing, (
        f"{name} assigns module state without a global declaration: "
        f"{sorted(missing)}"
    )
