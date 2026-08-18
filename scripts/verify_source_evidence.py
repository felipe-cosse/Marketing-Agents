#!/usr/bin/env python3
"""Verify the immutable local evidence set used by the implementation."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_INDEX = ROOT / "catalog" / "source-evidence.json"
INSPECTION = ROOT / "docs" / "implementation" / "repository-inspection.json"
EXPECTED_FRAMES = {
    "linkedin-ai-agents-org-chart-overview.png",
    "linkedin-ai-agents-org-chart-social-media.png",
    "linkedin-ai-agents-org-chart-blog-seo.png",
    "linkedin-ai-agents-org-chart-email.png",
    "linkedin-ai-agents-org-chart-community.png",
    "linkedin-ai-agents-org-chart-partnerships.png",
}


@dataclass(frozen=True)
class EvidenceResult:
    frame_count: int
    inspected_subject_count: int


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(root: Path = ROOT) -> EvidenceResult:
    index = _read_json(root / SOURCE_INDEX.relative_to(ROOT))
    inspection = _read_json(root / INSPECTION.relative_to(ROOT))
    assets = index.get("assets")
    if not isinstance(assets, list):
        raise ValueError("source evidence assets must be a list")

    names: set[str] = set()
    for item in assets:
        if not isinstance(item, dict):
            raise ValueError("source evidence asset must be an object")
        relative = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(relative, str) or not relative.startswith("references/"):
            raise ValueError("evidence path must be under references/")
        path = (root / relative).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            raise ValueError(f"missing or escaped evidence path: {relative}")
        if path.suffix.lower() != ".png" or path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"evidence is not a PNG: {relative}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"evidence hash mismatch: {relative}")
        names.add(path.name)

    if names != EXPECTED_FRAMES:
        raise ValueError("source frame inventory does not match the required six frames")
    if not (root / str(inspection.get("required_prompt"))).is_file():
        raise ValueError("implementation prompt is missing")
    found = inspection.get("repository_guidance_files_found")
    if not isinstance(found, list):
        raise ValueError("repository guidance result must be a list")
    for relative in found:
        if not isinstance(relative, str) or not (root / relative).is_file():
            raise ValueError(f"recorded guidance file is missing: {relative}")
    subjects = inspection.get("inspected_subjects")
    if not isinstance(subjects, list) or len(subjects) != 8:
        raise ValueError("inspection must cover guidance, prompt, and all six frames")
    return EvidenceResult(frame_count=len(assets), inspected_subject_count=len(subjects))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a JSON result")
    args = parser.parse_args()
    result = verify()
    if args.json:
        print(json.dumps({"frames": result.frame_count, "subjects": result.inspected_subject_count}))
    else:
        print(f"verified {result.frame_count} source frames and {result.inspected_subject_count} inspection subjects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
