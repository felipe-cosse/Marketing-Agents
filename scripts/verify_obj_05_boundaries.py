"""OBJ-05: dependency-free architecture gate for implementation-neutral bridges.

Behavioral adapter qualification lives in contract/integration tests. This gate
checks only the composition dependency boundary, including in an exported tree
without installed dependencies. Restoring the old bridge must fail this gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

DISPATCH_PATH = Path("apps/api/src/marketing_agents/infrastructure/adapters/connectors/dispatch.py")
BRIDGES = frozenset({"RegistryConnectorReadAdapter", "RegistryConnectorWriteGateway"})


def violations(source: str) -> list[str]:
    tree = ast.parse(source)
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        else:
            modules = []
        if any({"mock", "real", "deterministic"} & set(name.split(".")) for name in modules):
            errors.append(f"line {node.lineno}: bridge imports a concrete implementation")
        if isinstance(node, ast.Attribute) and node.attr == "ledger":
            errors.append(f"line {node.lineno}: bridge depends on concrete receipt storage")
        if (
            isinstance(node, ast.keyword)
            and node.arg in {"provider_mode", "provider_name", "provider_version"}
            and isinstance(node.value, ast.Constant)
        ):
            errors.append(f"line {node.lineno}: bridge hard-codes provider identity")

    found = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in BRIDGES:
            continue
        found.add(node.name)
        constructors = [
            item
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "__init__"
        ]
        if len(constructors) != 1 or not any(
            isinstance(arg.annotation, ast.Name) and arg.annotation.id == "ConnectorBindingSource"
            for arg in constructors[0].args.args
        ):
            errors.append(f"{node.name}: constructor must accept ConnectorBindingSource")
    if found != BRIDGES:
        errors.append("both controlled READ and authorized WRITE bridges are required")
    return errors


def verify(root: Path) -> None:
    errors = violations((root / DISPATCH_PATH).read_text(encoding="utf-8"))
    assert not errors, "OBJ-05 implementation-neutral bridge boundary: " + "; ".join(errors)


if __name__ == "__main__":
    verify(Path(__file__).resolve().parents[1])
    print("OBJ-05 implementation-neutral connector bridge boundary verified")
