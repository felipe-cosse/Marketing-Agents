#!/usr/bin/env python3
"""Verify ARCH-08 repository boundaries using only the Python standard library."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True, order=True)
class BoundaryViolation:
    """A stable, source-addressable architecture policy failure."""

    path: str
    line: int
    code: str
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.code}: {self.detail}"


class BoundaryPolicyError(ValueError):
    """Raised when the checked-in policy cannot be interpreted safely."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BoundaryPolicyError(f"{label} must be an object")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BoundaryPolicyError(f"{label} must be an array of strings")
    return tuple(value)


def _approved_external_roots(
    value: object, expected_layers: frozenset[str]
) -> dict[str, frozenset[str]]:
    raw_roots = _mapping(value, "python.approved_external_roots")
    if not all(isinstance(layer, str) for layer in raw_roots):
        raise BoundaryPolicyError("python.approved_external_roots keys must be strings")
    actual_layers = frozenset(raw_roots)
    if actual_layers != expected_layers:
        missing = sorted(expected_layers - actual_layers)
        unexpected = sorted(actual_layers - expected_layers)
        raise BoundaryPolicyError(
            "python.approved_external_roots must cover every Python layer "
            f"(missing={missing}, unexpected={unexpected})"
        )
    result: dict[str, frozenset[str]] = {}
    for layer, raw_allowed in raw_roots.items():
        allowed = _strings(raw_allowed, f"python.approved_external_roots.{layer}")
        if len(set(allowed)) != len(allowed) or any(not root.isidentifier() for root in allowed):
            raise BoundaryPolicyError(
                f"python.approved_external_roots.{layer} must contain unique module roots"
            )
        result[layer] = frozenset(allowed)
    return result


def _repo_relative_path(value: object, label: str) -> PurePosixPath:
    """Return one canonical repository-relative path or reject the policy."""

    if not isinstance(value, str) or not value or value.strip() != value:
        raise BoundaryPolicyError(f"{label} must be a non-empty path string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path == PurePosixPath(".")
        or ".." in path.parts
        or path.as_posix() != value
        or any(character in value for character in "*?[]")
    ):
        raise BoundaryPolicyError(f"{label} must be a canonical repository-relative path")
    return path


def _repo_relative_pattern(value: object, label: str) -> str:
    """Validate a repository-relative glob while retaining its wildcard syntax."""

    if not isinstance(value, str) or not value or value.strip() != value:
        raise BoundaryPolicyError(f"{label} must be a non-empty path pattern")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise BoundaryPolicyError(f"{label} must stay inside the repository")
    return value


def _contained_path(root: Path, relative: PurePosixPath, label: str) -> Path:
    """Resolve a policy path without allowing a symlink to escape the repository."""

    root = root.resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise BoundaryPolicyError(f"{label} resolves outside the repository")
    return candidate


