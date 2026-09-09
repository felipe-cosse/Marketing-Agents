#!/usr/bin/env python3
"""Expose fetched, retained GitHub branches to the unchanged history verifier."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REQUIREMENT_BRANCH = re.compile(r"req/[a-z]+-[0-9]{2}-[a-z0-9][a-z0-9-]*\Z")


class HistoryRefsError(RuntimeError):
    """The fetched branches cannot safely represent the retained history."""


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise HistoryRefsError(result.stderr.strip() or "Git history preparation failed")
    return result.stdout.strip()


def _refs(root: Path, prefix: str) -> dict[str, str]:
    raw = _git(root, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)", prefix)
    result: dict[str, str] = {}
    for line in raw.splitlines():
        fields = line.split("\t")
        name, sha = fields[:2]
        if len(fields) > 2 and fields[2]:
            raise HistoryRefsError(f"symbolic history ref is forbidden: {name}")
        result[name] = sha
    return result


def prepare_history_refs(root: Path) -> int:
    """Copy only real origin refs; never invent branches from merge parents.

    actions/checkout with fetch-depth: 0 fetches remote-tracking refs, but the
    repository's retained-branch verifier intentionally checks local heads.
    All validation precedes one atomic transaction; existing refs are never
    moved or deleted, and a detached pull-request checkout remains detached.
    """
    if _git(root, "rev-parse", "--is-shallow-repository") != "false":
        raise HistoryRefsError("full history is required; use checkout fetch-depth: 0")

    origin_main = _refs(root, "refs/remotes/origin/main")
    main_source = "refs/remotes/origin/main"
    if main_source not in origin_main:
        raise HistoryRefsError("missing fetched origin/main; cannot establish the main history")
    sources = _refs(root, "refs/remotes/origin/req/")
    if not sources:
        raise HistoryRefsError(
            "no retained origin/req/* branches were fetched; publish the retained requirement "
            "branches before running CI (merge commits alone do not retain branch names)"
        )
    for source in sources:
        branch = source.removeprefix("refs/remotes/origin/")
        if not REQUIREMENT_BRANCH.fullmatch(branch):
            raise HistoryRefsError(f"invalid retained requirement branch: {branch}")
    sources[main_source] = origin_main[main_source]

    existing = _refs(root, "refs/heads/req/")
    existing.update(_refs(root, "refs/heads/main"))
    targets = {
        source.replace("refs/remotes/origin/", "refs/heads/", 1): sha
        for source, sha in sources.items()
    }
    for target, sha in existing.items():
        if target.startswith("refs/heads/req/") and target not in targets:
            raise HistoryRefsError(
                f"local retained branch has no fetched origin counterpart: {target}"
            )
        if target in targets and targets[target] != sha:
            raise HistoryRefsError(
                f"local branch conflicts with fetched origin; refusing to move {target}"
            )
    for source, sha in sources.items():
        if _git(root, "cat-file", "-t", sha) != "commit":
            raise HistoryRefsError(
                f"retained history ref must point directly to a commit: {source}"
            )

    commands = ["start"]
    created = 0
    for source, sha in sorted(sources.items()):
        commands.append(f"verify {source} {sha}")
        target = source.replace("refs/remotes/origin/", "refs/heads/", 1)
        if target in existing:
            commands.append(f"verify {target} {sha}")
        else:
            commands.append(f"create {target} {sha}")
            created += 1
    commands.extend(["prepare", "commit"])
    _git(root, "update-ref", "--no-deref", "--stdin", input_text="\n".join(commands) + "\n")
    return created


def main() -> int:
    try:
        count = prepare_history_refs(Path.cwd())
    except (HistoryRefsError, OSError) as exc:
        print(f"CI history preparation failed: {exc}")
        return 1
    print(f"CI history refs ready: {count} local branches created from fetched origin refs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
