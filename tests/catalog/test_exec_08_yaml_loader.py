"""EXEC-08: safe parser compatibility without cached catalog results or weaker checks."""

from __future__ import annotations

import builtins
import datetime
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from marketing_agents.infrastructure.catalog import compile_catalog, loader

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fallback_module():
    """Exercise the actual import fallback in a private, unregistered module."""
    original_import = builtins.__import__
    attempts = []

    def without_c_loader(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yaml" and "CSafeLoader" in (fromlist or ()):
            attempts.append(name)
            raise ImportError("owned test: LibYAML unavailable")
        return original_import(name, globals, locals, fromlist, level)

    spec = importlib.util.spec_from_file_location("_exec08_fallback_loader", loader.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch("builtins.__import__", side_effect=without_c_loader):
        spec.loader.exec_module(module)
    assert attempts == ["yaml"]
    assert module.UniqueKeyLoader.__bases__ == (yaml.SafeLoader,)
    assert builtins.__import__ is original_import
    return module


@pytest.fixture(params=("selected", "fallback"))
def implementation(request, fallback_module):
    return loader if request.param == "selected" else fallback_module


def test_exec_08_selected_safe_base_and_constructor_registration(fallback_module):
    expected = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert loader.UniqueKeyLoader.__bases__ == (expected,)
    for module in (loader, fallback_module):
        for selected in (module.UniqueKeyLoader, module._PythonUniqueKeyLoader):
            tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG
            assert selected.yaml_constructors[tag] is module._construct_unique_mapping
            assert selected.yaml_constructors is not selected.__bases__[0].yaml_constructors
    # Registering the owned subclass constructor must not change PyYAML globals.
    for base in {expected, yaml.SafeLoader}:
        assert base.yaml_constructors[yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG].__name__ == (
            "construct_yaml_map"
        )
    assert loader.MAX_YAML_BYTES == fallback_module.MAX_YAML_BYTES == 1_000_000
    assert loader.MAX_JSON_BYTES == fallback_module.MAX_JSON_BYTES == 1_000_000
    assert loader.MAX_PROMPT_BYTES == fallback_module.MAX_PROMPT_BYTES == 100_000


def test_exec_08_full_catalog_content_and_hash_match_fallback(fallback_module, monkeypatch):
    selected_class = loader.UniqueKeyLoader
    original_load = yaml.load
    parsers = []

    def observed(stream, Loader):
        parsers.append(Loader)
        return original_load(stream, Loader=Loader)

    with monkeypatch.context() as context:
        context.setattr(yaml, "load", observed)
        selected = compile_catalog(ROOT / "catalog/v1")
    # Observe the actual dispatch for every checked-in YAML document, rather
    # than merely proving that the accelerated class exists in the module.
    assert len(parsers) == len(list((ROOT / "catalog/v1").rglob("*.yaml")))
    assert parsers and all(parser is selected_class for parser in parsers)
    with monkeypatch.context() as context:
        context.setattr(loader, "UniqueKeyLoader", fallback_module.UniqueKeyLoader)
        fallback = compile_catalog(ROOT / "catalog/v1")
    assert loader.UniqueKeyLoader is selected_class
    assert selected == fallback
    lock = json.loads((ROOT / "catalog/v1/release.lock.json").read_text(encoding="utf-8"))
    assert selected.content_hash == fallback.content_hash == lock["content_hash"]
    assert (len(selected.departments), len(selected.functions)) == (5, 12)
    assert (len(selected.templates), len(selected.instances)) == (36, 43)


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("value: ordinary\n", {"value": "ordinary"}),
        ("value: 'tab\tinside'\n", {"value": "tab\tinside"}),
        ("value: '100%'\n", {"value": "100%"}),
        ("value: 'why?'\n", {"value": "why?"}),
        ('value: "\\u65e5\\U0001f600"\n', {"value": "日😀"}),
        ('value: "\\uD800"\n', {"value": "\ud800"}),
        ("%YAML 1.0\n---\nvalue: ordinary\n", {"value": "ordinary"}),
        ("%YAML 1.3\n---\nvalue: ordinary\n", {"value": "ordinary"}),
    ),
)
def test_exec_08_compatibility_dispatch_preserves_valid_inputs(
    implementation, tmp_path, monkeypatch, source, expected
):
    path = tmp_path / "record.yaml"
    path.write_text(source, encoding="utf-8")
    original_load = yaml.load
    selected = []

    def observed(stream, Loader):
        selected.append(Loader)
        return original_load(stream, Loader=Loader)

    with monkeypatch.context() as context:
        context.setattr(yaml, "load", observed)
        assert implementation.load_yaml(tmp_path, "record.yaml") == expected
    wanted = (
        implementation.UniqueKeyLoader
        if source == "value: ordinary\n"
        else implementation._PythonUniqueKeyLoader
    )
    assert selected == [wanted]


