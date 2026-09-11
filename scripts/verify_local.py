"""DEL-07: ordered, bounded local gates with isolated coverage and source-drift checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_TIMEOUT_SECONDS = 30
TERMINATION_GRACE_SECONDS = 5
MAX_COMMITTED_SOURCE_BYTES = 128 * 1024 * 1024
MAX_COMMITTED_SOURCE_FILES = 100_000
BACKEND_GATE_NAMES = (
    "catalog-validate",
    "catalog-release",
    "catalog-tests",
    "backend-format",
    "backend-lint",
    "backend-types",
    "backend-tests",
    "safety-coverage",
)


class VerificationError(RuntimeError):
    """Bounded failure codes; never retain raw command output or environment."""


class VerificationInterrupted(VerificationError):
    pass


@dataclass(frozen=True)
class Gate:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not self.name or not self.argv or any(not item for item in self.argv):
            raise ValueError("gate identity and command must be nonempty")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 1800:
            raise ValueError("gate timeout must be an integer from 1 through 1800 seconds")


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    returncode: int | None
    elapsed_ms: int


@dataclass(frozen=True)
class SourceFile:
    kind: str
    mode: int
    digest: str


def gates(
    coverage_report: Path, *, acceptance: bool = False, backend: bool = False, ref: str = "HEAD"
) -> tuple[Gate, ...]:
    """Each Make target is its own serial process, even under a parallel parent Make."""
    if acceptance and backend:
        raise VerificationError("verification_modes_mutually_exclusive")

    def make(name: str, target: str, timeout: int = 300, *variables: str) -> Gate:
        return Gate(name, ("make", "-j1", f"PYTHON={sys.executable}", *variables, target), timeout)

    result: tuple[Gate, ...] = (
        make("catalog-validate", "catalog-validate"),
        make("catalog-release", "verify-catalog-release"),
        Gate(
            "catalog-tests",
            (
                sys.executable,
                "-m",
                "pytest",
                "tests/catalog",
                "--cov=marketing_agents",
                "--cov-branch",
                "--cov-report=",
            ),
            600,
        ),
        make("backend-format", "format-check"),
        make("backend-lint", "lint"),
        make("backend-types", "typecheck", 600),
        make("repository", "verify-repository", 900),
        make("api-contract", "api-contract-check"),
        make("network-canaries", "test-network"),
        Gate("api-contract-tests", ("node", "--test", "tools/api-contract/generate.test.mjs"), 300),
        Gate(
            "browser-inventory-tests",
            ("node", "--test", "tests/network/web_e2e_inventory.test.mjs"),
            300,
        ),
        Gate(
            "backend-tests",
            (
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "--ignore=tests/catalog",
                "--cov=marketing_agents",
                "--cov-branch",
                "--cov-append",
                f"--cov-report=json:{coverage_report}",
            ),
            1800,
        ),
        Gate(
            "safety-coverage",
            (
                sys.executable,
                "-m",
                "scripts.check_safety_coverage",
                "--report",
                str(coverage_report),
            ),
            60,
        ),
        make("web-format", "web-format-check"),
        make("web-lint", "web-lint"),
        make("web-types", "web-typecheck", 600),
        make("web-unit", "web-test", 1800),
        make("web-build", "web-build", 600),
        Gate(
            "browser-network-canary",
            ("node", "apps/web/scripts/run-browser-network-canary.mjs"),
            300,
        ),
        make("web-browser", "web-test-e2e", 1800),
    )
    if backend:
        result = tuple(gate for gate in result if gate.name in BACKEND_GATE_NAMES)
        if not result or tuple(gate.name for gate in result) != BACKEND_GATE_NAMES:
            raise VerificationError("backend_gate_inventory_invalid")
    if acceptance:
        # DEL-05 owns its internal execution and cleanup deadlines unchanged.
        result += (
            make("acceptance-clean", "verify-clean", 1800, f"REF={ref}"),
            make("acceptance-backup", "test-del-05-compose-backup", 1800),
        )
    return result


def _git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=SNAPSHOT_TIMEOUT_SECONDS,
            input=input_bytes,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerificationError("source_git_unavailable") from error
    if result.returncode != 0:
        raise VerificationError("source_git_failed")
    return result.stdout


def source_snapshot(root: Path) -> dict[str, SourceFile]:
    """Hash caller source as it exists, including an initially dirty/deleted state.

    Git's existing ignore rules are the only exclusions. Symlinks are hashed as
    links, never followed; regular files are read without invoking filters.
    """

    raw = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if not raw or not raw.endswith(b"\0"):
        raise VerificationError("source_inventory_empty_or_invalid")
    names = {os.fsdecode(name) for name in raw[:-1].split(b"\0")}
    result: dict[str, SourceFile] = {}
    for name in sorted(names):
        relative = PurePosixPath(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            raise VerificationError("source_inventory_path_invalid")
        path = root / name
        try:
            parent_inside = path.parent.resolve().is_relative_to(root.resolve())
        except (OSError, RuntimeError) as error:
            raise VerificationError("source_parent_unreadable") from error
        if not parent_inside:
            raise VerificationError("source_parent_escapes_repository")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            result[name] = SourceFile("missing", 0, "")
            continue
        except OSError as error:
            raise VerificationError("source_metadata_unreadable") from error
        mode = stat.S_IMODE(metadata.st_mode)
        try:
            if stat.S_ISLNK(metadata.st_mode):
                target = os.fsencode(os.readlink(path))
                result[name] = SourceFile("symlink", mode, hashlib.sha256(target).hexdigest())
            elif stat.S_ISREG(metadata.st_mode):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise VerificationError("source_changed_during_snapshot")
                    digest = hashlib.sha256()
                    while block := stream.read(1_048_576):
                        digest.update(block)
                    after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    after.st_mode,
                ):
                    raise VerificationError("source_changed_during_snapshot")
                result[name] = SourceFile("file", mode, digest.hexdigest())
            else:
                raise VerificationError("source_file_type_unsupported")
        except OSError as error:
            raise VerificationError("source_content_unreadable") from error
    if not result:
        raise VerificationError("source_inventory_empty_or_invalid")
    return result


def _resolve_ref(root: Path, ref: str) -> str:
    raw = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    value = raw.strip().decode("ascii", errors="replace")
    if len(value) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise VerificationError("acceptance_ref_invalid")
    return value


def committed_snapshot(root: Path, ref: str) -> dict[str, SourceFile]:
    """Read exact committed bytes in two Git processes, bypassing index hints/filters.

    Git stores only the regular-file owner executable bit and symlink type, not
    other filesystem permission bits. Bound inventory and total blob bytes before
    requesting contents; neither object contents nor filenames enter diagnostics.
    """
    raw = _git(root, "ls-tree", "-r", "-z", "-l", ref)
    if not raw or not raw.endswith(b"\0"):
        raise VerificationError("committed_source_inventory_invalid")
    records = raw[:-1].split(b"\0")
    if len(records) > MAX_COMMITTED_SOURCE_FILES:
        raise VerificationError("committed_source_inventory_too_large")
    entries: dict[str, tuple[int, bytes]] = {}
    sizes: dict[bytes, int] = {}
    total = 0
    for record in records:
        try:
            metadata, path = record.split(b"\t", 1)
            mode, kind, object_id, raw_size = metadata.split()
        except ValueError:
            raise VerificationError("committed_source_inventory_invalid") from None
        name = os.fsdecode(path)
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or name in entries
            or mode not in {b"100644", b"100755", b"120000"}
            or kind != b"blob"
            or len(object_id) not in {40, 64}
            or any(character not in b"0123456789abcdef" for character in object_id)
            or not raw_size.isdigit()
            or len(raw_size) > 10
        ):
            raise VerificationError("committed_source_inventory_invalid")
        size = int(raw_size)
        if object_id in sizes and sizes[object_id] != size:
            raise VerificationError("committed_source_inventory_invalid")
        if object_id not in sizes:
            total += size
            sizes[object_id] = size
        if total > MAX_COMMITTED_SOURCE_BYTES:
            raise VerificationError("committed_source_bytes_too_large")
        entries[name] = (int(mode, 8), object_id)
    contents = _git(root, "cat-file", "--batch", input_bytes=b"".join(key + b"\n" for key in sizes))
    digests: dict[bytes, str] = {}
    offset = 0
    for object_id, size in sizes.items():
        header = object_id + b" blob " + str(size).encode("ascii") + b"\n"
        if contents[offset : offset + len(header)] != header:
            raise VerificationError("committed_source_blob_invalid")
        offset += len(header)
        end = offset + size
        if contents[end : end + 1] != b"\n":
            raise VerificationError("committed_source_blob_invalid")
        digests[object_id] = hashlib.sha256(contents[offset:end]).hexdigest()
        offset = end + 1
    if offset != len(contents):
        raise VerificationError("committed_source_blob_invalid")
    return {
        name: SourceFile("symlink" if mode == 0o120000 else "file", mode, digests[object_id])
        for name, (mode, object_id) in entries.items()
    }


def require_matching_commit(root: Path, ref: str, source: Mapping[str, SourceFile]) -> None:
    actual = {
        name: SourceFile(
            item.kind,
            0o120000
            if item.kind == "symlink"
            else (0o100755 if item.mode & stat.S_IXUSR else 0o100644),
            item.digest,
        )
        for name, item in source.items()
    }
    if actual != committed_snapshot(root, ref):
        raise VerificationError("acceptance_requires_matching_clean_source")


def gate_environment(coverage_file: Path) -> dict[str, str]:
    environment = dict(os.environ)
    # Do not silently turn an operator's real-mode selection into a mock pass.
    allowed = {
        "APP_ENV": {"", "local", "test"},
        "AUTH_MODE": {"", "local"},
        "LLM_PROVIDER": {"", "mock"},
        "CONNECTOR_MODE": {"", "mock"},
        "ALLOW_EXTERNAL_NETWORK": {"", "0", "false", "no", "off"},
        "REAL_LLM_OPT_IN": {"", "0", "false", "no", "off"},
        "REAL_CONNECTOR_OPT_IN": {"", "0", "false", "no", "off"},
    }
    for key, value in environment.items():
        if key.upper() in allowed and value.strip().lower() not in allowed[key.upper()]:
            raise VerificationError("unsafe_verification_environment")
    for key in (
        "MAKEFLAGS",
        "MFLAGS",
        "MAKEOVERRIDES",
        "PYTEST_ADDOPTS",
        "COVERAGE_PROCESS_START",
        "COVERAGE_RCFILE",
    ):
        environment.pop(key, None)
    environment["COVERAGE_FILE"] = str(coverage_file)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # CLI flags on the launching `uv run` are not inherited by nested Make gates.
    environment["UV_OFFLINE"] = "1"
    environment["UV_FROZEN"] = "1"
    return environment


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    else:
        # A failed shell/Make process may have left descendants in its private
        # session. They must not survive just because the direct child exited.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def execute_gate(gate: Gate, root: Path, environment: Mapping[str, str]) -> GateResult:
    started = time.monotonic()
    status, returncode = "failed", None
    try:
        process = subprocess.Popen(gate.argv, cwd=root, env=environment, start_new_session=True)
    except OSError:
        status = "command_unavailable"
    else:
        try:
            returncode = process.wait(timeout=gate.timeout_seconds)
            status = "passed" if returncode == 0 else "failed"
            if returncode != 0:
                _terminate(process)
        except subprocess.TimeoutExpired:
            status = "timed_out"
            _terminate(process)
            returncode = process.returncode
        except (KeyboardInterrupt, VerificationInterrupted):
            status = "interrupted"
            _terminate(process)
            returncode = process.returncode
    return GateResult(
        gate.name, status, returncode, max(0, round((time.monotonic() - started) * 1000))
    )


@contextmanager
def _signals() -> Iterator[None]:
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

    def interrupt(_number: int, _frame: Any) -> None:
        raise VerificationInterrupted("verification_interrupted")

    try:
        for number in previous:
            signal.signal(number, interrupt)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


GateExecutor = Callable[[Gate, Path, Mapping[str, str]], GateResult]


def verify_local(
    root: Path,
    *,
    acceptance: bool = False,
    backend: bool = False,
    ref: str = "HEAD",
    executor: GateExecutor | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    completed: list[GateResult] = []
    error_code: str | None = None
    changed_count = 0
    coverage_directory: Path | None = None
    baseline = source_snapshot(root)
    run = execute_gate if executor is None else executor
    try:
        if acceptance and backend:
            raise VerificationError("verification_modes_mutually_exclusive")
        resolved_ref = _resolve_ref(root, ref) if acceptance else ref
        if acceptance:
            # Local gates and the committed clean export must test the same
            # inputs. Ordinary verify still preserves/accepts initial caller edits.
            require_matching_commit(root, resolved_ref, baseline)
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root.is_relative_to(root.resolve()):
            raise VerificationError("coverage_storage_must_be_outside_source")
        coverage_directory = Path(
            tempfile.mkdtemp(prefix="marketing-agents-del07-", dir=temporary_root)
        )
        coverage_report = coverage_directory / "coverage.json"
        environment = gate_environment(coverage_directory / ".coverage")
        print(json.dumps({"artifact_directory": str(coverage_directory)}), flush=True)
        with _signals():
            selected = gates(
                coverage_report, acceptance=acceptance, backend=backend, ref=resolved_ref
            )
            if not selected:
                raise VerificationError("verification_gate_inventory_empty")
            for gate in selected:
                print(
                    json.dumps(
                        {
                            "gate": gate.name,
                            "status": "started",
                            "timeout_seconds": gate.timeout_seconds,
                        }
                    ),
                    flush=True,
                )
                result = run(gate, root, environment)
                completed.append(result)
                print(json.dumps(asdict(result), sort_keys=True), flush=True)
                if result.status != "passed" or result.returncode != 0:
                    error_code = "gate_failed"
                    break
    except (KeyboardInterrupt, VerificationInterrupted):
        error_code = "verification_interrupted"
    except VerificationError as error:
        error_code = str(error)
    except OSError:
        error_code = "verification_storage_or_process_error"
    finally:
        try:
            after = source_snapshot(root)
            changed_count = sum(
                baseline.get(name) != after.get(name) for name in baseline.keys() | after.keys()
            )
            if changed_count:
                error_code = "source_drift_detected"
        except VerificationError:
            error_code = "source_snapshot_failed_after_gates"
    summary = {
        "valid": error_code is None,
        "error": error_code,
        "mode": "local-plus-acceptance" if acceptance else ("backend" if backend else "local"),
        "completed_gates": len(completed),
        "passed_gates": sum(
            result.status == "passed" and result.returncode == 0 for result in completed
        ),
        "source_files": len(baseline),
        "changed_source_files": changed_count,
        "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
    }
    if coverage_directory is not None:
        summary["artifact_directory"] = str(coverage_directory)
        try:
            (coverage_directory / "verification.json").write_text(
                json.dumps(
                    {"summary": summary, "gates": [asdict(result) for result in completed]},
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            summary["valid"] = False
            summary["error"] = "verification_metadata_write_failed"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list gates without running commands")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--acceptance",
        action="store_true",
        help="also run committed clean-startup and scoped Compose-backup gates",
    )
    mode.add_argument(
        "--backend", action="store_true", help="run only catalog, backend static/tests and coverage"
    )
    parser.add_argument("--ref", help="committed Git ref for --acceptance (default: HEAD)")
    args = parser.parse_args(argv)
    if args.ref is not None and not args.acceptance:
        parser.error("--ref requires --acceptance")
    if args.list:
        print(
            json.dumps(
                {
                    "gates": [
                        asdict(gate)
                        for gate in gates(
                            Path("<fresh-coverage-report>"),
                            acceptance=args.acceptance,
                            backend=args.backend,
                            ref="<resolved-commit>",
                        )
                    ]
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        result = verify_local(
            ROOT, acceptance=args.acceptance, backend=args.backend, ref=args.ref or "HEAD"
        )
    except VerificationError as error:
        result = {"valid": False, "error": str(error)}
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
