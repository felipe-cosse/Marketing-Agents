"""AC-16: reuse schema work, never record results or stale catalog inputs."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest
import yaml
from jsonschema.exceptions import SchemaError
from marketing_agents.infrastructure.catalog import (
    CatalogCompilationError,
    compile_catalog,
    compiler,
)

from tests.catalog.test_arch_04_catalog_compiler import CatalogFixture

ROOT = Path(__file__).resolve().parents[2]
STRUCTURAL_NAMES = (
    "manifest.schema.json",
    "department.schema.json",
    "function.schema.json",
    "tool-capability.schema.json",
    "approval-policy.schema.json",
    "template.schema.json",
    "instance.schema.json",
)


@pytest.fixture
def schema_checks(monkeypatch):
    original = compiler.Draft202012Validator.check_schema
    checked = []

    def check(schema, *args, **kwargs):
        checked.append(schema)
        return original(schema, *args, **kwargs)

    monkeypatch.setattr(compiler.Draft202012Validator, "check_schema", staticmethod(check))
    return checked


def schema_file(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "record.schema.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {"value": {"$ref": "#/$defs/value"}},
                "required": ["value"],
                "$defs": {"value": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    return path


def validate(path, value, validators, *, source="records.yaml", issues=None):
    if issues is None:
        issues = []
    compiler._structural_validate(path.parent, path.name, value, source, issues, validators)
    return issues


def test_ac_16_full_catalog_reduces_only_repeated_schema_checks(monkeypatch, schema_checks):
    original = compiler._structural_validate
    records = []

    def observed(*args, **kwargs):
        records.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, "_structural_validate", observed)
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    structural_ids = {
        json.loads((ROOT / "catalog" / "schema" / name).read_text(encoding="utf-8"))["$id"]
        for name in STRUCTURAL_NAMES
    }
    checked_ids = Counter(schema.get("$id") for schema in schema_checks)
    assert len(records) == 121
    assert Counter(records) == {
        "manifest.schema.json": 1,
        "department.schema.json": 5,
        "function.schema.json": 12,
        "tool-capability.schema.json": 22,
        "approval-policy.schema.json": 2,
        "template.schema.json": 36,
        "instance.schema.json": 43,
    }
    assert {identifier: checked_ids[identifier] for identifier in structural_ids} == dict.fromkeys(
        structural_ids, 1
    )
    # Every one of the 72 template schemas remains independently checked.
    io_ids = {
        schema["$id"]
        for schemas in (catalog.input_schema_by_template, catalog.output_schema_by_template)
        for schema in schemas.values()
    }
    assert len(io_ids) == 72
    assert all(checked_ids[identifier] == 1 for identifier in io_ids)
    assert len(schema_checks) == 79  # Previously 121 structural + 72 template checks = 193.
    lock = json.loads((ROOT / "catalog" / "v1" / "release.lock.json").read_text(encoding="utf-8"))
    assert catalog.content_hash == lock["content_hash"]
    assert (len(catalog.templates), len(catalog.instances)) == (36, 43)


def test_ac_16_reused_validator_checks_each_record_and_keeps_each_error(tmp_path, schema_checks):
    path = schema_file(tmp_path)
    validators, issues = {}, []
    assert not validate(path, {"value": "valid"}, validators)
    for source in ("second.yaml", "third.yaml"):
        validate(path, {"value": 7}, validators, source=source, issues=issues)
    assert len(schema_checks) == len(validators) == 1
    assert [(issue.code, issue.source_path, issue.json_pointer) for issue in issues] == [
        ("structural-schema", "second.yaml", "/value"),
        ("structural-schema", "third.yaml", "/value"),
    ]


def test_ac_16_fresh_bytes_detect_equal_size_and_mtime_schema_change(tmp_path, schema_checks):
    path = schema_file(tmp_path)
    before = path.stat()
    original = path.read_bytes()
    validators = {}
    assert not validate(path, {"value": "valid"}, validators)
    changed = original.replace(b'"string"', b'"number"')
    assert len(changed) == len(original) and changed != original
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    issues = validate(path, {"value": "invalid now"}, validators)
    assert [(issue.code, issue.json_pointer) for issue in issues] == [
        ("structural-schema", "/value")
    ]
    assert not validate(path, {"value": 7}, validators)
    assert len(schema_checks) == len(validators) == 2
    assert set(validators) == {(path.resolve(), original), (path.resolve(), changed)}


def test_ac_16_restored_content_reuses_only_its_own_checked_validator(tmp_path, schema_checks):
    path = schema_file(tmp_path)
    original = path.read_bytes()
    validators = {}
    assert not validate(path, {"value": "valid"}, validators)
    path.write_bytes(original.replace(b'"string"', b'"number"'))
    assert not validate(path, {"value": 7}, validators)
    path.write_bytes(original)
    assert not validate(path, {"value": "valid again"}, validators)
    assert validate(path, {"value": 7}, validators)[0].json_pointer == "/value"
    assert len(schema_checks) == 2


@pytest.mark.parametrize("fault", ["json", "metaschema", "utf8", "utf16", "bom", "deleted"])
def test_ac_16_warm_schema_never_masks_read_decode_or_validation_failure(
    tmp_path, schema_checks, fault
):
    path = schema_file(tmp_path)
    original = path.read_bytes()
    validators = {}
    assert not validate(path, {"value": "valid"}, validators)
    if fault == "deleted":
        path.unlink()
        error = FileNotFoundError
    else:
        payload, error = {
            "json": (b"{", json.JSONDecodeError),
            "metaschema": (b'{"type":"not-a-json-schema-type"}', SchemaError),
            "utf8": (b"\xff", UnicodeDecodeError),
            "utf16": (original.decode("utf-8").encode("utf-16"), UnicodeDecodeError),
            "bom": (b"\xef\xbb\xbf" + original, json.JSONDecodeError),
        }[fault]
        path.write_bytes(payload)
    for _ in range(2):
        with pytest.raises(error):
            validate(path, {"value": "never accepted from old schema"}, validators)
    assert len(validators) == 1  # Neither failures nor record results enter the memo.
    assert len(schema_checks) == (3 if fault == "metaschema" else 1)


def test_ac_16_canonical_path_aliases_reuse_but_distinct_paths_do_not(tmp_path, schema_checks):
    path = schema_file(tmp_path / "first")
    other = schema_file(tmp_path / "second")
    alias = path.parent / "alias.schema.json"
    alias.symlink_to(path.name)
    (path.parent / "nested").mkdir()
    validators = {}
    for selected in (path, path.parent / "nested" / ".." / path.name, alias, other):
        assert not validate(selected, {"value": "valid"}, validators)
    assert path.read_bytes() == other.read_bytes()
    assert len(schema_checks) == len(validators) == 2
    assert {identity[0] for identity in validators} == {path.resolve(), other.resolve()}


def test_ac_16_symlink_retarget_resolves_and_reads_the_new_schema(tmp_path, schema_checks):
    first = schema_file(tmp_path / "first")
    second = schema_file(tmp_path / "second")
    second.write_bytes(second.read_bytes().replace(b'"string"', b'"number"'))
    alias = tmp_path / "alias.schema.json"
    alias.symlink_to(first)
    validators = {}
    assert not validate(alias, {"value": "valid"}, validators)
    alias.unlink()
    alias.symlink_to(second)
    issues = validate(alias, {"value": "invalid now"}, validators)
    assert [(issue.code, issue.json_pointer) for issue in issues] == [
        ("structural-schema", "/value")
    ]
    assert len(schema_checks) == 2


def test_ac_16_compilations_never_share_validator_lifetime(tmp_path, monkeypatch, schema_checks):
    fixture = CatalogFixture(str(tmp_path))
    original = compiler._structural_validate
    lifetimes = []

    def observed(*args, **kwargs):
        if args[1] == "manifest.schema.json":
            lifetimes.append(args[5])
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, "_structural_validate", observed)
    first = compile_catalog(fixture.root, contract=fixture.contract)
    assert len(schema_checks) == 9  # Seven structural plus two template schemas.
    second = compile_catalog(fixture.root, contract=fixture.contract)
    assert first.content_hash == second.content_hash
    assert len(schema_checks) == 18
    assert len(lifetimes) == 2 and lifetimes[0] is not lifetimes[1]


def test_ac_16_separate_catalog_roots_keep_their_own_schema_restrictions(tmp_path):
    first = CatalogFixture(str(tmp_path / "first"))
    second = CatalogFixture(str(tmp_path / "second"))
    compile_catalog(first.root, contract=first.contract)
    path = second.catalog_parent / "schema" / "department.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"]["display_name"]["maxLength"] = 1
    path.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(CatalogCompilationError) as caught:
        compile_catalog(second.root, contract=second.contract)
    assert any(
        (issue.code, issue.source_path, issue.json_pointer)
        == ("structural-schema", "departments.yaml", "/display_name")
        for issue in caught.value.issues
    )


@pytest.mark.parametrize("change_schema", [False, True])
def test_ac_16_later_record_is_structurally_checked_after_warmup(
    tmp_path, monkeypatch, schema_checks, change_schema
):
    fixture = CatalogFixture(str(tmp_path))
    document = yaml.safe_load((fixture.root / "departments.yaml").read_text(encoding="utf-8"))
    second = {**document["departments"][0], "id": "dept.second", "display_name": "Second"}
    if not change_schema:
        second["display_name"] = None
    document["departments"].append(second)
    fixture.write_yaml("departments.yaml", document)
    original = compiler._structural_validate
    path = fixture.catalog_parent / "schema" / "department.schema.json"
    department_calls = 0

    def observed(*args, **kwargs):
        nonlocal department_calls
        result = original(*args, **kwargs)
        if args[1] == "department.schema.json":
            department_calls += 1
            if change_schema and department_calls == 1:
                schema = json.loads(path.read_text(encoding="utf-8"))
                schema["properties"]["display_name"]["maxLength"] = 1
                path.write_text(json.dumps(schema), encoding="utf-8")
        return result

    monkeypatch.setattr(compiler, "_structural_validate", observed)
    with pytest.raises(CatalogCompilationError) as caught:
        compile_catalog(fixture.root, contract=fixture.contract)
    assert department_calls == 2
    assert any(
        (issue.code, issue.source_path, issue.json_pointer)
        == ("structural-schema", "departments.yaml", "/display_name")
        for issue in caught.value.issues
    )
    checked = Counter(schema.get("$id") for schema in schema_checks)
    assert checked["urn:marketing-agents:catalog:department:v1"] == (2 if change_schema else 1)
