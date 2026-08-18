"""DEL-02: ship a validated, hash-pinned 36-template/43-instance catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_catalog_release import verify_release

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"
LOCK = CATALOG / "release.lock.json"


def test_del_02_release_lock_matches_authoritative_catalog() -> None:
    release = verify_release(CATALOG, LOCK)
    assert release["counts"] == {
        "departments": 5,
        "functions": 12,
        "templates": 36,
        "instances": 43,
    }
    assert release["department_instance_counts"] == {
        "dept.social-media": 12,
        "dept.blog-seo": 6,
        "dept.email": 5,
        "dept.community": 14,
        "dept.partnerships": 6,
    }
    assert release["content_hash"].startswith("catalog-sha256-v1:")


def test_del_02_hash_or_count_tampering_fails_closed(tmp_path: Path) -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    lock["content_hash"] = "catalog-sha256-v1:" + "0" * 64
    changed_hash = tmp_path / "changed-hash.json"
    changed_hash.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        verify_release(CATALOG, changed_hash)

    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    lock["counts"]["instances"] = 42
    changed_count = tmp_path / "changed-count.json"
    changed_count.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        verify_release(CATALOG, changed_count)


def test_del_02_unknown_release_lock_fields_are_rejected(tmp_path: Path) -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    lock["unreviewed"] = True
    changed = tmp_path / "unknown.json"
    changed.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected top-level shape"):
        verify_release(CATALOG, changed)
