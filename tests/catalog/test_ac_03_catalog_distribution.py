"""AC-03 compiler and release-lock distribution acceptance."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml
from marketing_agents.infrastructure.catalog import (
    CatalogCompilationError,
    CatalogContract,
    compile_catalog,
)

from scripts.verify_catalog_release import verify_release

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "catalog" / "v1"
RELEASE_LOCK = CATALOG_ROOT / "release.lock.json"
GLOBAL_COUNTS = {
    "departments": 5,
    "functions": 12,
    "templates": 36,
    "instances": 43,
}
EXPECTED_DRIFTED_DISTRIBUTION = {
    "dept.social-media": 11,
    "dept.blog-seo": 7,
    "dept.email": 5,
    "dept.community": 14,
    "dept.partnerships": 6,
}
MOVED_TEMPLATE_ID = "tpl.social-media.new-content.linkedin-post-drafter"


def _copy_catalog(tmp_path: Path) -> Path:
    copied_catalog = tmp_path / "catalog"
    shutil.copytree(ROOT / "catalog", copied_catalog)
    return copied_catalog / "v1"


def _move_one_social_template_to_blog(catalog_root: Path) -> None:
    template_path = catalog_root / "templates" / "social-media.yaml"
    payload = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    template = next(item for item in payload["templates"] if item["id"] == MOVED_TEMPLATE_ID)
    template["department_id"] = "dept.blog-seo"
    template["function_id"] = "func.blog-seo.new-content"
    template["display_order"] = 40
    template_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def test_ac_03_default_compiler_rejects_count_preserving_distribution_drift(
    tmp_path: Path,
) -> None:
    copied_root = _copy_catalog(tmp_path)
    _move_one_social_template_to_blog(copied_root)

    # A deliberately weaker global-count-only contract proves the changed fixture
    # still compiles coherently with 5/12/36/43; it is not AC-03 acceptance.
    weak_contract = CatalogContract(
        departments=5,
        functions=12,
        templates=36,
        instances=43,
    )
    weakly_compiled = compile_catalog(copied_root, contract=weak_contract)
    assert {
        "departments": len(weakly_compiled.departments),
        "functions": len(weakly_compiled.functions),
        "templates": len(weakly_compiled.templates),
        "instances": len(weakly_compiled.instances),
    } == GLOBAL_COUNTS
    assert dict(weakly_compiled.department_instance_counts) == EXPECTED_DRIFTED_DISTRIBUTION

    with pytest.raises(CatalogCompilationError) as captured:
        compile_catalog(copied_root)

    issue_codes = {issue.code for issue in captured.value.issues}
    assert "contract-distribution" in issue_codes
    assert "contract-count" not in issue_codes


def test_ac_03_release_lock_rejects_distribution_only_tamper(
    tmp_path: Path,
) -> None:
    original = json.loads(RELEASE_LOCK.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(original)
    tampered["department_instance_counts"] = dict(EXPECTED_DRIFTED_DISTRIBUTION)

    assert tampered["counts"] == original["counts"] == GLOBAL_COUNTS
    assert tampered["content_hash"] == original["content_hash"]
    assert sum(tampered["department_instance_counts"].values()) == 43
    assert {key for key in tampered if tampered[key] != original[key]} == {
        "department_instance_counts"
    }

    tampered_lock = tmp_path / "distribution-only-release.lock.json"
    tampered_lock.write_text(
        json.dumps(tampered, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="compiled catalog does not match the committed release lock",
    ):
        verify_release(CATALOG_ROOT, tampered_lock)