@pytest.mark.parametrize(
    "source",
    (
        "value:\t1\n",
        "value: [one,\ttwo]\n",
        "value: !!str\t1\n",
        "value: !<tag:yaml.org,2002:str%00> 1\n",
        "value: [foo?:bar]\n",
    ),
)
def test_exec_08_c_parser_acceptance_differences_still_fail(implementation, tmp_path, source):
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        implementation.load_yaml(tmp_path, "record.yaml")


@pytest.mark.parametrize("marker", ("[", "{", "- ", "-\r", "-\n", "-\x85", "-\u2028", "-\u2029"))
@pytest.mark.parametrize("count", (128, 129))
def test_exec_08_collection_marker_boundary_selects_parser_without_rejecting(
    implementation, tmp_path, monkeypatch, marker, count
):
    # Markers inside a quoted scalar are deliberately counted too. Both parsers
    # accept this shallow input; crossing 128 is selection, not invalidation.
    source = "value: '" + marker * count + "'\n"
    path = tmp_path / "record.yaml"
    path.write_text(source, encoding="utf-8")
    original_load = yaml.load
    expected = original_load(source, Loader=implementation._PythonUniqueKeyLoader)
    selected = []

    def observed(stream, Loader):
        selected.append(Loader)
        return original_load(stream, Loader=Loader)

    with monkeypatch.context() as context:
        context.setattr(yaml, "load", observed)
        assert implementation.load_yaml(tmp_path, "record.yaml") == expected
    wanted = (
        implementation.UniqueKeyLoader if count == 128 else implementation._PythonUniqueKeyLoader
    )
    assert selected == [wanted]


@pytest.mark.parametrize(
    "source",
    (
        "value: " + "[" * 500 + "0" + "]" * 500 + "\n",
        "value:\n" + "".join("  " * depth + "-\n" for depth in range(1, 501)),
        "value:\n  " + "- " * 500 + "0\n",
    ),
)
def test_exec_08_deep_sequences_retain_prior_recursion_failure(implementation, tmp_path, source):
    # Never change the interpreter's recursion limit or install a new YAML
    # limit. These bounded inputs must still use the prior parser's behavior.
    assert len(source.encode("utf-8")) < implementation.MAX_YAML_BYTES
    with pytest.raises(RecursionError):
        yaml.load(source, Loader=implementation._PythonUniqueKeyLoader)
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(RecursionError):
        implementation.load_yaml(tmp_path, "record.yaml")


@pytest.mark.parametrize("indent", (127, 128))
@pytest.mark.parametrize("separator", (None, "\r", "\n", "\r\n", "\x85", "\u2028", "\u2029"))
def test_exec_08_indentation_boundary_selects_parser_without_rejecting(
    implementation, tmp_path, monkeypatch, indent, separator
):
    prefix = "" if separator is None else "outer:" + separator
    source = prefix + " " * indent + "value: ordinary\n"
    path = tmp_path / "record.yaml"
    path.write_text(source, encoding="utf-8")
    original_load = yaml.load
    expected = original_load(source, Loader=implementation._PythonUniqueKeyLoader)
    selected = []

    def observed(stream, Loader):
        selected.append(Loader)
        return original_load(stream, Loader=Loader)

    with monkeypatch.context() as context:
        context.setattr(yaml, "load", observed)
        assert implementation.load_yaml(tmp_path, "record.yaml") == expected
    wanted = (
        implementation.UniqueKeyLoader if indent == 127 else implementation._PythonUniqueKeyLoader
    )
    assert selected == [wanted]


