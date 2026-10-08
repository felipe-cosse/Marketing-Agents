"""AC-16 source-bound quality records; stdlib fixtures never run product gates."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import ac_16_quality as quality
from scripts import verify_local as runner
from scripts.check_safety_coverage import SafetyCoverageError


def reviewed_gates(coverage: Path) -> tuple[runner.Gate, ...]:
    """Independent fixture: changing either production inventory cannot bless itself."""

    def make(name: str, target: str, timeout: int = 300) -> runner.Gate:
        return runner.Gate(name, ("make", "-j1", f"PYTHON={sys.executable}", target), timeout)

    return (
        make("catalog-validate", "catalog-validate"),
        make("catalog-release", "verify-catalog-release"),
        runner.Gate(
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
        runner.Gate(
            "api-contract-tests", ("node", "--test", "tools/api-contract/generate.test.mjs"), 300
        ),
        runner.Gate(
            "browser-inventory-tests",
            ("node", "--test", "tests/network/web_e2e_inventory.test.mjs"),
            300,
        ),
        runner.Gate(
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
                f"--cov-report=json:{coverage}",
            ),
            1800,
        ),
        runner.Gate(
            "safety-coverage",
            (sys.executable, "-m", "scripts.check_safety_coverage", "--report", str(coverage)),
            60,
        ),
        make("web-format", "web-format-check"),
        make("web-lint", "web-lint"),
        make("web-types", "web-typecheck", 600),
        make("web-unit", "web-test", 1800),
        make("web-build", "web-build", 600),
        runner.Gate(
            "browser-network-canary",
            ("node", "apps/web/scripts/run-browser-network-canary.mjs"),
            300,
        ),
        make("web-browser", "web-test-e2e", 1800),
    )


class QualityRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="marketing-agents-ac16-test-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name).resolve()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    key: os.environ[key]
                    for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
                    if key in os.environ
                },
                clear=True,
            )
        )
        self.root = self.temporary / "source"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "core.filemode", "true")
        # AC-15: checkout reflogs need local identity without hostname/DNS fallback.
        self.git("config", "--local", "user.name", "Fixture")
        self.git("config", "--local", "user.email", "fixture@example.invalid")
        for relative, content in {
            "source.txt": "original source\n",
            ".gitignore": "*.ignored\n",
            "uv.lock": "python fixture lock\n",
            "pnpm-lock.yaml": "node fixture lock\n",
            "package.json": '{"name":"fixture"}\n',
            "apps/web/package.json": '{"name":"fixture-web"}\n',
        }.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.commit()
        self.identity = quality.source_identity(self.root)
        self.store = self.temporary / "store"
        self.artifacts = self.temporary / "artifacts"
        self.artifacts.mkdir()
        self.enterContext(patch.object(quality, "STORE", self.store))
        self.enterContext(
            patch.object(runner.tempfile, "gettempdir", return_value=str(self.artifacts))
        )
        self.coverage = {"valid": True, "modules": 17, "statements": 2741, "branches": 1056}
        self.tools = {
            "python": "3.12.14",
            "node": "v24.20.0",
            "pnpm": "11.24.0",
            "uv": "uv 0.10.7",
            "platform": "Test/fixture",
        }
        self.commit_serial = 0

    def git(self, *arguments: str) -> str:
        # Only this owned temporary repository is a Git mutation target.
        process = subprocess.run(
            ("git", *arguments),
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        )
        return process.stdout.strip()

    def commit(self, *, empty: bool = False) -> None:
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "quality fixture",
            *(("--allow-empty",) if empty else ()),
        )

    def commit_tree(self, *parents: str) -> str:
        self.commit_serial += 1
        arguments = []
        for parent in parents:
            arguments.extend(("-p", parent))
        return self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit-tree",
            self.identity["tree"],
            *arguments,
            "-m",
            f"merge fixture {self.commit_serial}",
        )

    def fixture_report(self) -> tuple[dict, dict]:
        started = time.time_ns()
        directory = Path(tempfile.mkdtemp(prefix="report-", dir=self.artifacts))
        observed = [
            {"name": gate.name, "status": "passed", "returncode": 0, "elapsed_ms": 1}
            for gate in reviewed_gates(directory / "coverage.json")
        ]
        saved = {
            "summary": {
                "valid": True,
                "mode": "local",
                "error": None,
                "completed_gates": 20,
                "passed_gates": 20,
                "source_files": self.identity["files"],
                "changed_source_files": 0,
                "elapsed_ms": 20,
                "artifact_directory": str(directory),
            },
            "gates": copy.deepcopy(observed),
        }
        plan = []
        for gate in reviewed_gates(directory / "coverage.json"):
            argv = [
                "<python>"
                if item == sys.executable
                else "PYTHON=<python>"
                if item == f"PYTHON={sys.executable}"
                else item.replace(str(directory / "coverage.json"), "<coverage-report>")
                for item in gate.argv
            ]
            plan.append({"name": gate.name, "argv": argv, "timeout_seconds": gate.timeout_seconds})
        report = {
            "schema_version": 1,
            "valid": True,
            "trust": quality.TRUST,
            "source": copy.deepcopy(self.identity),
            "started_ns": started,
            "finished_ns": started,
            "tools": self.tools.copy(),
            "plan": plan,
            "observed": observed,
            "artifact_directory": str(directory),
            "artifacts": {},
            "coverage": self.coverage.copy(),
        }
        (directory / ".coverage").write_bytes(
            b"synthetic coverage database, never product evidence"
        )
        (directory / "coverage.json").write_text('{"fixture":true}\n')
        self.refresh(report, saved)
        return report, saved

    def refresh(self, report: dict, saved: dict | None = None) -> None:
        directory = Path(report["artifact_directory"])
        if saved is not None:
            (directory / "verification.json").write_text(json.dumps(saved))
        report["artifacts"] = {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("verification.json", "coverage.json", ".coverage")
        }
        report["finished_ns"] = time.time_ns()

    def validate(self, report: dict) -> None:
        with patch.object(quality, "check_coverage", return_value=self.coverage) as checked:
            quality.validate_report(report, self.identity, self.root)
        checked.assert_called_once_with(
            Path(report["artifact_directory"]) / "coverage.json",
            root=self.root,
            policy_path=self.root / "docs/verification/safety-coverage.json",
        )

    def reject(self, report: dict, pattern: str) -> None:
        with (
            patch.object(quality, "check_coverage", return_value=self.coverage),
            self.assertRaisesRegex(quality.QualityError, pattern),
        ):
            quality.validate_report(report, self.identity, self.root)

    def publish_fixture(self, report: dict) -> Path:
        self.store.mkdir(exist_ok=True)
        target = self.store / (self.identity["tree"] + ".json")
        target.write_text(json.dumps(report))
        return target

    @contextlib.contextmanager
    def simulated_run(self, executor=None):
        def execute(gate, root, environment):
            self.assertEqual(root, self.root)
            directory = Path(environment["COVERAGE_FILE"]).parent
            if gate.name == "catalog-tests":
                Path(environment["COVERAGE_FILE"]).write_bytes(b"fresh fixture coverage")
            if gate.name == "backend-tests":
                (directory / "coverage.json").write_text('{"fixture":true}\n')
            return runner.GateResult(gate.name, "passed", 0, 1)

        with (
            patch.object(quality, "_tools", return_value=self.tools),
            patch.object(quality, "check_coverage", return_value=self.coverage),
            patch.object(runner, "execute_gate", side_effect=executor or execute) as executed,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            yield executed

    def test_independent_twenty_gate_contract_matches_current_full_aggregate(self) -> None:
        coverage = self.artifacts / "coverage.json"
        expected = reviewed_gates(coverage)
        self.assertEqual(20, len(expected))
        self.assertEqual(expected, quality.expected_gates(coverage))
        self.assertEqual(expected, runner.gates(coverage))
        with patch.object(runner, "gates", return_value=()):
            self.assertEqual(expected, quality.expected_gates(coverage))

    def test_owned_git_fixture_persists_identity_for_checkout_reflogs(self) -> None:
        self.assertEqual("Fixture", self.git("config", "--local", "--get", "user.name"))
        self.assertEqual(
            "fixture@example.invalid", self.git("config", "--local", "--get", "user.email")
        )
        child = self.commit_tree(self.identity["commit"])
        self.git("checkout", "--detach", "-q", child)
        self.assertEqual(child, self.git("rev-parse", "HEAD"))
        self.assertEqual(
            ["Fixture", "fixture@example.invalid"],
            self.git("reflog", "show", "-1", "--format=%gn%n%ge", "HEAD").splitlines(),
        )
        self.assertTrue(
            self.git("reflog", "show", "-1", "--format=%gs", "HEAD").startswith(
                "checkout: moving from "
            )
        )

    def test_source_identity_binds_commit_tree_file_bytes_modes_and_locks(self) -> None:
        self.assertEqual(self.git("rev-parse", "HEAD"), self.identity["commit"])
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), self.identity["tree"])
        self.assertEqual(6, self.identity["files"])
        self.assertEqual(
            {
                name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in ("uv.lock", "pnpm-lock.yaml", "package.json", "apps/web/package.json")
            },
            self.identity["locks"],
        )
        self.assertEqual(self.identity, quality.source_identity(self.root))
        (self.root / "scratch.ignored").write_text("ignored build artifact")
        self.assertEqual(self.identity, quality.source_identity(self.root))

    def test_dirty_initial_bytes_rejected_even_when_index_assumes_unchanged(self) -> None:
        self.git("update-index", "--assume-unchanged", "source.txt")
        (self.root / "source.txt").write_text("hidden dirty bytes\n")
        self.assertEqual("", self.git("status", "--porcelain"))
        with self.assertRaises(runner.VerificationError):
            quality.source_identity(self.root)

    def test_untracked_missing_executable_and_symlink_source_drift_are_rejected(self) -> None:
        path = self.root / "source.txt"
        original = path.read_bytes()
        original_mode = path.stat().st_mode
        mutations = (
            lambda: (self.root / "untracked.txt").write_text("untracked"),
            path.unlink,
            lambda: path.chmod(0o755),
            lambda: (path.unlink(), path.symlink_to(self.temporary / "outside")),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                mutate()
                with self.assertRaises(runner.VerificationError):
                    quality.source_identity(self.root)
                (self.root / "untracked.txt").unlink(missing_ok=True)
                if path.is_symlink():
                    path.unlink()
                path.write_bytes(original)
                path.chmod(original_mode)

    def test_missing_committed_lock_cannot_be_qualified(self) -> None:
        (self.root / "uv.lock").unlink()
        self.commit()
        with self.assertRaisesRegex(quality.QualityError, "source_locks_missing"):
            quality.source_identity(self.root)

    def test_complete_fixture_validates_and_audits_from_tree_key(self) -> None:
        report, _ = self.fixture_report()
        self.validate(report)
        self.publish_fixture(report)
        with patch.object(quality, "check_coverage", return_value=self.coverage):
            self.assertEqual(report, quality.audit(self.root))

    def test_exact_same_tree_second_parent_feature_merge_consumes_feature_evidence(self) -> None:
        report, _ = self.fixture_report()
        self.publish_fixture(report)
        base = self.commit_tree()
        merge = self.commit_tree(base, self.identity["commit"])
        self.git("checkout", "--detach", "-q", merge)
        current = quality.source_identity(self.root)
        self.assertNotEqual(self.identity["commit"], current["commit"])
        self.assertEqual(self.identity["tree"], current["tree"])
        with patch.object(quality, "check_coverage", return_value=self.coverage):
            self.assertEqual(report, quality.audit(self.root))

    def test_same_tree_unrelated_first_parent_and_octopus_commits_are_rejected(self) -> None:
        report, _ = self.fixture_report()
        self.publish_fixture(report)
        first = self.commit_tree()
        second = self.commit_tree()
        variants = {
            "unrelated": self.commit_tree(),
            "single_parent": self.commit_tree(self.identity["commit"]),
            "first_parent": self.commit_tree(self.identity["commit"], first),
            "octopus": self.commit_tree(first, self.identity["commit"], second),
        }
        for kind, commit in variants.items():
            with self.subTest(commit=kind):
                self.git("checkout", "--detach", "-q", commit)
                with self.assertRaisesRegex(
                    quality.QualityError, "source_commit_not_feature_or_merge"
                ):
                    quality.audit(self.root)

    def test_changed_head_tree_or_source_record_cannot_consume_old_result(self) -> None:
        report, _ = self.fixture_report()
        self.publish_fixture(report)
        altered = copy.deepcopy(report)
        altered["source"]["tree"] = "0" * 40
        self.reject(altered, "source_identity_mismatch")
        (self.root / "source.txt").write_text("different committed product\n")
        self.commit()
        with self.assertRaises(quality.QualityError):
            quality.audit(self.root)
        with self.assertRaisesRegex(quality.QualityError, "source_identity_mismatch"):
            quality.validate_report(report, quality.source_identity(self.root), self.root)

    def test_source_mutation_during_audit_is_not_silently_accepted(self) -> None:
        report, _ = self.fixture_report()
        self.publish_fixture(report)
        changed = {**self.identity, "commit": "a" * 40}
        with (
            patch.object(quality, "source_identity", side_effect=(self.identity, changed)),
            patch.object(quality, "check_coverage", return_value=self.coverage),
            self.assertRaisesRegex(quality.QualityError, "source_changed_during_audit"),
        ):
            quality.audit(self.root)

    def test_record_schema_success_times_and_tool_types_fail_closed(self) -> None:
        mutations = (
            ("schema_version", True, "record_schema"),
            ("valid", 1, "record_not_successful"),
            ("trust", "authenticated", "record_not_successful"),
            ("started_ns", True, "record_times"),
            ("finished_ns", 0, "record_times"),
            ("tools", {**self.tools, "node": "v24\nsecret"}, "tool_record_invalid"),
        )
        for field, value, code in mutations:
            with self.subTest(field=field):
                report, _ = self.fixture_report()
                report[field] = value
                self.reject(report, code)
        report, _ = self.fixture_report()
        report["extra"] = "unsupported"
        self.reject(report, "record_schema")

    def test_plan_rejects_omission_reorder_duplicate_filtered_arguments_and_timeout(self) -> None:
        for mutation in ("omitted", "reordered", "duplicate", "filtered", "timeout"):
            with self.subTest(mutation=mutation):
                report, _ = self.fixture_report()
                plan = report["plan"]
                if mutation == "omitted":
                    plan.pop()
                elif mutation == "reordered":
                    plan[0], plan[1] = plan[1], plan[0]
                elif mutation == "duplicate":
                    plan[-1] = plan[0]
                elif mutation == "filtered":
                    plan[11]["argv"].append("-k=only_one")
                else:
                    plan[-1]["timeout_seconds"] = 1801
                self.reject(report, "gate_plan_mismatch")

    def test_observed_inventory_rejects_omitted_reordered_duplicate_failed_and_bool_results(
        self,
    ) -> None:
        for mutation in ("omitted", "reordered", "duplicate", "failed", "bool_code", "bool_time"):
            with self.subTest(mutation=mutation):
                report, saved = self.fixture_report()
                observed = report["observed"]
                if mutation == "omitted":
                    observed.pop()
                elif mutation == "reordered":
                    observed[0], observed[1] = observed[1], observed[0]
                elif mutation == "duplicate":
                    observed[-1] = observed[0]
                elif mutation == "failed":
                    observed[-1]["status"] = "failed"
                elif mutation == "bool_code":
                    observed[-1]["returncode"] = False
                else:
                    observed[-1]["elapsed_ms"] = True
                saved["gates"] = copy.deepcopy(observed)
                self.refresh(report, saved)
                self.reject(report, "observed_(inventory|gate_failure)")

    def test_summary_rejects_partial_modes_false_success_and_boolean_counters(self) -> None:
        for field, value in (
            ("mode", "backend"),
            ("mode", "local-plus-acceptance"),
            ("valid", 1),
            ("error", "gate_failed"),
            ("completed_gates", 19),
            ("passed_gates", 19),
            ("changed_source_files", 1),
            ("completed_gates", True),
            ("passed_gates", True),
            ("changed_source_files", False),
            ("source_files", True),
            ("elapsed_ms", False),
        ):
            with self.subTest(field=field, value=value):
                report, saved = self.fixture_report()
                saved["summary"][field] = value
                self.refresh(report, saved)
                self.reject(report, "incomplete_quality_run")

    def test_saved_observations_must_equal_external_record(self) -> None:
        report, saved = self.fixture_report()
        saved["gates"][-1]["elapsed_ms"] += 1
        self.refresh(report, saved)
        self.reject(report, "saved_result_mismatch")

    def test_each_artifact_is_required_regular_nonempty_fresh_and_hash_bound(self) -> None:
        for name in ("verification.json", "coverage.json", ".coverage"):
            for mutation in ("missing", "tampered", "empty", "stale", "symlink"):
                with self.subTest(artifact=name, mutation=mutation):
                    report, _ = self.fixture_report()
                    target = Path(report["artifact_directory"]) / name
                    if mutation == "missing":
                        target.unlink()
                    elif mutation == "tampered":
                        target.write_bytes(target.read_bytes() + b"tampered")
                    elif mutation == "empty":
                        target.write_bytes(b"")
                    elif mutation == "stale":
                        os.utime(target, ns=(1, 1))
                    else:
                        original = target.with_suffix(".original")
                        target.rename(original)
                        target.symlink_to(original)
                    self.reject(report, "artifact_")

    def test_artifacts_cannot_be_taken_from_source_tree_or_symlink_directory(self) -> None:
        report, _ = self.fixture_report()
        report["artifact_directory"] = str(self.root)
        self.reject(report, "artifact_directory_invalid")
        report, _ = self.fixture_report()
        link = self.temporary / "linked-artifacts"
        link.symlink_to(Path(report["artifact_directory"]), target_is_directory=True)
        report["artifact_directory"] = str(link)
        self.reject(report, "artifact_directory_invalid")

    def test_json_truncation_duplicates_nonfinite_and_nonobject_records_are_rejected(self) -> None:
        for raw, code in (
            ('{"valid":', "invalid_json"),
            ('{"valid":true,"valid":false}', "duplicate_json_key"),
            ('{"nested":{"count":1,"count":2}}', "duplicate_json_key"),
            ('{"count":NaN}', "nonfinite_json"),
            ('{"count":Infinity}', "nonfinite_json"),
            ("[]", "json_not_object"),
        ):
            with self.subTest(raw=raw):
                report, _ = self.fixture_report()
                target = self.publish_fixture(report)
                target.write_text(raw)
                with self.assertRaisesRegex(quality.QualityError, code):
                    quality.audit(self.root)
                saved_path = Path(report["artifact_directory"]) / "verification.json"
                saved_path.write_text(raw)
                self.refresh(report)
                self.reject(report, code)

    def test_coverage_policy_is_rechecked_and_recorded_summary_must_match(self) -> None:
        report, _ = self.fixture_report()
        report["coverage"]["modules"] = 16
        self.reject(report, "coverage_record_mismatch")
        report, _ = self.fixture_report()
        with (
            patch.object(quality, "check_coverage", side_effect=SafetyCoverageError("fixture")),
            self.assertRaises(SafetyCoverageError),
        ):
            quality.validate_report(report, self.identity, self.root)

    def test_json_reader_enforces_size_and_regular_file_boundaries(self) -> None:
        path = self.artifacts / "oversized.json"
        path.write_text(json.dumps({"content": "x" * 64}))
        with (
            patch.object(quality, "MAX_BYTES", 32),
            self.assertRaisesRegex(quality.QualityError, "artifact_type_or_size"),
        ):
            quality._read(path)
        fifo = self.artifacts / "not-a-regular-file"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(quality.QualityError, "artifact_type_or_size"):
            quality._read(fifo)

    def test_real_wrapper_observes_all_twenty_gates_and_publishes_auditable_record(self) -> None:
        with self.simulated_run() as executed:
            report = quality.run_quality(self.root)
            self.assertEqual(20, executed.call_count)
            self.assertEqual(report, quality.audit(self.root))
        self.assertEqual(20, len(report["observed"]))
        self.assertEqual(self.identity, report["source"])
        self.assertEqual(quality.TRUST, report["trust"])
        self.assertEqual(
            0o600, (self.store / (self.identity["tree"] + ".json")).stat().st_mode & 0o777
        )

    def test_actual_gate_omission_reordering_duplicate_and_filtering_invalidate_retry(self) -> None:
        original_gates = runner.gates
        for mutation in ("omitted", "reordered", "duplicate", "filtered"):
            with self.subTest(mutation=mutation):
                report, _ = self.fixture_report()
                path = self.publish_fixture(report)

                def altered(*args, mutation=mutation, **kwargs):
                    gates = list(original_gates(*args, **kwargs))
                    if mutation == "omitted":
                        gates.pop()
                    elif mutation == "reordered":
                        gates[0], gates[1] = gates[1], gates[0]
                    elif mutation == "duplicate":
                        gates[-1] = gates[0]
                    else:
                        gate = gates[11]
                        gates[11] = runner.Gate(
                            gate.name, (*gate.argv, "-k=one"), gate.timeout_seconds
                        )
                    return tuple(gates)

                with (
                    self.simulated_run(),
                    patch.object(runner, "gates", side_effect=altered),
                    self.assertRaises(quality.QualityError),
                ):
                    quality.run_quality(self.root)
                self.assertIs(False, json.loads(path.read_text())["valid"])
                with self.assertRaises(quality.QualityError):
                    quality.audit(self.root)

    def test_failed_gate_or_tool_setup_cannot_reuse_prior_pass(self) -> None:
        for failure in ("tool", "gate"):
            with self.subTest(failure=failure):
                report, _ = self.fixture_report()
                path = self.publish_fixture(report)
                with self.simulated_run(
                    lambda gate, root, env: runner.GateResult(gate.name, "failed", 5, 1)
                ):
                    manager = (
                        patch.object(
                            quality, "_tools", side_effect=quality.QualityError("tool_unavailable")
                        )
                        if failure == "tool"
                        else contextlib.nullcontext()
                    )
                    with manager, self.assertRaises(quality.QualityError):
                        quality.run_quality(self.root)
                self.assertEqual("run_in_progress", json.loads(path.read_text())["error"])
                with self.assertRaises(quality.QualityError):
                    quality.audit(self.root)

    def test_twenty_successes_without_both_coverage_artifacts_cannot_publish(self) -> None:
        for missing in ("coverage.json", ".coverage"):
            with self.subTest(missing=missing):
                report, _ = self.fixture_report()
                target = self.publish_fixture(report)

                def execute(gate, root, environment, missing=missing):
                    directory = Path(environment["COVERAGE_FILE"]).parent
                    if gate.name == "catalog-tests" and missing != ".coverage":
                        (directory / ".coverage").write_bytes(b"fixture")
                    if gate.name == "backend-tests" and missing != "coverage.json":
                        (directory / "coverage.json").write_text('{"fixture":true}\n')
                    return runner.GateResult(gate.name, "passed", 0, 1)

                with (
                    self.simulated_run(execute) as executed,
                    self.assertRaisesRegex(quality.QualityError, "artifact_unreadable"),
                ):
                    quality.run_quality(self.root)
                self.assertEqual(20, executed.call_count)
                self.assertIs(False, json.loads(target.read_text())["valid"])

    def test_command_and_tool_environment_overrides_fail_before_tools_or_gates(self) -> None:
        for key in (
            "UV",
            "MAKEFILES",
            "PYTHONPATH",
            "NODE_OPTIONS",
            "OBJ06_NATIVE_READY",
            "UV_PROJECT",
            "PNPM_HOME",
            "NPM_CONFIG_USERCONFIG",
            "PLAYWRIGHT_BROWSERS_PATH",
        ):
            with self.subTest(key=key):
                report, _ = self.fixture_report()
                path = self.publish_fixture(report)
                with (
                    patch.dict(os.environ, {key: "untrusted-fixture-value"}),
                    patch.object(quality, "_tools") as tools,
                    patch.object(runner, "verify_local") as verify,
                    self.assertRaisesRegex(
                        quality.QualityError, "unsupported_quality_environment_override"
                    ),
                ):
                    quality.run_quality(self.root)
                tools.assert_not_called()
                verify.assert_not_called()
                self.assertIs(False, json.loads(path.read_text())["valid"])

    def test_observer_strips_secrets_and_preserves_validated_mock_and_offline_flags(self) -> None:
        seen = []
        uv = self.temporary / "uv"
        uv.write_text("fixture executable identity only; never invoked\n")
        uv.chmod(0o755)

        def execute(gate, root, environment):
            seen.append(environment.copy())
            directory = Path(environment["COVERAGE_FILE"]).parent
            if gate.name == "catalog-tests":
                Path(environment["COVERAGE_FILE"]).write_bytes(b"fixture")
            if gate.name == "backend-tests":
                (directory / "coverage.json").write_text('{"fixture":true}\n')
            return runner.GateResult(gate.name, "passed", 0, 1)

        with (
            patch.dict(
                os.environ,
                {
                    "PRIVATE_TEST_SECRET": "secret-not-for-children",
                    "HTTP_PROXY": "http://proxy.invalid",
                    "PATH": str(self.temporary) + os.pathsep + os.environ["PATH"],
                    "UV": str(uv),
                    "UV_RUN_RECURSION_DEPTH": "1",
                    "UV_CACHE_DIR": str(self.artifacts / "uv-cache"),
                    "APP_ENV": "local",
                    "AUTH_MODE": "local",
                    "LLM_PROVIDER": "mock",
                    "CONNECTOR_MODE": "mock",
                    "ALLOW_EXTERNAL_NETWORK": "false",
                    "REAL_LLM_OPT_IN": "false",
                    "REAL_CONNECTOR_OPT_IN": "false",
                },
            ),
            self.simulated_run(execute),
        ):
            report = quality.run_quality(self.root)
        self.assertEqual(20, len(seen))
        for environment in seen:
            self.assertNotIn("PRIVATE_TEST_SECRET", environment)
            self.assertNotIn("HTTP_PROXY", environment)
            self.assertNotIn("UV", environment)
            self.assertNotIn("UV_RUN_RECURSION_DEPTH", environment)
            self.assertEqual("mock", environment["LLM_PROVIDER"])
            self.assertEqual("mock", environment["CONNECTOR_MODE"])
            self.assertEqual("false", environment["ALLOW_EXTERNAL_NETWORK"])
            self.assertEqual("1", environment["UV_OFFLINE"])
            self.assertEqual("1", environment["UV_FROZEN"])
            self.assertEqual(str(self.artifacts / "uv-cache"), environment["UV_CACHE_DIR"])
        self.assertNotIn("secret-not-for-children", json.dumps(report))

    def test_uv_launch_metadata_accepts_only_matching_executable_and_bounded_depth(self) -> None:
        uv = self.temporary / "uv"
        uv.write_text("fixture executable identity only; never invoked\n")
        uv.chmod(0o755)
        alias = self.temporary / "uv-alias"
        alias.symlink_to(uv)
        with patch.dict(
            os.environ,
            {
                "PATH": str(self.temporary) + os.pathsep + os.environ["PATH"],
                "UV": str(uv),
                "UV_RUN_RECURSION_DEPTH": "1",
            },
        ):
            for executable in (uv, alias):
                for depth in range(1, 9):
                    with (
                        self.subTest(executable=executable.name, depth=depth),
                        patch.dict(
                            os.environ,
                            {"UV": str(executable), "UV_RUN_RECURSION_DEPTH": str(depth)},
                        ),
                    ):
                        quality._environment_preflight()
            for value in ("uv", str(self.temporary / "other-uv")):
                with (
                    self.subTest(alternate_uv=value),
                    patch.dict(os.environ, {"UV": value}),
                    self.assertRaisesRegex(
                        quality.QualityError, "unsupported_quality_environment_override"
                    ),
                ):
                    quality._environment_preflight()
            for value in ("", "0", "9", "-1", "true", "01", "1.0", "1 "):
                with (
                    self.subTest(depth=value),
                    patch.dict(os.environ, {"UV_RUN_RECURSION_DEPTH": value}),
                    self.assertRaisesRegex(
                        quality.QualityError, "unsupported_quality_environment_override"
                    ),
                ):
                    quality._environment_preflight()

    def test_head_change_during_gate_execution_prevents_publication(self) -> None:
        def changed(gate, root, environment):
            self.commit(empty=True)
            return runner.GateResult(gate.name, "failed", 5, 1)

        with (
            self.simulated_run(changed),
            self.assertRaisesRegex(quality.QualityError, "source_changed_during_run"),
        ):
            quality.run_quality(self.root)

    def test_cli_has_no_partial_mode_report_path_or_executor_injection(self) -> None:
        for option in ("--backend", "--acceptance", "--report=fixture", "--executor=fixture"):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    quality.main(["run", option])
                self.assertEqual(2, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
