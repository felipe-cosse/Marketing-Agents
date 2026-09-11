#!/usr/bin/env python3
"""DEL-06: read-only, offline checks for the required product documentation.

These checks catch omissions and mechanical drift, not arbitrary semantic errors
in prose. Human review and the referenced behavior tests remain necessary.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
GUIDES = (
    "README.md",
    "docs/architecture.md",
    "docs/assumptions.md",
    "docs/security.md",
    "docs/identity-and-authorization.md",
    "docs/data-handling.md",
    "docs/adapter-contracts.md",
    "docs/catalog-authoring.md",
    "docs/testing.md",
    "docs/operations.md",
    "docs/demos.md",
    "docs/verification.md",
    "docs/local-operations.md",
)
CLAIM_LABELS = (
    "implemented and verified",
    "implemented but not live-tested",
    "deterministic mock behavior",
    "acceptance target not yet verified",
    "deferred real-adapter work",
    "assumption",
    "residual risk",
)
LINK = re.compile(r"!?\[[^\]\n]+\]\((<[^>\n]+>|[^)\n]+)\)")
SHELL_BLOCK = re.compile(r"^```(?:sh|bash|shell)\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)
MAKE_RULE = re.compile(r"^([A-Za-z0-9_.-]+(?: +[A-Za-z0-9_.-]+)*):(?!=)", re.MULTILINE)


class DocumentationError(ValueError):
    """A stable, non-secret documentation-contract failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DocumentationError(message)


def plain(text: str) -> str:
    return " ".join(re.sub(r"[*`]", "", text).split())


def local_links(root: Path, relative: str, text: str) -> list[tuple[Path, str]]:
    """Resolve inline Markdown links, without fetching any external destination."""
    result = []
    for match in LINK.finditer(text):
        raw = match.group(1).strip().removeprefix("<").removesuffix(">")
        parts = urlsplit(raw)
        if parts.scheme in {"http", "https", "mailto"}:
            continue
        require(not parts.scheme and not parts.netloc, f"unsupported_link:{relative}")
        require(not parts.query, f"local_link_query:{relative}")
        path = (root / relative).parent / unquote(parts.path) if parts.path else root / relative
        path = path.resolve()
        require(path.is_relative_to(root.resolve()), f"escaped_link:{relative}")
        result.append((path, unquote(parts.fragment)))
    return result


def heading_anchors(text: str) -> set[str]:
    """GitHub-style anchors for the ordinary ATX headings used in these guides."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            label = re.sub(r"[^\w\- ]", "", match.group(1).lower()).replace(" ", "-")
            index = counts.get(label, 0)
            counts[label] = index + 1
            anchors.add(f"{label}-{index}" if index else label)
    return anchors


def shell_make_targets(text: str) -> set[str]:
    """Inspect literal make commands; never execute documentation examples."""
    targets: set[str] = set()
    for block in SHELL_BLOCK.findall(text):
        for line in block.replace("\\\n", " ").splitlines():
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            tokens = list(lexer)
            for index, command in enumerate(tokens):
                if command != "make":
                    continue
                for token in tokens[index + 1 :]:
                    if token and all(char in ";&|<>" for char in token):
                        break
                    # Inspect every command segment, including an attached
                    # semicolon; never run documentation examples.
                    require(not token.startswith("-"), "unsupported_make_option_in_example")
                    if "=" not in token:
                        require(
                            bool(re.fullmatch(r"[a-zA-Z0-9_.-]+", token)), "nonliteral_make_target"
                        )
                        targets.add(token)
    return targets


def verify(root: Path = ROOT) -> dict[str, int]:
    root = root.resolve()
    documents: dict[str, str] = {}
    links_checked = 0
    targets_checked: set[str] = set()
    rules = {
        target
        for group in MAKE_RULE.findall((root / "Makefile").read_text(encoding="utf-8"))
        for target in group.split()
        if target != ".PHONY"
    }
    for relative in GUIDES:
        path = root / relative
        require(path.is_file() and not path.is_symlink(), f"missing_guide:{relative}")
        text = path.read_text(encoding="utf-8")
        documents[relative] = text
        require(text.startswith("# "), f"missing_title:{relative}")
        require(len(re.findall(r"^## ", text, re.MULTILINE)) >= 2, f"missing_sections:{relative}")
        require(len(text.strip()) >= 400, f"incomplete_guide:{relative}")
        require(not re.search(r"\b(?:TODO|TBD)\b", text, re.IGNORECASE), f"placeholder:{relative}")
        normalized = plain(text).lower()
        require(
            any(label in normalized for label in CLAIM_LABELS), f"unclassified_claims:{relative}"
        )
        for destination, fragment in local_links(root, relative, text):
            require(destination.exists(), f"broken_link:{relative}:{destination.relative_to(root)}")
            if fragment:
                require(destination.is_file(), f"anchor_on_directory:{relative}")
                require(destination.suffix == ".md", f"unsupported_anchor:{relative}")
                require(
                    fragment in heading_anchors(destination.read_text(encoding="utf-8")),
                    f"broken_anchor:{relative}:{fragment}",
                )
            links_checked += 1
        for target in shell_make_targets(text):
            require(target in rules, f"unknown_make_target:{relative}:{target}")
            targets_checked.add(target)

    readme = plain(documents["README.md"])
    destinations = {path for path, _ in local_links(root, "README.md", documents["README.md"])}
    for relative in GUIDES[1:]:
        require(
            (root / relative).resolve() in destinations, f"missing_readme_navigation:{relative}"
        )
    lock = json.loads((root / "catalog/v1/release.lock.json").read_text(encoding="utf-8"))
    for key, label in (
        ("departments", "departments"),
        ("functions", "functions"),
        ("templates", "role templates"),
        ("instances", "instances"),
    ):
        require(f"{lock['counts'][key]} {label}" in readme, f"catalog_count_drift:{key}")
    require(lock["content_hash"] in documents["docs/verification.md"], "catalog_hash_drift")
    versions = {
        "Python": (root / ".python-version").read_text(encoding="utf-8").strip(),
        "Node": (root / ".nvmrc").read_text(encoding="utf-8").strip(),
        "pnpm": json.loads((root / "package.json").read_text(encoding="utf-8"))[
            "packageManager"
        ].split("@")[-1],
    }
    for tool, version in versions.items():
        require(f"{tool} {version}" in readme, f"toolchain_drift:{tool}")
    assumptions = documents["docs/assumptions.md"]
    ids = re.findall(r"^\| (ASM-\d{3}) \|", assumptions, re.MULTILINE)
    require(ids == [f"ASM-{index:03}" for index in range(1, 25)], "assumption_inventory_drift")
    require(
        re.search(r"\| ASM-017 \| accepted \|", assumptions) is not None,
        "lifecycle_decision_not_closed",
    )
    for label in CLAIM_LABELS:
        require(
            label in plain(documents["docs/verification.md"]).lower(), f"missing_taxonomy:{label}"
        )
    return {
        "guides": len(documents),
        "local_links": links_checked,
        "make_targets": len(targets_checked),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = verify(args.root)
    except (DocumentationError, OSError, ValueError, KeyError) as exc:
        print(f"DEL-06 documentation verification failed: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
