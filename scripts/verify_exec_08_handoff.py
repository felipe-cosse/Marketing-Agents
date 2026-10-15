"""EXEC-08: bounded, read-only documentation contracts, not execution authentication.

This offline checker detects curated omissions and scope promotions in one guide.
It does not execute commands, import product code, validate reports or establish
that the documented tests ran. Independent review and source-bound gates remain
necessary; arbitrary prose truth is outside this documentation-only contract.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
from pathlib import Path

from scripts.verify_product_docs import DocumentationError, plain, require

ROOT = Path(__file__).resolve().parents[1]
INPUT = "docs/verification.md"
MAX_INPUT_BYTES = 128 * 1024
HEADING = "EXEC-08 current verification handoff"
HISTORICAL_HEADING = "AC-17 current documentation review"
BASELINE_IDENTITIES = (
    "a8e197da4ca9f4a7abbc842af737b1c8fbecb2ef",
    "63df4bb4f39f22d31e9c3638b5e7e6e4049a4650",
    "b8eccaaf6cd91a1cb7ac64bbb850187e13e8e93f",
)
CHANGED_FILES = (
    "Makefile",
    "docs/verification.md",
    "docs/assumptions.md",
    "scripts/verify_exec_08_handoff.py",
    "tests/tooling/test_exec_08_handoff.py",
    "docs/implementation-plan/16-requirements-traceability-matrix.md",
    "docs/verification/requirements/EXEC-08.json",
    "docs/verification/requirements/EXEC-08.md",
)
SECTION_PHRASES = {
    "Source and qualification boundary": (
        "Inherited EXEC-07 baseline",
        "not fresh EXEC-08 verification",
        *BASELINE_IDENTITIES,
    ),
    "Files changed": CHANGED_FILES,
    "Commands and observed baseline results": (
        "python -B -m scripts.ac_16_quality run",
        "make verify-clean REF=HEAD",
        "make test-del-05-compose-backup",
        "81 catalog tests passed",
        "4,932 other backend tests passed",
        "33 optional PostgreSQL tests skipped",
        "472 frontend tests in 55 files",
        "17 safety modules",
        "16 browser runners",
        "5,013 backend tests passed and 416 subtests passed",
        "Failures, errors, skips and subtests are reported separately",
        "subtests are not additional test cases",
    ),
    "Candidate verification and handoff": (
        "Candidate verification is pending",
        "all 20 quality gates",
        "complete clean-state journey",
        "paired backup/restore",
        "connection witness",
        "external source-bound attestation",
        "documentation checks do not authenticate execution",
    ),
    "Assumptions and remaining limits": (
        "CI remains disabled, not green",
        "Historical AC-15 evidence is not a fresh four-suite run",
        "DEL-01 remains pending",
        "Deterministic mock behavior is not live-provider delivery",
        "PostgreSQL skips are not passes",
        "credentials, raw payloads, databases and secret-bearing backups "
        "are not verification artifacts",
    ),
}
UNSUPPORTED_PROMOTIONS = (
    "EXEC-08 full verification passed",
    "CI is green",
    "DEL-01 is complete",
    "PostgreSQL is qualified",
    "AC-15 four-suite verification was rerun",
    "live-provider delivery verified",
)


def read_guide(root: Path) -> str:
    """Read only the fixed regular guide, bounded and without symlink inputs."""
    require(root.is_dir() and not root.is_symlink(), "exec08_input_root")
    root = root.resolve()
    path = root / INPUT
    require(
        not (root / "docs").is_symlink()
        and (root / "docs").is_dir()
        and path.is_file()
        and not path.is_symlink()
        and path.resolve().is_relative_to(root),
        "exec08_input_path",
    )
    require(stat.S_ISREG(path.stat().st_mode), "exec08_input_path")
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    require(len(raw) <= MAX_INPUT_BYTES, "exec08_input_bound")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentationError("exec08_input_encoding") from error


def visible_text(text: str) -> str:
    """Exclude comments and fenced examples from literal documentation claims."""
    text = re.sub(r"<!--.*?(?:-->|\Z)", "", text, flags=re.DOTALL)
    lines: list[str] = []
    fenced: tuple[str, int] | None = None
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            marker = match.group(1)
            if fenced is None:
                fenced = marker[0], len(marker)
            elif marker[0] == fenced[0] and len(marker) >= fenced[1] and not match.group(2).strip():
                fenced = None
            lines.append("")
        else:
            # An indented code example cannot satisfy a visible claim either.
            lines.append("" if fenced or line.startswith(("    ", "\t")) else line)
    return "\n".join(lines)


def section(text: str, heading: str, level: int) -> str:
    """Require exactly one literal heading at its declared hierarchy level."""
    pattern = r"^" + "#" * level + " " + re.escape(heading) + r"[ \t]*$"
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    require(len(matches) == 1, f"exec08_section:{heading}")
    start = matches[0].end()
    end = re.search(r"^#{1," + str(level) + r"} ", text[start:], re.MULTILINE)
    return text[start : start + end.start()] if end else text[start:]


def verify(root: Path = ROOT) -> dict[str, str | int | bool]:
    text = visible_text(read_guide(root))
    body = section(text, HEADING, 2)
    require(
        "historical snapshot" in plain(section(text, HISTORICAL_HEADING, 2)).lower(),
        "exec08_historical_marker",
    )
    for heading, phrases in SECTION_PHRASES.items():
        normalized = plain(section(body, heading, 3)).lower()
        for phrase in phrases:
            require(phrase.lower() in normalized, f"exec08_claim:{heading}:{phrase}")
    files = plain(section(body, "Files changed", 3))
    require(all(files.count(path) == 1 for path in CHANGED_FILES), "exec08_changed_files")
    normalized = plain(body).lower()
    for phrase in UNSUPPORTED_PROMOTIONS:
        require(phrase.lower() not in normalized, "exec08_unsupported_promotion")
    return {
        "scope": "documentation-only",
        "guides": 1,
        "handoff_sections": len(SECTION_PHRASES),
        "changed_files": len(CHANGED_FILES),
        "execution_authenticated": False,
        "candidate_verification_established": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = verify(args.root)
    except DocumentationError as error:
        print(f"EXEC-08 documentation verification failed: {error}")
        return 1
    except OSError:
        print("EXEC-08 documentation verification failed: exec08_input_unreadable")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