def load_policy(path: Path) -> Mapping[str, Any]:
    """Load and validate an ARCH-08 JSON policy."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BoundaryPolicyError(f"cannot load {path}: {error}") from error
    policy = _mapping(value, "policy")
    if policy.get("schema_version") != 1:
        raise BoundaryPolicyError("schema_version must be 1")
    for index, value in enumerate(
        _strings(policy.get("required_layer_homes"), "required_layer_homes")
    ):
        _repo_relative_path(value, f"required_layer_homes[{index}]")
    for index, value in enumerate(
        _strings(policy.get("forbidden_python_roots"), "forbidden_python_roots")
    ):
        _repo_relative_path(value, f"forbidden_python_roots[{index}]")

    python = _mapping(policy.get("python"), "python")
    _repo_relative_path(python.get("source_root"), "python.source_root")
    raw_layers = _mapping(python.get("allowed_internal_layers"), "allowed_internal_layers")
    if not all(isinstance(layer, str) for layer in raw_layers):
        raise BoundaryPolicyError("python.allowed_internal_layers keys must be strings")
    for layer, allowed in raw_layers.items():
        _strings(allowed, f"python.allowed_internal_layers.{layer}")
    _approved_external_roots(python.get("approved_external_roots"), frozenset(raw_layers))
    for index, pattern in enumerate(
        _strings(python.get("pure_contract_globs"), "python.pure_contract_globs")
    ):
        _repo_relative_pattern(pattern, f"python.pure_contract_globs[{index}]")
    exceptions = python.get("import_exceptions")
    if not isinstance(exceptions, list):
        raise BoundaryPolicyError("python.import_exceptions must be an array")
    for index, raw_exception in enumerate(exceptions):
        exception = _mapping(raw_exception, f"python.import_exceptions[{index}]")
        _repo_relative_path(exception.get("source"), f"python.import_exceptions[{index}].source")

    frontend = _mapping(policy.get("frontend"), "frontend")
    for key in ("source_root", "api_root", "contract_root", "entrypoint"):
        _repo_relative_path(frontend.get(key), f"frontend.{key}")
    for key in ("test_roots", "api_allowed_import_roots"):
        for index, value in enumerate(_strings(frontend.get(key), f"frontend.{key}")):
            _repo_relative_path(value, f"frontend.{key}[{index}]")
    return policy


def _prefix_matches(imported: str, prefix: str) -> bool:
    return imported == prefix or imported.startswith(f"{prefix}.")


def _portable(path: Path) -> str:
    return path.as_posix()


def _relative_module(source_root: Path, path: Path, package: str) -> tuple[str, str]:
    relative = path.relative_to(source_root)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    module = ".".join((package, *parts)) if parts else package
    package_name = module if path.name == "__init__.py" else module.rpartition(".")[0]
    return module, package_name


def _python_imports(
    source_root: Path,
    path: Path,
    package: str,
    internal_roots: frozenset[str],
) -> tuple[list[tuple[str, int]], list[BoundaryViolation]]:
    relative = _portable(path.relative_to(source_root))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        line = getattr(error, "lineno", 1) or 1
        return [], [BoundaryViolation(relative, line, "python-parse-error", str(error))]

    _module, package_name = _relative_module(source_root, path, package)
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported = node.module or ""
            if node.level:
                try:
                    imported = importlib.util.resolve_name(
                        f"{'.' * node.level}{imported}", package_name
                    )
                except (ImportError, ValueError):
                    imported = ""
            if imported:
                imports.append((imported, node.lineno))
                if imported == package:
                    imports.extend(
                        (f"{package}.{alias.name}", node.lineno)
                        for alias in node.names
                        if alias.name in internal_roots
                    )
    return imports, []


def _source_layer(relative: Path) -> str:
    return relative.parts[0] if len(relative.parts) > 1 else "<root>"


def _exception_allows(relative: str, imported: str, exceptions: Sequence[object]) -> bool:
    for raw_exception in exceptions:
        exception = _mapping(raw_exception, "python.import_exceptions item")
        if exception.get("source") != relative:
            continue
        prefixes = _strings(
            exception.get("allowed_prefixes"),
            "python.import_exceptions[].allowed_prefixes",
        )
        if any(_prefix_matches(imported, prefix) for prefix in prefixes):
            return True
    return False


def _contract_files(source_root: Path, globs: Iterable[str]) -> set[Path]:
    return {
        path.resolve() for pattern in globs for path in source_root.glob(pattern) if path.is_file()
    }


def _check_python(root: Path, policy: Mapping[str, Any]) -> list[BoundaryViolation]:
    config = _mapping(policy["python"], "python")
    source_relative = _repo_relative_path(config["source_root"], "python.source_root")
    source_root = _contained_path(root, source_relative, "python.source_root")
    if not source_root.is_dir():
        return [
            BoundaryViolation(
                source_relative.as_posix(),
                1,
                "missing-python-source-root",
                "configured Python source root is absent",
            )
        ]
    package = str(config["package"])
    raw_layers = _mapping(config["allowed_internal_layers"], "allowed_internal_layers")
    layers = {
        str(layer): _strings(allowed, f"allowed_internal_layers.{layer}")
        for layer, allowed in raw_layers.items()
    }
    approved_external = _approved_external_roots(
        config.get("approved_external_roots"), frozenset(layers)
    )
    internal_roots = frozenset(
        root_name
        for layer, allowed in layers.items()
        for root_name in (layer, *allowed)
        if root_name != "<root>"
    )
    exceptions = config.get("import_exceptions", [])
    if not isinstance(exceptions, list):
        raise BoundaryPolicyError("python.import_exceptions must be an array")
    api_orm = _strings(config.get("api_orm_forbidden_prefixes"), "api_orm_forbidden_prefixes")
    forbidden_tests = _strings(
        config.get("production_test_forbidden_prefixes"),
        "production_test_forbidden_prefixes",
    )
    contract_globs = tuple(
        _repo_relative_pattern(pattern, f"pure_contract_globs[{index}]")
        for index, pattern in enumerate(
            _strings(config.get("pure_contract_globs"), "pure_contract_globs")
        )
    )
    contract_forbidden = _strings(
        config.get("pure_contract_forbidden_prefixes"),
        "pure_contract_forbidden_prefixes",
    )
    contract_files = _contract_files(source_root, contract_globs)
    violations: list[BoundaryViolation] = []

    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative_path = path.relative_to(source_root)
        relative = _portable(relative_path)
        layer = _source_layer(relative_path)
        allowed_layers = layers.get(layer)
        if allowed_layers is None:
            violations.append(BoundaryViolation(relative, 1, "unknown-python-layer", layer))
            continue
        imports, parse_failures = _python_imports(source_root, path, package, internal_roots)
        for failure in parse_failures:
            violations.append(
                BoundaryViolation(
                    f"{config['source_root']}/{failure.path}",
                    failure.line,
                    failure.code,
                    failure.detail,
                )
            )
        for imported, line in imports:
            repo_path = f"{config['source_root']}/{relative}"
            top_level = imported.split(".", 1)[0]
            if (
                top_level != package
                and top_level not in sys.stdlib_module_names
                and top_level not in approved_external[layer]
            ):
                violations.append(
                    BoundaryViolation(
                        repo_path,
                        line,
                        "python-unapproved-external-import",
                        f"{layer} imports unapproved third-party package {imported}",
                    )
                )
            if any(_prefix_matches(imported, prefix) for prefix in forbidden_tests):
                violations.append(
                    BoundaryViolation(
                        repo_path,
                        line,
                        "python-production-test-import",
                        f"production module imports {imported}",
                    )
                )
            if layer == "api" and any(_prefix_matches(imported, prefix) for prefix in api_orm):
                violations.append(
                    BoundaryViolation(
                        repo_path,
                        line,
                        "api-orm-import",
                        f"API layer imports ORM implementation {imported}",
                    )
                )
            if path.resolve() in contract_files and any(
                _prefix_matches(imported, prefix) for prefix in contract_forbidden
            ):
                violations.append(
                    BoundaryViolation(
                        repo_path,
                        line,
                        "impure-python-contract",
                        f"contract imports implementation {imported}",
                    )
                )
            if imported == package or not imported.startswith(f"{package}."):
                continue
            target_layer = imported[len(package) + 1 :].split(".", 1)[0]
            if target_layer in allowed_layers or _exception_allows(relative, imported, exceptions):
                continue
            violations.append(
                BoundaryViolation(
                    repo_path,
                    line,
                    "python-import-direction",
                    f"{layer} must not import {imported}",
                )
            )
    return violations


def _line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


@dataclass(frozen=True)
class _TypeScriptToken:
    kind: str
    value: str
    offset: int


_SIMPLE_TYPESCRIPT_ESCAPES = {
    "0": "\0",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


def _scan_typescript_escape(source: str, start: int) -> tuple[str, int]:
    index = start + 1
    if index >= len(source):
        return "", index
    escaped = source[index]
    if escaped == "\r" or escaped == "\n":
        if escaped == "\r" and index + 1 < len(source) and source[index + 1] == "\n":
            index += 1
        return "", index + 1
    if escaped == "x" and index + 2 < len(source):
        digits = source[index + 1 : index + 3]
        if all(character in "0123456789abcdefABCDEF" for character in digits):
            return chr(int(digits, 16)), index + 3
    if escaped == "u":
        if index + 1 < len(source) and source[index + 1] == "{":
            end = source.find("}", index + 2)
            digits = source[index + 2 : end] if end >= 0 else ""
            if digits and all(character in "0123456789abcdefABCDEF" for character in digits):
                codepoint = int(digits, 16)
                if codepoint <= 0x10FFFF:
                    return chr(codepoint), end + 1
        elif index + 4 < len(source):
            digits = source[index + 1 : index + 5]
            if all(character in "0123456789abcdefABCDEF" for character in digits):
                return chr(int(digits, 16)), index + 5
    return _SIMPLE_TYPESCRIPT_ESCAPES.get(escaped, escaped), index + 1


def _scan_typescript_string(source: str, start: int) -> tuple[str, int]:
    quote = source[start]
    index = start + 1
    value: list[str] = []
    while index < len(source):
        character = source[index]
        if character == quote:
            return "".join(value), index + 1
        if character == "\\":
            decoded, index = _scan_typescript_escape(source, index)
            value.append(decoded)
            continue
        value.append(character)
        index += 1
    return "".join(value), index


def _is_typescript_identifier_start(character: str) -> bool:
    return character in "_$" or character.isalpha()


def _is_typescript_identifier_part(character: str) -> bool:
    return _is_typescript_identifier_start(character) or character.isdigit()


def _typescript_tokens(source: str) -> tuple[_TypeScriptToken, ...]:
    """Lex boundary-relevant TypeScript tokens while excluding inert source text."""

    tokens: list[_TypeScriptToken] = []

    def scan_template(start: int) -> int:
        index = start + 1
        segment_start = index
        segment: list[str] = []

        def emit_segment() -> None:
            if segment:
                tokens.append(_TypeScriptToken("template", "".join(segment), segment_start))

        while index < len(source):
            character = source[index]
            if character == "\\":
                decoded, index = _scan_typescript_escape(source, index)
                segment.append(decoded)
                continue
            if character == "`":
                emit_segment()
                return index + 1
            if character == "$" and index + 1 < len(source) and source[index + 1] == "{":
                emit_segment()
                tokens.append(_TypeScriptToken("punctuation", "{", index + 1))
                index, closing_offset = scan_code(index + 2, stop_at_closing_brace=True)
                if closing_offset is not None:
                    tokens.append(_TypeScriptToken("punctuation", "}", closing_offset))
                segment_start = index
                segment = []
                continue
            segment.append(character)
            index += 1
        emit_segment()
        return index

    def scan_code(start: int, *, stop_at_closing_brace: bool = False) -> tuple[int, int | None]:
        index = start
        nested_braces = 0
        while index < len(source):
            character = source[index]
            if character.isspace():
                index += 1
                continue
            if source.startswith("//", index):
                newline = source.find("\n", index + 2)
                index = len(source) if newline < 0 else newline + 1
                continue
            if source.startswith("/*", index):
                end = source.find("*/", index + 2)
                index = len(source) if end < 0 else end + 2
                continue
            if character in "\"'":
                string_start = index
                value, index = _scan_typescript_string(source, index)
                tokens.append(_TypeScriptToken("string", value, string_start))
                continue
            if character == "`":
                index = scan_template(index)
                continue
            if _is_typescript_identifier_start(character):
                end = index + 1
                while end < len(source) and _is_typescript_identifier_part(source[end]):
                    end += 1
                tokens.append(_TypeScriptToken("identifier", source[index:end], index))
                index = end
                continue
            if character == "}" and stop_at_closing_brace and nested_braces == 0:
                return index + 1, index
            tokens.append(_TypeScriptToken("punctuation", character, index))
            if character == "{":
                nested_braces += 1
            elif character == "}" and nested_braces:
                nested_braces -= 1
            index += 1
        return index, None

    scan_code(0)
    return tuple(tokens)


def _static_import_specifier(
    tokens: Sequence[_TypeScriptToken], start: int
) -> _TypeScriptToken | None:
    index = start + 1
    if index >= len(tokens):
        return None
    first = tokens[index]
    if first.kind == "string":
        return first
    if first.value in {"(", "."}:
        return None
    while index < len(tokens) and tokens[index].value != ";":
        token = tokens[index]
        if (
            token.kind == "identifier"
            and token.value == "from"
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "string"
        ):
            return tokens[index + 1]
        if (
            token.kind == "identifier"
            and token.value == "require"
            and index + 2 < len(tokens)
            and tokens[index + 1].value == "("
            and tokens[index + 2].kind == "string"
        ):
            return tokens[index + 2]
        index += 1
    return None


def _static_export_specifier(
    tokens: Sequence[_TypeScriptToken], start: int
) -> _TypeScriptToken | None:
    index = start + 1
    if index < len(tokens) and tokens[index].kind == "identifier" and tokens[index].value == "type":
        index += 1
    if index >= len(tokens):
        return None
    if tokens[index].value == "*":
        index += 1
        if (
            index < len(tokens)
            and tokens[index].kind == "identifier"
            and tokens[index].value == "as"
        ):
            index += 2
    elif tokens[index].value == "{":
        depth = 1
        index += 1
        while index < len(tokens) and depth:
            if tokens[index].value == "{":
                depth += 1
            elif tokens[index].value == "}":
                depth -= 1
            index += 1
    else:
        return None
    if (
        index + 1 < len(tokens)
        and tokens[index].kind == "identifier"
        and tokens[index].value == "from"
        and tokens[index + 1].kind == "string"
    ):
        return tokens[index + 1]
    return None


def _typescript_imports(source: str) -> list[tuple[str, int]]:
    imports: list[tuple[str, int]] = []
    tokens = _typescript_tokens(source)
    brace_depth = 0
    for index, token in enumerate(tokens):
        if token.value == "{":
            brace_depth += 1
        elif token.value == "}":
            brace_depth = max(brace_depth - 1, 0)
        if token.kind != "identifier":
            continue
        specifier: _TypeScriptToken | None = None
        if token.value == "import":
            if (
                index + 2 < len(tokens)
                and (index == 0 or tokens[index - 1].value != ".")
                and tokens[index + 1].value == "("
                and tokens[index + 2].kind == "string"
            ):
                specifier = tokens[index + 2]
            elif brace_depth == 0:
                specifier = _static_import_specifier(tokens, index)
        elif token.value == "export" and brace_depth == 0:
            specifier = _static_export_specifier(tokens, index)
        if specifier is not None:
            imports.append((specifier.value, _line_number(source, token.offset)))
    return imports


_GLOBAL_FETCH_OWNERS = frozenset({"globalThis", "self", "window"})


def _call_open_token(tokens: Sequence[_TypeScriptToken], after_callee: int) -> int | None:
    if after_callee < len(tokens) and tokens[after_callee].value == "(":
        return after_callee
    if (
        after_callee + 2 < len(tokens)
        and tokens[after_callee].value == "?"
        and tokens[after_callee + 1].value == "."
        and tokens[after_callee + 2].value == "("
    ):
        return after_callee + 2
    return None


def _call_is_invocation(tokens: Sequence[_TypeScriptToken], open_index: int) -> bool:
    depth = 0
    for index in range(open_index, len(tokens)):
        if tokens[index].value == "(":
            depth += 1
        elif tokens[index].value == ")":
            depth -= 1
            if depth == 0:
                following = tokens[index + 1].value if index + 1 < len(tokens) else None
                return following not in {"{", ":"}
    return False


def _global_fetch_call(tokens: Sequence[_TypeScriptToken], start: int) -> tuple[int, int] | None:
    index = start + 1
    if index + 1 < len(tokens) and tokens[index].value == "?" and tokens[index + 1].value == ".":
        index += 2
    elif index < len(tokens) and tokens[index].value == ".":
        index += 1
    if (
        index < len(tokens)
        and tokens[index].kind == "identifier"
        and tokens[index].value == "fetch"
    ):
        open_index = _call_open_token(tokens, index + 1)
        return (start, open_index) if open_index is not None else None
    if (
        index + 2 < len(tokens)
        and tokens[index].value == "["
        and tokens[index + 1].kind in {"string", "template"}
        and tokens[index + 1].value == "fetch"
        and tokens[index + 2].value == "]"
    ):
        open_index = _call_open_token(tokens, index + 3)
        return (start, open_index) if open_index is not None else None
    return None


def _is_api_path_literal(value: str) -> bool:
    return value == "/api" or value.startswith("/api/")


def _typescript_ui_network_ownership(source: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return source offsets for direct fetch calls and UI-owned API literals."""

    tokens = _typescript_tokens(source)
    fetch_offsets: set[int] = set()
    api_literal_offsets = {
        token.offset
        for token in tokens
        if token.kind in {"string", "template"} and _is_api_path_literal(token.value)
    }
    for index, token in enumerate(tokens):
        if token.kind != "identifier":
            continue
        if token.value in _GLOBAL_FETCH_OWNERS:
            global_call = _global_fetch_call(tokens, index)
            if global_call is not None:
                owner_index, open_index = global_call
                if _call_is_invocation(tokens, open_index):
                    fetch_offsets.add(tokens[owner_index].offset)
            continue
        if token.value != "fetch" or (index and tokens[index - 1].value == "."):
            continue
        if (
            index
            and tokens[index - 1].kind == "identifier"
            and tokens[index - 1].value == "function"
        ):
            continue
        open_index = _call_open_token(tokens, index + 1)
        if open_index is not None and _call_is_invocation(tokens, open_index):
            fetch_offsets.add(token.offset)
    return tuple(sorted(fetch_offsets)), tuple(sorted(api_literal_offsets))


