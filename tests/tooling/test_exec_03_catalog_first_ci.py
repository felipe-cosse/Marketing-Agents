"""EXEC-03: catalog schema, seed, and count gates run before runtime work."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_ci_order import verify_ci_order

ROOT = Path(__file__).resolve().parents[2]


def test_exec_03_repository_ci_enforces_catalog_first() -> None:
    verify_ci_order(ROOT / ".github" / "workflows" / "ci.yml")


def test_exec_03_missing_catalog_dependency_is_rejected(tmp_path: Path) -> None:
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        """jobs:
  catalog:
    steps:
      - run: uv sync --frozen --python 3.12
      - run: make verify-catalog-release
      - run: uv run pytest -q tests/catalog
  runtime:
    steps:
      - run: make verify-backend
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must depend on catalog"):
        verify_ci_order(workflow)


def test_exec_03_reordered_or_missing_catalog_gate_is_rejected(tmp_path: Path) -> None:
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        """jobs:
  catalog:
    steps:
      - run: uv run pytest -q tests/catalog
      - run: uv sync --frozen --python 3.12
      - run: make verify-catalog-release
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="out of order"):
        verify_ci_order(workflow)

    workflow.write_text(
        """jobs:
  catalog:
    steps:
      - run: uv sync --frozen --python 3.12
      - run: uv run pytest -q tests/catalog
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must run exactly once"):
        verify_ci_order(workflow)