def test_exec_08_deep_block_mapping_retains_prior_recursion_failure(implementation, tmp_path):
    depth = 512
    source = "\n".join("  " * index + "a:" for index in range(depth)) + "  " * depth + " 0\n"
    assert len(source.encode("utf-8")) < implementation.MAX_YAML_BYTES
    with pytest.raises(RecursionError):
        yaml.load(source, Loader=implementation._PythonUniqueKeyLoader)
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(RecursionError):
        implementation.load_yaml(tmp_path, "record.yaml")


def test_exec_08_valid_scalar_collection_unicode_and_alias_semantics(implementation, tmp_path):
    source = """text: 'équipe 日本語 😀'
integer: 42
floating: 1.25
boolean: true
legacy_boolean: yes
null_value: null
date: 2026-10-15
binary: !!binary SGVsbG8=
quoted_number: '042'
block: |
  first line
  second line
items: [one, 2, false]
base: &base {name: shared, count: 3}
alias: *base
empty_mapping: {}
empty_sequence: []
"""
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    actual = implementation.load_yaml(tmp_path, "record.yaml")
    assert actual == {
        "text": "équipe 日本語 😀",
        "integer": 42,
        "floating": 1.25,
        "boolean": True,
        "legacy_boolean": True,
        "null_value": None,
        "date": datetime.date(2026, 10, 15),
        "binary": b"Hello",
        "quoted_number": "042",
        "block": "first line\nsecond line\n",
        "items": ["one", 2, False],
        "base": {"name": "shared", "count": 3},
        "alias": {"name": "shared", "count": 3},
        "empty_mapping": {},
        "empty_sequence": [],
    }
    assert actual["base"] is actual["alias"]
    assert type(actual["integer"]) is int
    assert type(actual["boolean"]) is bool


@pytest.mark.parametrize(
    "source",
    (
        "value: one\nvalue: two\n",
        "outer: {value: one, value: two}\n",
        "{true: one, 1: two}\n",
        "{1: one, 1.0: two}\n",
        "{null: one, ~: two}\n",
        "{key: one, 'key': two}\n",
        "items: [{value: one, value: two}]\n",
        "first: &key same\nmapping: {*key: one, same: two}\n",
    ),
)
def test_exec_08_duplicate_and_colliding_keys_fail(implementation, tmp_path, source):
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(implementation.DuplicateKeyError, match="duplicate YAML key"):
        implementation.load_yaml(tmp_path, "record.yaml")


@pytest.mark.parametrize(
    "source",
    (
        "value: [unterminated\n",
        "value: 'unterminated\n",
        "outer:\n\tvalue: tab-indented\n",
        "value: *missing\n",
        "one: &same 1\ntwo: &same 2\n",
        "value: 1\n---\nvalue: 2\n",
        "value: !unknown payload\n",
        "value: !!python/tuple [one, two]\n",
        "value: !!python/object:builtins.object {}\n",
        "value: !!python/object/apply:builtins.print [must-not-run]\n",
        "base: &base {value: one}\nmerged: {<<: *base}\n",
        "root: &root {self: *root}\n",
        "value: nul\x00byte\n",
        "value: control\x01byte\n",
    ),
)
def test_exec_08_malformed_unsafe_merge_and_recursive_mapping_fail(
    implementation, tmp_path, source, capsys
):
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        implementation.load_yaml(tmp_path, "record.yaml")
    assert "must-not-run" not in capsys.readouterr().out


