"""Bounded, duplicate-key-safe, local-only catalog file loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

MAX_YAML_BYTES = 1_000_000
MAX_JSON_BYTES = 1_000_000
MAX_PROMPT_BYTES = 100_000


class DuplicateKeyError(ValueError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def resolve_local_file(root: Path, relative: str, *, suffixes: set[str]) -> Path:
    if not relative or Path(relative).is_absolute() or urlparse(relative).scheme:
        raise ValueError("catalog reference must be a nonempty relative local path")
    relative_path = Path(relative)
    if ".." in relative_path.parts:
        raise ValueError("catalog reference cannot traverse outside the catalog root")
    root = root.resolve()
    candidate = root / relative_path
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("catalog references cannot resolve through symlinks")
    resolved = candidate.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("catalog reference escaped the catalog root")
    if resolved.suffix.lower() not in suffixes:
        raise ValueError("catalog reference uses an unsupported file extension")
    if not resolved.is_file():
        raise ValueError("catalog reference does not identify a regular file")
    return resolved


def read_bounded(path: Path, *, maximum_bytes: int) -> bytes:
    size = path.stat().st_size
    if size > maximum_bytes:
        raise ValueError(f"catalog file exceeds the {maximum_bytes}-byte limit")
    payload = path.read_bytes()
    if len(payload) > maximum_bytes:
        raise ValueError(f"catalog file exceeds the {maximum_bytes}-byte limit")
    return payload


def load_yaml(root: Path, relative: str) -> dict[str, Any]:
    path = resolve_local_file(root, relative, suffixes={".yaml", ".yml"})
    payload = read_bounded(path, maximum_bytes=MAX_YAML_BYTES)
    value = yaml.load(payload.decode("utf-8"), Loader=UniqueKeyLoader)
    if not isinstance(value, dict):
        raise ValueError("YAML document root must be an object")
    return value


def load_json(root: Path, relative: str) -> dict[str, Any]:
    path = resolve_local_file(root, relative, suffixes={".json"})
    payload = read_bounded(path, maximum_bytes=MAX_JSON_BYTES)
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON document root must be an object")
    return value


def load_prompt(root: Path, relative: str) -> str:
    path = resolve_local_file(root, relative, suffixes={".md", ".txt"})
    text = read_bounded(path, maximum_bytes=MAX_PROMPT_BYTES).decode("utf-8").strip()
    if not text:
        raise ValueError("system prompt cannot be empty")
    return text


def record_list(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    if set(document) != {key}:
        raise ValueError(f"record document must contain only the {key!r} key")
    records = document[key]
    if not isinstance(records, list):
        raise ValueError(f"{key!r} must be a list")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"every {key!r} item must be an object")
    return records
