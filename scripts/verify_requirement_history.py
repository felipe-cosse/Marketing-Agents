#!/usr/bin/env python3
"""Verify the one-branch/commit/merge contract for traceability IDs."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/implementation-plan/16-requirements-traceability-matrix.md"
ID_PATTERN = re.compile(r"^\| ([A-Z]+-\d+) \|")
COMMIT_PATTERN = re.compile(r"^\[([A-Z]+-\d+)\]\s+")
MERGE_PATTERN = re.compile(r"^merge:\s+([A-Z]+-\d+)\b", re.IGNORECASE)


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def requirement_ids() -> list[str]:
    ids = [
        match.group(1)
        for line in MATRIX.read_text(encoding="utf-8").splitlines()
        if (match := ID_PATTERN.match(line))
    ]
    if len(ids) != len(set(ids)):
        raise RuntimeError("traceability matrix contains duplicate requirement IDs")
    return ids


def subjects(ref: str) -> list[str]:
    output = run_git("log", ref, "--format=%s")
    return [line for line in output.splitlines() if line]


def branch_names() -> set[str]:
    output = run_git("for-each-ref", "--format=%(refname:short)", "refs/heads/req/")
    return {line for line in output.splitlines() if line}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default="main")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--check-branches", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    required = requirement_ids()
    required_set = set(required)
    history = subjects(args.ref)
    commit_counts = Counter(
        match.group(1).upper()
        for subject in history
        if (match := COMMIT_PATTERN.match(subject))
    )
    merge_counts = Counter(
        match.group(1).upper()
        for subject in history
        if (match := MERGE_PATTERN.match(subject))
    )

    duplicate_commits = sorted(key for key, count in commit_counts.items() if count != 1)
    duplicate_merges = sorted(key for key, count in merge_counts.items() if count != 1)
    unexpected = sorted((set(commit_counts) | set(merge_counts)) - required_set)
    completed = sorted(
        requirement_id
        for requirement_id in required
        if commit_counts[requirement_id] == 1 and merge_counts[requirement_id] == 1
    )
    missing = sorted(required_set - set(completed))

    missing_branches: list[str] = []
    if args.check_branches:
        branches = branch_names()
        missing_branches = [
            requirement_id
            for requirement_id in required
            if not any(
                branch.startswith(f"req/{requirement_id.lower()}") for branch in branches
            )
        ]

    print(f"requirements={len(required)} completed={len(completed)} missing={len(missing)}")
    if missing:
        print("missing=" + ",".join(missing))
    if duplicate_commits:
        print("invalid_commit_counts=" + ",".join(duplicate_commits))
    if duplicate_merges:
        print("invalid_merge_counts=" + ",".join(duplicate_merges))
    if unexpected:
        print("unexpected_ids=" + ",".join(unexpected))
    if missing_branches:
        print("missing_branches=" + ",".join(missing_branches))

    invalid = duplicate_commits or duplicate_merges or unexpected or missing_branches
    if invalid:
        return 1
    if missing and not args.allow_incomplete:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
