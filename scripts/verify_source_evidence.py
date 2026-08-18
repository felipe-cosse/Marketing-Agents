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
EXPECTED_PROMPT = "IMPLEMENTATION_PROMPT.md"
EXPECTED_INDEX = "catalog/source-evidence.json"
EXPECTED_GUIDANCE_NAMES = [
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".cursorrules",
    ".agents",
    ".codex",
]
EXPECTED_SUBJECTS = [
    "repository guidance",
    "implementation prompt",
    "overview frame",
    "Social media frame",
    "Blog & SEO frame",
    "Email frame",
    "Community frame",
    "Partnerships frame",
]
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


def _discover_guidance(root: Path) -> list[str]:
    discovered: set[str] = set()
    file_names = set(EXPECTED_GUIDANCE_NAMES[:4])
    directory_names = set(EXPECTED_GUIDANCE_NAMES[4:])
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_file() and path.name in file_names:
            discovered.add(relative.as_posix())
        if path.is_dir() and path.name in directory_names:
            discovered.add(relative.as_posix())
    return sorted(discovered)


def verify(root: Path = ROOT) -> EvidenceResult:
    index_path = root / SOURCE_INDEX.relative_to(ROOT)
    inspection_path = root / INSPECTION.relative_to(ROOT)
    index = _read_json(index_path)
    inspection = _read_json(inspection_path)
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
    if inspection.get("required_prompt") != EXPECTED_PROMPT:
        raise ValueError("inspection must use the fixed implementation prompt path")
    if inspection.get("reference_index") != EXPECTED_INDEX or index_path != root / EXPECTED_INDEX:
        raise ValueError("inspection must use the fixed source-evidence index")
    prompt_path = (root / EXPECTED_PROMPT).resolve()
    if root.resolve() not in prompt_path.parents or not prompt_path.is_file():
        raise ValueError("implementation prompt is missing or escaped")
    prompt_hash = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    if inspection.get("required_prompt_sha256") != prompt_hash:
        raise ValueError("implementation prompt hash mismatch")
    if inspection.get("guidance_names_checked") != EXPECTED_GUIDANCE_NAMES:
        raise ValueError("guidance candidate list is incomplete or reordered")
    found = inspection.get("repository_guidance_files_found")
    if not isinstance(found, list):
        raise ValueError("repository guidance result must be a list")
    if found != _discover_guidance(root):
        raise ValueError("recorded repository guidance does not match filesystem discovery")
    subjects = inspection.get("inspected_subjects")
    if subjects != EXPECTED_SUBJECTS or len(set(subjects)) != len(EXPECTED_SUBJECTS):
        raise ValueError("inspection must cover the exact unique guidance, prompt, and frame subjects")
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
