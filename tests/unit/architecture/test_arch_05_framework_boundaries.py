"""ARCH-05: the orchestration core depends inward through pure typed ports only."""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from marketing_agents.application.orchestration import (
    OrchestrationDependencies,
    OrchestrationDependencyError,
)

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN_EXTERNAL = {"alembic", "fastapi", "sqlalchemy", "uvicorn"}
FORBIDDEN_INTERNAL = {
    "marketing_agents.adapters",
    "marketing_agents.api",
    "marketing_agents.infrastructure",
    "marketing_agents.workers",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_arch_05_domain_and_application_have_no_framework_or_outward_imports() -> None:
    source_root = ROOT / "apps" / "api" / "src" / "marketing_agents"
    for layer in ("domain", "application"):
        for path in sorted((source_root / layer).rglob("*.py")):
            for imported in _imports(path):
                assert imported.split(".", 1)[0] not in FORBIDDEN_EXTERNAL, (path, imported)
                assert not any(
                    imported == forbidden or imported.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_INTERNAL
                ), (path, imported)


def test_arch_05_orchestration_dependencies_run_with_in_memory_fakes() -> None:
    events: list[str] = []

    class FakeClock:
        def now(self) -> datetime:
            return datetime(2026, 1, 1, tzinfo=UTC)

    class FakeIds:
        def new(self, namespace: str) -> str:
            return f"{namespace}.fixture-1"

    class FakeUnitOfWork:
        works = object()
        runs = object()
        audits = object()

        async def __aenter__(self) -> FakeUnitOfWork:
            events.append("enter")
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append("exit")

        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            events.append("rollback")

    dependencies = OrchestrationDependencies(FakeClock(), FakeIds(), FakeUnitOfWork)
    assert dependencies.utc_now() == datetime(2026, 1, 1, tzinfo=UTC)
    assert dependencies.new_id("run") == "run.fixture-1"

    async def transact() -> None:
        async with dependencies.unit_of_work() as unit_of_work:
            await unit_of_work.commit()

    asyncio.run(transact())
    assert events == ["enter", "commit", "exit"]


def test_arch_05_invalid_clock_id_and_namespace_fail_at_the_core_boundary() -> None:
    class NaiveClock:
        def now(self) -> datetime:
            return datetime(2026, 1, 1)

    class BadIds:
        def new(self, _namespace: str) -> str:
            return "wrong.fixture-1"

    class UnitOfWorkFactory:
        def __call__(self) -> object:
            return object()

    dependencies = OrchestrationDependencies(NaiveClock(), BadIds(), UnitOfWorkFactory())  # type: ignore[arg-type]
    with pytest.raises(OrchestrationDependencyError, match="UTC"):
        dependencies.utc_now()
    with pytest.raises(OrchestrationDependencyError, match="namespace"):
        dependencies.new_id("run")
    with pytest.raises(OrchestrationDependencyError, match="slug"):
        dependencies.new_id("bad namespace")