def _logical_import(importer: Path, specifier: str) -> Path | None:
    if not specifier.startswith("."):
        return None
    joined = PurePosixPath(importer.parent.as_posix()) / specifier
    parts: list[str] = []
    for part in joined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return Path(*parts)


def _path_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _has_marker(path: Path, markers: Sequence[str]) -> bool:
    portable = path.as_posix()
    return any(marker in portable for marker in markers)


def _is_test_path(path: Path, roots: Sequence[Path], markers: Sequence[str]) -> bool:
    return _has_marker(path, markers) or any(_path_under(path, root) for root in roots)


def _resolve_typescript_file(root: Path, logical: Path) -> Path | None:
    candidates = [
        logical,
        logical.with_suffix(".ts"),
        logical.with_suffix(".tsx"),
        logical / "index.ts",
        logical / "index.tsx",
    ]
    for candidate in candidates:
        if (root / candidate).is_file():
            return candidate
    return None


def _main_dependency_closure(
    root: Path, entrypoint: Path, extensions: tuple[str, ...]
) -> tuple[set[Path], list[BoundaryViolation]]:
    pending = [entrypoint]
    visited: set[Path] = set()
    violations: list[BoundaryViolation] = []
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        absolute = root / current
        if absolute.suffix not in extensions:
            continue
        try:
            source = absolute.read_text(encoding="utf-8")
        except OSError as error:
            violations.append(
                BoundaryViolation(current.as_posix(), 1, "frontend-read-error", str(error))
            )
            continue
        for specifier, _line in _typescript_imports(source):
            logical = _logical_import(current, specifier)
            if logical is None:
                continue
            resolved = _resolve_typescript_file(root, logical)
            if resolved is not None:
                pending.append(resolved)
    return visited, violations


