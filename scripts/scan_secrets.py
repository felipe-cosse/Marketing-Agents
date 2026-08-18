#!/usr/bin/env python3
"""Fail on secret-like tracked files or high-confidence credential material."""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BYTES = 1_500_000
FORBIDDEN_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite3",
    "*.sqlite-*",
)
CONTENT_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("provider-token", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|client[_-]?secret|password|access[_-]?token)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{16,}"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str


def _git_paths(*args: str, root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", *args, "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)


def tracked_paths(root: Path = ROOT) -> list[str]:
    return _git_paths("ls-files", root=root)


def changed_paths(base: str, root: Path = ROOT) -> list[str]:
    return _git_paths("diff", "--name-only", f"{base}...HEAD", root=root)


def forbidden_filename(relative: str) -> bool:
    name = Path(relative).name
    if name == ".env.example":
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in FORBIDDEN_FILE_PATTERNS)


def scan_paths(root: Path, relative_paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for relative in sorted(set(relative_paths)):
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            findings.append(Finding(relative, "escaped-path"))
            continue
        path = root / relative
        if forbidden_filename(relative):
            findings.append(Finding(relative, "forbidden-secret-or-state-file"))
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_TEXT_BYTES:
            continue
        payload = path.read_bytes()
        if b"\0" in payload[:8192]:
            continue
        text = payload.decode("utf-8", errors="replace")
        for kind, pattern in CONTENT_PATTERNS:
            if pattern.search(text):
                findings.append(Finding(relative, kind))
    return sorted(set(findings), key=lambda item: (item.path, item.kind))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracked", action="store_true", help="scan every tracked path")
    parser.add_argument("--base", help="also scan paths changed from this Git base")
    args = parser.parse_args()
    if not args.tracked and args.base is None:
        parser.error("select --tracked, --base, or both")
    paths: list[str] = []
    if args.tracked:
        paths.extend(tracked_paths())
    if args.base is not None:
        paths.extend(changed_paths(args.base))
    findings = scan_paths(ROOT, paths)
    for finding in findings:
        print(f"{finding.path}: {finding.kind}")
    if findings:
        print(f"secret scan failed with {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"secret scan passed for {len(set(paths))} path(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
