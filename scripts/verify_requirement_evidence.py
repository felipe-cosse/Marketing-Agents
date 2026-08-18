#!/usr/bin/env python3
"""Validate requirement branches, merge topology, evidence, and executable gates."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = Path("docs/implementation-plan/16-requirements-traceability-matrix.md")
POLICY_PATH = Path("docs/verification/requirement-policy.json")
MANIFEST_ROOT = Path("docs/verification/requirements")
ID_RE = re.compile(r"^[A-Z]+-[0-9]{2}$")
FEATURE_SUBJECT_RE = re.compile(r"^\[([A-Z]+-[0-9]{2})\]\s+\S")
MERGE_SUBJECT_RE = re.compile(r"^merge:\s+([A-Z]+-[0-9]{2})\s+\S", re.IGNORECASE)
CLAIM_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SHELL_META_RE = re.compile(r"[;&|`$><\n\r]")
FORBIDDEN_EXECUTABLES = {"true", "false", "echo", "curl", "wget", "sh", "bash", "zsh", "fish"}
FORBIDDEN_INSTALLERS = {"pip", "pip3", "easy_install"}
EVIDENCE_CLASSES = {
    "source_boundary",
    "decision_record",
    "process_gate",
    "tooling_control",
    "product",
    "security_control",
    "acceptance",
}
GROUP_KINDS = {"source", "decision", "implementation", "control", "test", "journey"}
CLAIM_TYPES = {"positive", "negative_control", "contract", "journey", "documentation"}
NETWORK_REQUIREMENTS = {"not_required", "not_enforced", "loopback_only", "deny"}


class EvidenceError(RuntimeError):
    """Raised when requirement evidence is structurally or semantically invalid."""


@dataclass(frozen=True)
class MatrixEntry:
    requirement_id: str
    statement: str
    status: str

    @property
    def statement_sha256(self) -> str:
        return hashlib.sha256(self.statement.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    subject: str


@dataclass
class GateResult:
    gate_id: str
    argv: list[str]
    exit_code: int
    duration_seconds: float
    output_sha256: str
    network_requirement: str


@dataclass
class RequirementResult:
    requirement_id: str
    feature_commit: str
    merge_commit: str | None
    changed_paths: list[str]
    legacy_bootstrap: bool
    gate_results: list[GateResult] = field(default_factory=list)
    witness_exit_code: int | None = None


@dataclass
class HistoryResult:
    requirement_count: int
    completed: list[str]
    missing: list[str]
    results: list[RequirementResult]


class GitRepo:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def run(self, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=check,
            capture_output=True,
            text=text,
        )

    def output(self, *args: str) -> str:
        return self.run(*args).stdout

    def resolve(self, ref: str) -> str:
        return self.output("rev-parse", "--verify", ref).strip()

    def commit(self, ref: str) -> Commit:
        raw = self.output("show", "-s", "--format=%H%x1f%P%x1f%s", ref).rstrip("\n")
        sha, parents, subject = raw.split("\x1f", 2)
        return Commit(sha=sha, parents=tuple(parents.split()) if parents else (), subject=subject)

    def first_parent_commits(self, start: str, end: str) -> list[Commit]:
        raw = self.output("log", "--first-parent", "--reverse", "--format=%H%x1f%P%x1f%s", f"{start}..{end}")
        commits: list[Commit] = []
        for line in raw.splitlines():
            if not line:
                continue
            sha, parents, subject = line.split("\x1f", 2)
            commits.append(Commit(sha, tuple(parents.split()) if parents else (), subject))
        return commits

    def changed_paths(self, base: str, head: str) -> list[str]:
        return sorted(filter(None, self.output("diff", "--name-only", base, head).splitlines()))

    def tree(self, ref: str) -> str:
        return self.output("rev-parse", f"{ref}^{{tree}}").strip()

    def merge_base(self, left: str, right: str) -> str:
        return self.output("merge-base", left, right).strip()

    def blob_text(self, ref: str, path: str) -> str:
        return self.output("show", f"{ref}:{path}")

    def blob_bytes(self, ref: str, path: str) -> bytes | None:
        result = self.run("show", f"{ref}:{path}", check=False, text=False)
        return result.stdout if result.returncode == 0 else None

    def has_path(self, ref: str, path: str) -> bool:
        return self.run("cat-file", "-e", f"{ref}:{path}", check=False).returncode == 0

    def branch_refs(self, requirement_id: str) -> list[tuple[str, str]]:
        pattern = f"refs/heads/req/{requirement_id.lower()}-*"
        raw = self.output("for-each-ref", "--format=%(refname)%09%(objectname)", pattern)
        refs: list[tuple[str, str]] = []
        for line in raw.splitlines():
            if "\t" in line:
                name, sha = line.split("\t", 1)
                refs.append((name, sha))
        return refs


def _load_json_text(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{label}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label}: root must be an object")
    return value


def load_policy(root: Path) -> dict[str, Any]:
    return _load_json_text((root / POLICY_PATH).read_text(encoding="utf-8"), str(POLICY_PATH))


def parse_matrix_text(text: str) -> dict[str, MatrixEntry]:
    entries: dict[str, MatrixEntry] = {}
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        if not columns or not ID_RE.fullmatch(columns[0]):
            continue
        if len(columns) < 3:
            raise EvidenceError(f"matrix row {columns[0]} is incomplete")
        requirement_id = columns[0]
        if requirement_id in entries:
            raise EvidenceError(f"matrix contains duplicate requirement ID {requirement_id}")
        entries[requirement_id] = MatrixEntry(requirement_id, columns[1], columns[-1])
    if not entries:
        raise EvidenceError("matrix contains no requirement IDs")
    return entries


def load_matrix(root: Path, repo: GitRepo | None = None, ref: str | None = None) -> dict[str, MatrixEntry]:
    if repo is not None and ref is not None:
        return parse_matrix_text(repo.blob_text(ref, str(MATRIX_PATH)))
    return parse_matrix_text((root / MATRIX_PATH).read_text(encoding="utf-8"))


def _expect_keys(value: dict[str, Any], required: set[str], optional: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required - optional)
    if missing:
        raise EvidenceError(f"{label}: missing fields {', '.join(missing)}")
    if extra:
        raise EvidenceError(f"{label}: unknown fields {', '.join(extra)}")


def _string_list(value: Any, label: str, *, minimum: int = 0, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or not (minimum <= len(value) <= maximum):
        raise EvidenceError(f"{label}: expected {minimum}..{maximum} strings")
    if not all(isinstance(item, str) and item and len(item) <= 500 for item in value):
        raise EvidenceError(f"{label}: contains an invalid string")
    if len(value) != len(set(value)):
        raise EvidenceError(f"{label}: duplicate values are not allowed")
    return value


def expected_evidence_class(requirement_id: str, policy: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    overrides = policy.get("requirement_overrides", {})
    override = overrides.get(requirement_id, {}) if isinstance(overrides, dict) else {}
    prefix = requirement_id.split("-", 1)[0]
    category_classes = policy.get("category_classes")
    if not isinstance(category_classes, dict) or prefix not in category_classes:
        raise EvidenceError(f"policy has no evidence class for {requirement_id}")
    evidence_class = override.get("evidence_class", category_classes[prefix])
    defaults = policy.get("class_defaults")
    if evidence_class not in EVIDENCE_CLASSES or not isinstance(defaults, dict) or evidence_class not in defaults:
        raise EvidenceError(f"policy has invalid evidence class for {requirement_id}")
    merged = dict(defaults[evidence_class])
    merged.update({key: value for key, value in override.items() if key != "evidence_class"})
    return evidence_class, merged


def validate_gate(gate: dict[str, Any], claim_ids: set[str], label: str) -> None:
    _expect_keys(
        gate,
        {"id", "runner", "argv", "cwd", "timeout_seconds", "network_requirement", "covers"},
        set(),
        label,
    )
    gate_id = gate["id"]
    if not isinstance(gate_id, str) or not CLAIM_ID_RE.fullmatch(gate_id):
        raise EvidenceError(f"{label}: invalid gate ID")
    if gate["runner"] != "argv":
        raise EvidenceError(f"{label}: only argv runner is allowed")
    argv = _string_list(gate["argv"], f"{label}.argv", minimum=2, maximum=32)
    executable = Path(argv[0]).name
    if executable in FORBIDDEN_EXECUTABLES or executable in FORBIDDEN_INSTALLERS:
        raise EvidenceError(f"{label}: forbidden executable {executable}")
    if executable in {"npm", "pnpm", "yarn", "uv"} and any(arg in {"install", "add", "sync"} for arg in argv[1:]):
        raise EvidenceError(f"{label}: dependency installation is not an evidence gate")
    if "-c" in argv or any(SHELL_META_RE.search(arg) for arg in argv):
        raise EvidenceError(f"{label}: shell code and metacharacters are forbidden")
    cwd = gate["cwd"]
    if not isinstance(cwd, str) or not cwd or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
        raise EvidenceError(f"{label}: cwd must remain inside the repository")
    timeout = gate["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 1800:
        raise EvidenceError(f"{label}: timeout must be 1..1800 seconds")
    if gate["network_requirement"] not in NETWORK_REQUIREMENTS:
        raise EvidenceError(f"{label}: invalid network requirement")
    covers = set(_string_list(gate["covers"], f"{label}.covers", minimum=1, maximum=32))
    if not covers <= claim_ids:
        raise EvidenceError(f"{label}: covers unknown claims {sorted(covers - claim_ids)}")


def validate_manifest(
    manifest: dict[str, Any],
    requirement_id: str,
    matrix: dict[str, MatrixEntry],
    policy: dict[str, Any],
) -> None:
    _expect_keys(
        manifest,
        {"$schema", "schema_version", "requirement_id", "matrix_statement_sha256", "evidence_class", "scope", "claims", "gates", "limitations"},
        {"legacy_bootstrap", "legacy_manifest_added_at", "connection_witness"},
        requirement_id,
    )
    if manifest["$schema"] != "../schema/requirement-evidence.schema.json" or manifest["schema_version"] != 1:
        raise EvidenceError(f"{requirement_id}: unsupported evidence schema")
    if manifest["requirement_id"] != requirement_id or requirement_id not in matrix:
        raise EvidenceError(f"{requirement_id}: manifest ID/filename/matrix mismatch")
    digest = manifest["matrix_statement_sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or digest != matrix[requirement_id].statement_sha256:
        raise EvidenceError(f"{requirement_id}: matrix statement digest mismatch")
    expected_class, defaults = expected_evidence_class(requirement_id, policy)
    if manifest["evidence_class"] != expected_class:
        raise EvidenceError(f"{requirement_id}: evidence class must be {expected_class}")

    legacy_ids = set(policy.get("legacy_bootstrap_ids", []))
    legacy = requirement_id in legacy_ids
    if bool(manifest.get("legacy_bootstrap")) != legacy:
        raise EvidenceError(f"{requirement_id}: legacy bootstrap declaration mismatch")
    if legacy and manifest.get("legacy_manifest_added_at") != policy.get("legacy_manifests_added_by"):
        raise EvidenceError(f"{requirement_id}: legacy manifest carrier mismatch")
    if not legacy and "legacy_manifest_added_at" in manifest:
        raise EvidenceError(f"{requirement_id}: strict manifest cannot claim a legacy carrier")

    scope = manifest["scope"]
    if not isinstance(scope, dict):
        raise EvidenceError(f"{requirement_id}.scope: expected object")
    _expect_keys(scope, {"allowed_path_globs", "required_path_groups"}, set(), f"{requirement_id}.scope")
    _string_list(scope["allowed_path_globs"], f"{requirement_id}.scope.allowed_path_globs", minimum=1)
    groups = scope["required_path_groups"]
    if not isinstance(groups, list) or not 1 <= len(groups) <= 16:
        raise EvidenceError(f"{requirement_id}: required_path_groups must be a nonempty list")
    group_kinds: set[str] = set()
    for index, group in enumerate(groups):
        label = f"{requirement_id}.scope.required_path_groups[{index}]"
        if not isinstance(group, dict):
            raise EvidenceError(f"{label}: expected object")
        _expect_keys(group, {"kind", "one_of"}, set(), label)
        if group["kind"] not in GROUP_KINDS:
            raise EvidenceError(f"{label}: invalid kind")
        group_kinds.add(group["kind"])
        _string_list(group["one_of"], f"{label}.one_of", minimum=1, maximum=32)
    required_kinds = set(defaults.get("required_group_kinds", []))
    if not required_kinds <= group_kinds:
        raise EvidenceError(f"{requirement_id}: missing required path groups {sorted(required_kinds - group_kinds)}")

    claims = manifest["claims"]
    if not isinstance(claims, list) or len(claims) < int(defaults.get("minimum_claims", 1)):
        raise EvidenceError(f"{requirement_id}: insufficient claims")
    claim_ids: set[str] = set()
    for index, claim in enumerate(claims):
        label = f"{requirement_id}.claims[{index}]"
        if not isinstance(claim, dict):
            raise EvidenceError(f"{label}: expected object")
        _expect_keys(claim, {"id", "type", "statement", "test_refs"}, set(), label)
        claim_id = claim["id"]
        if not isinstance(claim_id, str) or not CLAIM_ID_RE.fullmatch(claim_id) or claim_id in claim_ids:
            raise EvidenceError(f"{label}: invalid or duplicate claim ID")
        claim_ids.add(claim_id)
        if claim["type"] not in CLAIM_TYPES:
            raise EvidenceError(f"{label}: invalid claim type")
        if not isinstance(claim["statement"], str) or not 10 <= len(claim["statement"]) <= 500:
            raise EvidenceError(f"{label}: statement must be 10..500 characters")
        _string_list(claim["test_refs"], f"{label}.test_refs", minimum=1, maximum=16)

    gates = manifest["gates"]
    if not isinstance(gates, list) or len(gates) < int(defaults.get("minimum_gates", 1)):
        raise EvidenceError(f"{requirement_id}: insufficient gates")
    gate_ids: set[str] = set()
    covered: set[str] = set()
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            raise EvidenceError(f"{requirement_id}.gates[{index}]: expected object")
        validate_gate(gate, claim_ids, f"{requirement_id}.gates[{index}]")
        if gate["id"] in gate_ids:
            raise EvidenceError(f"{requirement_id}: duplicate gate ID {gate['id']}")
        gate_ids.add(gate["id"])
        covered.update(gate["covers"])
    if covered != claim_ids:
        raise EvidenceError(f"{requirement_id}: uncovered claims {sorted(claim_ids - covered)}")

    limitations = manifest["limitations"]
    _string_list(limitations, f"{requirement_id}.limitations", maximum=16)
    witness_required = bool(defaults.get("connection_witness"))
    witness = manifest.get("connection_witness")
    if witness_required and not isinstance(witness, dict):
        raise EvidenceError(f"{requirement_id}: connection witness is required")
    if witness is not None:
        if not isinstance(witness, dict):
            raise EvidenceError(f"{requirement_id}.connection_witness: expected object")
        _expect_keys(witness, {"implementation_path_globs", "gate_id"}, set(), f"{requirement_id}.connection_witness")
        _string_list(witness["implementation_path_globs"], f"{requirement_id}.connection_witness.implementation_path_globs", minimum=1, maximum=32)
        if witness["gate_id"] not in gate_ids:
            raise EvidenceError(f"{requirement_id}: witness references an unknown gate")


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def validate_paths(
    repo: GitRepo,
    requirement_id: str,
    base: str,
    head: str,
    manifest: dict[str, Any],
    policy: dict[str, Any],
    *,
    legacy: bool,
) -> list[str]:
    changed = repo.changed_paths(base, head)
    if not changed or repo.tree(base) == repo.tree(head):
        raise EvidenceError(f"{requirement_id}: feature commit has no tree change")
    evidence_only = policy.get("evidence_only_path_globs", [])
    substantive = [path for path in changed if not _matches_any(path, evidence_only)]
    if not substantive:
        raise EvidenceError(f"{requirement_id}: branch delta is evidence-only")
    allowed = manifest["scope"]["allowed_path_globs"]
    disallowed = [path for path in changed if not _matches_any(path, allowed)]
    if disallowed:
        raise EvidenceError(f"{requirement_id}: changed paths outside manifest scope: {', '.join(disallowed)}")
    protected = policy.get("protected_path_globs", [])
    protected_changed = [path for path in changed if _matches_any(path, protected)]
    if protected_changed and not legacy and requirement_id not in set(policy.get("protected_carrier_ids", [])):
        raise EvidenceError(f"{requirement_id}: unauthorized evidence-protocol change: {', '.join(protected_changed)}")

    available = set(changed)
    if legacy:
        available.update(path.relative_to(repo.root).as_posix() for path in repo.root.rglob("*") if path.is_file())
    for group in manifest["scope"]["required_path_groups"]:
        if not any(_matches_any(path, group["one_of"]) for path in available):
            raise EvidenceError(f"{requirement_id}: required {group['kind']} path group is not satisfied")

    diff = repo.output("diff", "--unified=0", base, head, "--", str(MATRIX_PATH))
    touched_ids: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith(("+| ", "-| ")):
            continue
        columns = [column.strip() for column in line[1:].strip().strip("|").split("|")]
        if columns and ID_RE.fullmatch(columns[0]):
            touched_ids.add(columns[0])
    if touched_ids - {requirement_id}:
        raise EvidenceError(f"{requirement_id}: matrix rows for other IDs changed: {sorted(touched_ids - {requirement_id})}")
    return changed


def validate_test_refs(repo: GitRepo, ref: str, requirement_id: str, manifest: dict[str, Any], *, legacy: bool) -> None:
    marker_variants = {requirement_id, requirement_id.lower(), requirement_id.lower().replace("-", "_")}
    for claim in manifest["claims"]:
        for test_ref in claim["test_refs"]:
            path = test_ref.split("::", 1)[0]
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise EvidenceError(f"{requirement_id}: test_ref escapes repository: {test_ref}")
            if legacy:
                file_path = repo.root / path
                if not file_path.is_file():
                    raise EvidenceError(f"{requirement_id}: missing legacy test_ref path {path}")
                text = file_path.read_text(encoding="utf-8")
            else:
                if not repo.has_path(ref, path):
                    raise EvidenceError(f"{requirement_id}: missing test_ref path {path} in feature tree")
                text = repo.blob_text(ref, path)
            if not any(marker in text for marker in marker_variants):
                raise EvidenceError(f"{requirement_id}: test_ref {path} lacks a requirement marker")


def sanitized_environment(temp_home: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(temp_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    return env


def execute_gate(root: Path, gate: dict[str, Any]) -> GateResult:
    with tempfile.TemporaryDirectory(prefix="marketing-agents-gate-home-") as home:
        started = time.monotonic()
        try:
            result = subprocess.run(
                gate["argv"],
                cwd=(root / gate["cwd"]).resolve(),
                env=sanitized_environment(Path(home)),
                capture_output=True,
                text=True,
                shell=False,
                timeout=gate["timeout_seconds"],
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceError(f"gate {gate['id']} timed out after {gate['timeout_seconds']} seconds") from exc
        duration = time.monotonic() - started
        output = (result.stdout + "\n" + result.stderr).encode("utf-8", errors="replace")
        gate_result = GateResult(
            gate_id=gate["id"],
            argv=list(gate["argv"]),
            exit_code=result.returncode,
            duration_seconds=duration,
            output_sha256=hashlib.sha256(output).hexdigest(),
            network_requirement=gate["network_requirement"],
        )
        if result.returncode != 0:
            tail = (result.stdout + result.stderr)[-2000:]
            raise EvidenceError(f"gate {gate['id']} failed with exit {result.returncode}:\n{tail}")
        return gate_result


def _safe_extract_tar(payload: bytes, destination: Path) -> None:
    archive_path = destination.parent / "tree.tar"
    archive_path.write_bytes(payload)
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() != target and destination.resolve() not in target.parents:
                raise EvidenceError("git archive contains an escaped path")
        archive.extractall(destination, filter="data")
    archive_path.unlink()


def execute_connection_witness(
    repo: GitRepo,
    base: str,
    head: str,
    manifest: dict[str, Any],
) -> int | None:
    witness = manifest.get("connection_witness")
    if not witness:
        return None
    gate = next(gate for gate in manifest["gates"] if gate["id"] == witness["gate_id"])
    implementation_paths = [
        path
        for path in repo.changed_paths(base, head)
        if _matches_any(path, witness["implementation_path_globs"])
    ]
    if not implementation_paths:
        raise EvidenceError("connection witness matches no changed implementation path")
    archive = repo.run("archive", "--format=tar", head, text=False).stdout
    with tempfile.TemporaryDirectory(prefix="marketing-agents-witness-") as temporary:
        root = Path(temporary) / "tree"
        root.mkdir()
        _safe_extract_tar(archive, root)
        for relative in implementation_paths:
            destination = root / relative
            base_content = repo.blob_bytes(base, relative)
            if base_content is None:
                if destination.is_file() or destination.is_symlink():
                    destination.unlink()
                elif destination.is_dir():
                    shutil.rmtree(destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(base_content)
        with tempfile.TemporaryDirectory(prefix="marketing-agents-witness-home-") as home:
            try:
                result = subprocess.run(
                    gate["argv"],
                    cwd=(root / gate["cwd"]).resolve(),
                    env=sanitized_environment(Path(home)),
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=gate["timeout_seconds"],
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return 124
        if result.returncode == 0:
            raise EvidenceError("connection witness stayed green after implementation paths were restored to the base")
        return result.returncode


def write_attestation(
    repo: GitRepo,
    requirement_id: str,
    feature_commit: str,
    manifest: dict[str, Any],
    gate_results: list[GateResult],
    witness_exit_code: int | None,
) -> Path:
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tree = repo.tree(feature_commit)
    destination_root = Path("/tmp/marketing-agents-requirement-evidence")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"{requirement_id}-{tree[:16]}.json"
    payload = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "feature_commit": feature_commit,
        "tree": tree,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "executed_at_epoch_seconds": int(time.time()),
        "gates": [
            {
                "id": result.gate_id,
                "argv": result.argv,
                "exit_code": result.exit_code,
                "duration_seconds": round(result.duration_seconds, 6),
                "output_sha256": result.output_sha256,
                "network_requirement": result.network_requirement,
            }
            for result in gate_results
        ],
        "connection_witness_exit_code": witness_exit_code,
    }
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def load_manifest_for_feature(
    repo: GitRepo,
    feature_commit: str,
    requirement_id: str,
    *,
    legacy: bool,
) -> dict[str, Any]:
    path = MANIFEST_ROOT / f"{requirement_id}.json"
    if legacy:
        if not (repo.root / path).is_file():
            raise EvidenceError(f"{requirement_id}: missing legacy manifest backfill")
        return _load_json_text((repo.root / path).read_text(encoding="utf-8"), str(path))
    if not repo.has_path(feature_commit, str(path)):
        raise EvidenceError(f"{requirement_id}: strict manifest is absent from feature commit")
    return _load_json_text(repo.blob_text(feature_commit, str(path)), str(path))


def validate_requirement(
    repo: GitRepo,
    requirement_id: str,
    base: str,
    feature_commit: str,
    merge_commit: str | None,
    matrix: dict[str, MatrixEntry],
    policy: dict[str, Any],
    *,
    run_gates: bool,
    run_witness: bool,
) -> RequirementResult:
    legacy = requirement_id in set(policy.get("legacy_bootstrap_ids", []))
    manifest = load_manifest_for_feature(repo, feature_commit, requirement_id, legacy=legacy)
    validate_manifest(manifest, requirement_id, matrix, policy)
    changed = validate_paths(repo, requirement_id, base, feature_commit, manifest, policy, legacy=legacy)
    validate_test_refs(repo, feature_commit, requirement_id, manifest, legacy=legacy)
    gate_results: list[GateResult] = []
    witness_exit_code: int | None = None
    if run_gates:
        gate_results = [execute_gate(repo.root, gate) for gate in manifest["gates"]]
        if run_witness:
            witness_exit_code = execute_connection_witness(repo, base, feature_commit, manifest)
        write_attestation(repo, requirement_id, feature_commit, manifest, gate_results, witness_exit_code)
    return RequirementResult(requirement_id, feature_commit, merge_commit, changed, legacy, gate_results, witness_exit_code)


def validate_feature_topology(repo: GitRepo, requirement_id: str, base: str, feature: str) -> Commit:
    commit = repo.commit(feature)
    if len(commit.parents) != 1 or commit.parents[0] != base:
        raise EvidenceError(f"{requirement_id}: feature must be one direct, non-merge commit on the target base")
    match = FEATURE_SUBJECT_RE.match(commit.subject)
    if not match or match.group(1) != requirement_id:
        raise EvidenceError(f"{requirement_id}: feature subject must start with [{requirement_id}]")
    if repo.tree(base) == repo.tree(feature):
        raise EvidenceError(f"{requirement_id}: empty feature commit is forbidden")
    return commit


def validate_branch(
    root: Path,
    requirement_id: str,
    base_ref: str,
    head_ref: str,
    *,
    run_gates: bool,
    run_witness: bool,
) -> RequirementResult:
    repo = GitRepo(root)
    base = repo.resolve(base_ref)
    head = repo.resolve(head_ref)
    matrix = load_matrix(root)
    policy = load_policy(root)
    if requirement_id not in matrix:
        raise EvidenceError(f"unknown requirement ID {requirement_id}")
    validate_feature_topology(repo, requirement_id, base, head)
    return validate_requirement(
        repo,
        requirement_id,
        base,
        head,
        None,
        matrix,
        policy,
        run_gates=run_gates,
        run_witness=run_witness,
    )


def validate_history(
    root: Path,
    ref: str,
    *,
    allow_incomplete: bool,
    check_branches: bool,
    run_all: bool,
    run_latest: bool,
    run_witness: bool,
) -> HistoryResult:
    repo = GitRepo(root)
    policy = load_policy(root)
    baseline = repo.resolve(str(policy.get("baseline_commit", "")))
    target = repo.resolve(ref)
    matrix = load_matrix(root, repo, target)
    mainline = repo.first_parent_commits(baseline, target)
    seen: set[str] = set()
    topology: list[tuple[str, str, str, str]] = []
    for commit in mainline:
        match = MERGE_SUBJECT_RE.match(commit.subject)
        if not match:
            raise EvidenceError(f"mainline commit {commit.sha[:12]} is not a requirement merge: {commit.subject}")
        requirement_id = match.group(1).upper()
        if requirement_id not in matrix:
            raise EvidenceError(f"unknown requirement merge ID {requirement_id}")
        if requirement_id in seen:
            raise EvidenceError(f"duplicate requirement merge {requirement_id}")
        seen.add(requirement_id)
        if len(commit.parents) != 2:
            raise EvidenceError(f"{requirement_id}: merge commit must have exactly two parents")
        base, feature = commit.parents
        if repo.merge_base(base, feature) != base:
            raise EvidenceError(f"{requirement_id}: feature is not based on merge parent one")
        validate_feature_topology(repo, requirement_id, base, feature)
        if repo.tree(commit.sha) != repo.tree(feature):
            raise EvidenceError(f"{requirement_id}: merge tree differs from the feature tree")
        topology.append((requirement_id, base, feature, commit.sha))

    feature_subject_counts: dict[str, int] = {}
    raw_subjects = repo.output("log", target, "--format=%s")
    for subject in raw_subjects.splitlines():
        match = FEATURE_SUBJECT_RE.match(subject)
        if match:
            key = match.group(1)
            feature_subject_counts[key] = feature_subject_counts.get(key, 0) + 1
    for requirement_id in seen:
        if feature_subject_counts.get(requirement_id) != 1:
            raise EvidenceError(f"{requirement_id}: expected exactly one reachable feature commit")
    unknown_features = sorted(set(feature_subject_counts) - set(matrix))
    if unknown_features:
        raise EvidenceError(f"unknown feature IDs in history: {', '.join(unknown_features)}")

    run_ids: set[str] = set()
    if run_all:
        run_ids = seen
    elif run_latest and topology:
        run_ids = {topology[-1][0]}
    results: list[RequirementResult] = []
    for requirement_id, base, feature, merge in topology:
        result = validate_requirement(
            repo,
            requirement_id,
            base,
            feature,
            merge,
            matrix,
            policy,
            run_gates=requirement_id in run_ids,
            run_witness=run_witness and requirement_id in run_ids,
        )
        if check_branches:
            refs = repo.branch_refs(requirement_id)
            if len(refs) != 1 or refs[0][1] != feature:
                raise EvidenceError(f"{requirement_id}: retained req/ branch must uniquely point to the feature commit")
        results.append(result)

    completed = [item[0] for item in topology]
    missing = sorted(set(matrix) - set(completed))
    if missing and not allow_incomplete:
        raise EvidenceError(f"missing requirement merges: {', '.join(missing)}")
    return HistoryResult(len(matrix), completed, missing, results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    branch = subparsers.add_parser("branch", help="validate one unmerged requirement branch")
    branch.add_argument("--id", required=True, dest="requirement_id")
    branch.add_argument("--base", default="main")
    branch.add_argument("--head", default="HEAD")
    branch.add_argument("--run", action="store_true")
    branch.add_argument("--witness", action="store_true")
    history = subparsers.add_parser("history", help="validate target first-parent requirement history")
    history.add_argument("--ref", default="main")
    history.add_argument("--allow-incomplete", action="store_true")
    history.add_argument("--check-branches", action="store_true")
    run_group = history.add_mutually_exclusive_group()
    run_group.add_argument("--run-all", action="store_true")
    run_group.add_argument("--run-latest", action="store_true")
    history.add_argument("--witness", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "branch":
            if not ID_RE.fullmatch(args.requirement_id):
                raise EvidenceError("invalid requirement ID")
            result = validate_branch(
                ROOT,
                args.requirement_id,
                args.base,
                args.head,
                run_gates=args.run,
                run_witness=args.witness,
            )
            print(
                f"requirement={result.requirement_id} feature={result.feature_commit[:12]} "
                f"paths={len(result.changed_paths)} gates={len(result.gate_results)}"
            )
        else:
            result = validate_history(
                ROOT,
                args.ref,
                allow_incomplete=args.allow_incomplete,
                check_branches=args.check_branches,
                run_all=args.run_all,
                run_latest=args.run_latest,
                run_witness=args.witness,
            )
            print(
                f"requirements={result.requirement_count} completed={len(result.completed)} "
                f"missing={len(result.missing)}"
            )
            if result.missing:
                print("missing=" + ",".join(result.missing))
        return 0
    except (EvidenceError, subprocess.CalledProcessError, OSError) as exc:
        print(f"requirement evidence error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