def _check_frontend(root: Path, policy: Mapping[str, Any]) -> list[BoundaryViolation]:
    config = _mapping(policy["frontend"], "frontend")
    source_root = Path(*_repo_relative_path(config["source_root"], "frontend.source_root").parts)
    api_root = Path(*_repo_relative_path(config["api_root"], "frontend.api_root").parts)
    contract_root = Path(
        *_repo_relative_path(config["contract_root"], "frontend.contract_root").parts
    )
    entrypoint = Path(*_repo_relative_path(config["entrypoint"], "frontend.entrypoint").parts)
    test_roots = tuple(
        Path(*_repo_relative_path(value, f"frontend.test_roots[{index}]").parts)
        for index, value in enumerate(_strings(config["test_roots"], "test_roots"))
    )
    markers = _strings(config["test_name_markers"], "test_name_markers")
    api_allowed = tuple(
        Path(*_repo_relative_path(value, f"frontend.api_allowed_import_roots[{index}]").parts)
        for index, value in enumerate(
            _strings(config["api_allowed_import_roots"], "api_allowed_import_roots")
        )
    )
    extensions = (".ts", ".tsx")
    absolute_source_root = _contained_path(
        root, PurePosixPath(source_root.as_posix()), "frontend.source_root"
    )
    absolute_entrypoint = _contained_path(
        root, PurePosixPath(entrypoint.as_posix()), "frontend.entrypoint"
    )
    violations: list[BoundaryViolation] = []
    if not absolute_source_root.is_dir():
        violations.append(
            BoundaryViolation(
                source_root.as_posix(),
                1,
                "missing-frontend-source-root",
                "configured frontend source root is absent",
            )
        )
    if not absolute_entrypoint.is_file():
        violations.append(
            BoundaryViolation(
                entrypoint.as_posix(),
                1,
                "missing-frontend-entrypoint",
                "configured frontend entrypoint is absent",
            )
        )
    else:
        closure, closure_violations = _main_dependency_closure(root, entrypoint, extensions)
        violations.extend(closure_violations)
        for dependency in sorted(closure):
            if dependency != entrypoint and _is_test_path(dependency, test_roots, markers):
                violations.append(
                    BoundaryViolation(
                        dependency.as_posix(),
                        1,
                        "frontend-main-test-dependency",
                        f"{entrypoint.as_posix()} reaches test/config/e2e code",
                    )
                )

    if not absolute_source_root.is_dir():
        return violations
    sources = sorted(
        path
        for path in absolute_source_root.rglob("*")
        if path.is_file() and path.suffix in extensions
    )
    for absolute in sources:
        relative = absolute.relative_to(root)
        if _is_test_path(relative, test_roots, markers):
            continue
        source = absolute.read_text(encoding="utf-8")
        is_api = _path_under(relative, api_root)
        is_contract = _path_under(relative, contract_root)
        for specifier, line in _typescript_imports(source):
            target = _logical_import(relative, specifier)
            resolved_target = _resolve_typescript_file(root, target) if target is not None else None
            test_target = resolved_target or target
            if test_target is not None and _is_test_path(test_target, test_roots, markers):
                violations.append(
                    BoundaryViolation(
                        relative.as_posix(),
                        line,
                        "frontend-production-test-import",
                        f"production source imports {specifier}",
                    )
                )
            if (
                is_api
                and target is not None
                and not any(_path_under(target, allowed) for allowed in api_allowed)
            ):
                violations.append(
                    BoundaryViolation(
                        relative.as_posix(),
                        line,
                        "frontend-api-outward-import",
                        f"API adapter imports {specifier}",
                    )
                )
            if is_contract and (target is None or not _path_under(target, contract_root)):
                violations.append(
                    BoundaryViolation(
                        relative.as_posix(),
                        line,
                        "impure-frontend-contract",
                        f"contract imports runtime dependency {specifier}",
                    )
                )
        if not is_api:
            fetch_offsets, api_literal_offsets = _typescript_ui_network_ownership(source)
            for offset in fetch_offsets:
                violations.append(
                    BoundaryViolation(
                        relative.as_posix(),
                        _line_number(source, offset),
                        "frontend-direct-fetch",
                        "network calls belong under apps/web/src/api",
                    )
                )
            for offset in api_literal_offsets:
                violations.append(
                    BoundaryViolation(
                        relative.as_posix(),
                        _line_number(source, offset),
                        "frontend-api-literal",
                        "API paths belong under apps/web/src/api",
                    )
                )
    return violations


