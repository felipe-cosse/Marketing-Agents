"""DEL-07: offline repository text hygiene, structured parsing, and Markdown references.

The format policy is UTF-8/LF, space indentation, no trailing whitespace (except
Markdown's two-space hard break), and a final newline. JSON/YAML must parse with
no duplicate keys. This does not impose Prettier layout on historical evidence;
frontend files and generated API artifacts have their separate canonical checks.
Original files under references/ are byte-preserved, checked by verify-source.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from scripts.verify_product_docs import LINK, heading_anchors, local_links

ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = {".json", ".yaml", ".yml", ".md"}
EXTERNAL_HOSTS = frozenset(
    {
        "alembic.sqlalchemy.org",
        "docs.sqlalchemy.org",
        "github.com",
        "www.postgresql.org",
        "openapi-ts.dev",
        "nodejs.org",
        "www.linkedin.com",
    }
)
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class TextPolicyError(ValueError):
    """Stable path/reason diagnostics, never file contents or credentials."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TextPolicyError("duplicate_key")
        result[key] = value
    return result


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_yaml_mapping(loader, node):
    # YAML merge keys deliberately permit local overrides (used by Compose).
    # Reject duplicate explicit keys before flattening those inherited defaults.
    unique_object(
        (loader.construct_object(key, deep=True), None)
        for key, _value in node.value
        if key.tag != "tag:yaml.org,2002:merge"
    )
    loader.flatten_mapping(node)
    return loader.construct_mapping(node, deep=True)


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_yaml_mapping)


def reject_constant(_value):
    raise TextPolicyError("nonfinite_json_number")


def check_external_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise TextPolicyError("malformed_external_url") from exc
    if (
        any(char.isspace() or ord(char) < 32 for char in value)
        or parsed.username is not None
        or parsed.password is not None
        or hostname is None
        or (port is not None and not 1 <= port <= 65535)
        or parsed.scheme not in {"http", "https"}
    ):
        raise TextPolicyError("malformed_external_url")
    if hostname in LOCAL_HOSTS:
        return
    if parsed.scheme != "https" or hostname not in EXTERNAL_HOSTS or port not in {None, 443}:
        raise TextPolicyError("unapproved_external_url")


def check_text(root: Path, relative: str) -> None:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise TextPolicyError("unsupported_symlink_or_escape")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if not text or not text.endswith("\n") or "\r" in text or "\x00" in text or "\t" in text:
        raise TextPolicyError("utf8_lf_spaces_and_final_newline_required")
    for line in text.splitlines():
        trailing = len(line) - len(line.rstrip(" "))
        if trailing and not (path.suffix == ".md" and trailing == 2 and line.strip()):
            raise TextPolicyError("trailing_whitespace")
    if path.suffix == ".json":
        json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    elif path.suffix in {".yaml", ".yml"}:
        list(yaml.load_all(text, Loader=UniqueLoader))
    elif path.suffix == ".md":
        # Also check reference definitions and autolinks. Literal examples are
        # intentionally checked too; no URL is ever fetched.
        extra = "\n".join(
            f"[reference]({match})"
            for match in re.findall(r"^\s*\[[^\]\n]+\]:\s*(\S+)", text, re.MULTILINE)
        )
        extra += "\n" + "\n".join(
            f"[autolink]({match})" for match in re.findall(r"<(https?://[^>]+)>", text)
        )
        combined = text + "\n" + extra
        for match in LINK.finditer(combined):
            value = match.group(1).strip().removeprefix("<").removesuffix(">")
            if urlsplit(value).scheme:
                check_external_url(value)
        for target, anchor in local_links(root, relative, combined):
            if not target.exists():
                raise TextPolicyError("missing_local_reference")
            if anchor and (
                target.suffix != ".md"
                or not target.is_file()
                or anchor not in heading_anchors(target.read_text(encoding="utf-8"))
            ):
                raise TextPolicyError("missing_local_anchor")


def verify(root: Path) -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = sorted({value.decode("utf-8") for value in result.stdout.split(b"\0") if value})
    selected = [
        path
        for path in paths
        if Path(path).suffix in EXTENSIONS and not path.startswith("references/")
    ]
    if not selected:
        raise TextPolicyError("empty_text_inventory")
    for relative in selected:
        try:
            check_text(root, relative)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            # Parser exceptions often contain excerpts; do not echo them.
            reason = str(exc) if isinstance(exc, TextPolicyError) else type(exc).__name__
            raise TextPolicyError(f"{relative}:{reason}") from None
    return len(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        count = verify(args.root.resolve())
    except (TextPolicyError, subprocess.SubprocessError) as exc:
        print(f"DEL-07 repository text failed: {exc}")
        return 1
    print(f"DEL-07 repository text verified: {count} files; no external URLs fetched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