def test_exec_08_recursive_sequence_keeps_prior_parser_behavior(implementation, tmp_path):
    # The existing safe parser accepts this alias graph; parser selection must
    # neither silently flatten it nor promote it to a valid compiled catalog.
    (tmp_path / "record.yaml").write_text("root: &root [*root]\n", encoding="utf-8")
    actual = implementation.load_yaml(tmp_path, "record.yaml")
    assert isinstance(actual["root"], list)
    assert actual["root"][0] is actual["root"]


@pytest.mark.parametrize("source", ("{[one, two]: value}\n", "{{key: value}: value}\n"))
def test_exec_08_unhashable_mapping_keys_keep_rejection(implementation, tmp_path, source):
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(TypeError, match="unhashable"):
        implementation.load_yaml(tmp_path, "record.yaml")


@pytest.mark.parametrize("source", ("", "null\n", "42\n", "[one, two]\n"))
def test_exec_08_root_must_remain_a_mapping(implementation, tmp_path, source):
    (tmp_path / "record.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="root must be an object"):
        implementation.load_yaml(tmp_path, "record.yaml")


def test_exec_08_utf8_decoding_remains_strict(implementation, tmp_path):
    (tmp_path / "record.yaml").write_bytes(b"value: \xff\n")
    with pytest.raises(UnicodeDecodeError):
        implementation.load_yaml(tmp_path, "record.yaml")


def test_exec_08_byte_limit_boundary_and_post_read_recheck(implementation, tmp_path, monkeypatch):
    path = tmp_path / "record.yaml"
    payload = b"value: valid\n"
    path.write_bytes(payload)
    with monkeypatch.context() as context:
        context.setattr(implementation, "MAX_YAML_BYTES", len(payload))
        assert implementation.load_yaml(tmp_path, "record.yaml") == {"value": "valid"}
        path.write_bytes(payload + b" ")
        with pytest.raises(ValueError, match="byte limit"):
            implementation.load_yaml(tmp_path, "record.yaml")
    path.write_bytes(b"x")
    with monkeypatch.context() as context:
        context.setattr(Path, "read_bytes", lambda self: b"abcd")
        with pytest.raises(ValueError, match="byte limit"):
            implementation.read_bounded(path, maximum_bytes=3)


@pytest.mark.parametrize(
    "relative",
    (
        "",
        "../record.yaml",
        "https://example.invalid/catalog.yaml",
        "file:record.yaml",
        "record.txt",
    ),
)
def test_exec_08_references_stay_local_and_yaml_only(implementation, tmp_path, relative):
    (tmp_path / "record.yaml").write_text("value: valid\n", encoding="utf-8")
    (tmp_path / "record.txt").write_text("value: valid\n", encoding="utf-8")
    with pytest.raises(ValueError):
        implementation.load_yaml(tmp_path, relative)


def test_exec_08_absolute_missing_directory_and_symlink_inputs_fail(implementation, tmp_path):
    path = tmp_path / "record.yaml"
    path.write_text("value: valid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="relative local path"):
        implementation.load_yaml(tmp_path, str(path))
    with pytest.raises(ValueError, match="regular file"):
        implementation.load_yaml(tmp_path, "missing.yaml")
    (tmp_path / "directory.yaml").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        implementation.load_yaml(tmp_path, "directory.yaml")
    (tmp_path / "linked.yaml").symlink_to(path)
    with pytest.raises(ValueError, match="symlinks"):
        implementation.load_yaml(tmp_path, "linked.yaml")
    directory = tmp_path / "actual"
    directory.mkdir()
    (directory / "record.yaml").write_text("value: valid\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        implementation.load_yaml(tmp_path, "linked/record.yaml")


def test_exec_08_each_load_reads_changed_bytes_despite_equal_size_and_time(
    implementation, tmp_path
):
    path = tmp_path / "record.yaml"
    path.write_text("value: first\n", encoding="utf-8")
    before = path.stat()
    first = implementation.load_yaml(tmp_path, "record.yaml")
    path.write_text("value: other\n", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert implementation.load_yaml(tmp_path, "record.yaml") == {"value": "other"}
    assert first == {"value": "first"}