def check_repository(root: Path, policy: Mapping[str, Any]) -> tuple[BoundaryViolation, ...]:
    """Return all policy violations for ``root`` without mutating the repository."""

    root = root.resolve()
    violations: list[BoundaryViolation] = []
    for index, required in enumerate(
        _strings(policy["required_layer_homes"], "required_layer_homes")
    ):
        relative = _repo_relative_path(required, f"required_layer_homes[{index}]")
        if not _contained_path(root, relative, f"required_layer_homes[{index}]").is_dir():
            violations.append(
                BoundaryViolation(required, 1, "missing-layer-home", "required path is absent")
            )
    for index, forbidden in enumerate(
        _strings(policy["forbidden_python_roots"], "forbidden_python_roots")
    ):
        relative = _repo_relative_path(forbidden, f"forbidden_python_roots[{index}]")
        path = _contained_path(root, relative, f"forbidden_python_roots[{index}]")
        if path.is_file() or (path.is_dir() and any(path.rglob("*.py"))):
            violations.append(
                BoundaryViolation(
                    forbidden,
                    1,
                    "legacy-python-layer",
                    "legacy Python source home must remain absent",
                )
            )
    violations.extend(_check_python(root, policy))
    violations.extend(_check_frontend(root, policy))
    return tuple(sorted(set(violations)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path, default=Path("architecture-boundaries.json"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    policy_path = args.policy if args.policy.is_absolute() else root / args.policy
    try:
        policy = load_policy(policy_path)
        violations = check_repository(root, policy)
    except BoundaryPolicyError as error:
        print(f"architecture boundary policy error: {error}")
        return 2
    if violations:
        print("ARCH-08 architecture boundary verification failed:")
        for violation in violations:
            print(violation.render())
        return 1
    print("ARCH-08 architecture boundary verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
