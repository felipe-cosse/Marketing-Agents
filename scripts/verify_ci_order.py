#!/usr/bin/env python3
"""Verify that catalog compilation is a hard prerequisite for later CI jobs."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

CATALOG_COMMANDS = (
    "uv sync --frozen --python 3.12",
    "make verify-catalog-release",
    "uv run pytest -q tests/catalog",
)


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def verify_ci_order(path: Path) -> None:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), dict):
        raise ValueError("CI workflow must define a jobs mapping")
    jobs: dict[str, Any] = document["jobs"]
    catalog = jobs.get("catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("steps"), list):
        raise ValueError("CI workflow must define a catalog job with steps")
    commands = [
        step["run"]
        for step in catalog["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    positions: list[int] = []
    for required in CATALOG_COMMANDS:
        if commands.count(required) != 1:
            raise ValueError(f"catalog job must run exactly once: {required}")
        positions.append(commands.index(required))
    if positions != sorted(positions):
        raise ValueError("catalog acquisition, release verification, and tests are out of order")
    for name, raw_job in jobs.items():
        if name == "catalog" or not isinstance(raw_job, dict):
            continue
        steps = raw_job.get("steps", [])
        run_commands = (
            [step.get("run", "") for step in steps if isinstance(step, dict)]
            if isinstance(steps, list)
            else []
        )
        exercises_project = any(
            command.startswith(("make ", "uv run ")) for command in run_commands
        )
        if exercises_project and "catalog" not in _needs(raw_job):
            raise ValueError(f"project job {name!r} must depend on catalog")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, nargs="?", default=Path(".github/workflows/ci.yml"))
    args = parser.parse_args()
    try:
        verify_ci_order(args.path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"CI order invalid: {exc}")
        return 1
    print("CI order valid: catalog release and contract tests gate every project job")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
