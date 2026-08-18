#!/usr/bin/env python3
"""Verify the authoritative catalog against its committed semantic release lock."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from marketing_agents.infrastructure.catalog import compile_catalog

TOP_LEVEL_KEYS = {
    "schema_version",
    "format_version",
    "content_version",
    "content_hash",
    "counts",
    "department_instance_counts",
}
COUNT_KEYS = {"departments", "functions", "templates", "instances"}


def verify_release(root: Path, lock_path: Path) -> dict[str, Any]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or set(lock) != TOP_LEVEL_KEYS:
        raise ValueError("catalog release lock has an unexpected top-level shape")
    if lock["schema_version"] != 1:
        raise ValueError("catalog release lock schema version is unsupported")
    if not isinstance(lock["counts"], dict) or set(lock["counts"]) != COUNT_KEYS:
        raise ValueError("catalog release lock has an unexpected count shape")
    compiled = compile_catalog(root)
    actual = {
        "schema_version": 1,
        "format_version": compiled.manifest.format_version,
        "content_version": compiled.manifest.content_version,
        "content_hash": compiled.content_hash,
        "counts": {
            "departments": len(compiled.departments),
            "functions": len(compiled.functions),
            "templates": len(compiled.templates),
            "instances": len(compiled.instances),
        },
        "department_instance_counts": dict(compiled.department_instance_counts),
    }
    if lock != actual:
        raise ValueError("compiled catalog does not match the committed release lock")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("catalog/v1"))
    parser.add_argument("--lock", type=Path, default=Path("catalog/v1/release.lock.json"))
    args = parser.parse_args()
    try:
        result = verify_release(args.root, args.lock)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"valid": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
