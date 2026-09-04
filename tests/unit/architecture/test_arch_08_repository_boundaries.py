"""ARCH-08: repository homes and import directions stay mechanically enforced."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.verify_architecture_boundaries import (
    BoundaryPolicyError,
    _typescript_imports,
    _typescript_ui_network_ownership,
    check_repository,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "architecture-boundaries.json"


def test_arch_08_current_repository_satisfies_boundary_policy() -> None:
    violations = check_repository(ROOT, load_policy(POLICY_PATH))

    assert violations == (), "\n".join(violation.render() for violation in violations)


def test_arch_08_boundary_gates_remain_on_both_ci_verification_paths() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^verify-governance:.*\bverify-architecture\b", makefile)
    assert re.search(r"(?m)^verify-web:.*\bweb-test-arch-08-unit\b", makefile)
    assert "run: make verify-governance" in workflow
    assert "run: make verify-web" in workflow


def test_arch_08_rejects_forbidden_boundary_fixtures(tmp_path: Path) -> None:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["required_layer_homes"] = [
        "apps/api/src/marketing_agents/domain",
        "apps/api/src/marketing_agents/application/ports",
        "apps/api/src/marketing_agents/api",
        "apps/web/src/api",
        "apps/web/src/contracts",
        "required/missing-layer",
    ]
    (tmp_path / "architecture-boundaries.json").write_text(json.dumps(policy), encoding="utf-8")

    files = {
        "apps/api/src/marketing_agents/__init__.py": "",
        "apps/api/src/marketing_agents/domain/leak.py": (
            "from fastapi import APIRouter\nfrom marketing_agents.api import app\n"
        ),
        "apps/api/src/marketing_agents/application/service.py": (
            "from tests.helpers import fixture\n"
        ),
        "apps/api/src/marketing_agents/application/framework_leak.py": (
            "import boto3\nimport fastapi\n"
        ),
        "apps/api/src/marketing_agents/application/ports/leak.py": (
            "from marketing_agents.infrastructure.db import Base\n"
        ),
        "apps/api/src/marketing_agents/api/orm_leak.py": ("from sqlalchemy.orm import Session\n"),
        "apps/api/src/marketing_agents/api/provider_leak.py": "import openai\n",
        "apps/api/src/marketing_agents/adapters/registry.py": "REGISTRY = object()\n",
        "apps/web/src/main.tsx": 'import "./test/fixture";\n',
        "apps/web/src/test/fixture.ts": "export const fixture = true;\n",
        "apps/web/src/api/bad.ts": ('import {\n  widget,\n} from "../features/widget";\n'),
        "apps/web/src/contracts/bad.ts": 'import "react";\n',
        "apps/web/src/features/widget.ts": 'fetch("/api/v1/widgets");\n',
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    violations = check_repository(tmp_path, load_policy(tmp_path / "architecture-boundaries.json"))
    codes = {violation.code for violation in violations}

    assert {
        "missing-layer-home",
        "legacy-python-layer",
        "python-import-direction",
        "python-unapproved-external-import",
        "api-orm-import",
        "python-production-test-import",
        "impure-python-contract",
        "frontend-api-outward-import",
        "frontend-direct-fetch",
        "frontend-api-literal",
        "frontend-production-test-import",
        "frontend-main-test-dependency",
        "impure-frontend-contract",
    } <= codes
    external_leaks = {
        (violation.path, violation.detail)
        for violation in violations
        if violation.code == "python-unapproved-external-import"
    }
    assert {
        (
            "apps/api/src/marketing_agents/application/framework_leak.py",
            "application imports unapproved third-party package boto3",
        ),
        (
            "apps/api/src/marketing_agents/application/framework_leak.py",
            "application imports unapproved third-party package fastapi",
        ),
        (
            "apps/api/src/marketing_agents/api/provider_leak.py",
            "api imports unapproved third-party package openai",
        ),
    } <= external_leaks


def test_arch_08_typescript_lexer_handles_real_import_syntax_without_inert_text() -> None:
    source = """
"use client"; import {
  /* a semicolon here must not terminate the import: ; */ widget,
} from "../features/widget";
export {
  CatalogContract,
} from "../contracts/catalog";
const lazy = import /* comments may separate tokens */ (
  "../test/lazyFixture"
);
const interpolated = `value: ${import("../test/interpolatedFixture")}`;
export const prose = `from "../features/not-an-import"`;
const quoted = 'import("../test/not-an-import")';
// import "../test/commented-out";
/* export { fake } from "../test/commented-out"; */
"""

    assert _typescript_imports(source) == [
        ("../features/widget", 2),
        ("../contracts/catalog", 5),
        ("../test/lazyFixture", 8),
        ("../test/interpolatedFixture", 11),
    ]


def test_arch_08_typescript_lexer_finds_fetch_and_api_literals_without_inert_text() -> None:
    source = """
fetch("/api/v1/plain");
window.fetch("/api/v1/property");
globalThis["fetch"](`/api/v1/${itemId}`);
self?.["fetch"](`${origin}/api/v1/tail`);
const prose = 'fetch("/api/v1/inert")';
const template = `window.fetch("/api/v1/inert")`;
function fetch(path: string): string { return path; }
const owner = { fetch(path: string): string { return path; } };
// fetch("/api/v1/comment");
/* window["fetch"]("/api/v1/comment"); */
"""

    fetch_offsets, api_literal_offsets = _typescript_ui_network_ownership(source)

    def lines(offsets: tuple[int, ...]) -> list[int]:
        return [source.count("\n", 0, offset) + 1 for offset in offsets]

    assert lines(fetch_offsets) == [2, 3, 4, 5]
    assert lines(api_literal_offsets) == [2, 3, 4, 5]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python.source_root", "../outside"),
        ("frontend.source_root", "/tmp/outside"),
        ("frontend.entrypoint", "apps/web/src/../outside.tsx"),
    ],
)
def test_arch_08_policy_paths_must_be_canonical_and_repo_relative(
    tmp_path: Path, field: str, value: str
) -> None:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    section, key = field.split(".", 1)
    policy[section][key] = value
    policy_path = tmp_path / "architecture-boundaries.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(BoundaryPolicyError, match=r"repository|inside"):
        load_policy(policy_path)


@pytest.mark.parametrize(
    ("section", "key", "expected_code"),
    [
        ("python", "source_root", "missing-python-source-root"),
        ("frontend", "source_root", "missing-frontend-source-root"),
        ("frontend", "entrypoint", "missing-frontend-entrypoint"),
    ],
)
def test_arch_08_missing_configured_source_paths_fail_closed(
    section: str, key: str, expected_code: str
) -> None:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy[section][key] = "missing/arch-08/path"

    codes = {violation.code for violation in check_repository(ROOT, policy)}

    assert expected_code in codes
