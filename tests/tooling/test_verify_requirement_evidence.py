import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_requirement_evidence.py"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_requirement_evidence", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load requirement evidence verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


class SyntheticRepository:
    """Small Git histories used to test requirement DEL-08 topology enforcement."""

    statement = "Implement a synthetic control."

    def __init__(self, root: Path, *, baseline_assets: bool = False, protected_carriers=None) -> None:
        self.root = root
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Evidence Tests")
        self.git("config", "user.email", "evidence@example.invalid")
        (root / "docs" / "implementation-plan").mkdir(parents=True)
        (root / "docs" / "verification").mkdir(parents=True)
        matrix = (
            "# Matrix\n\n"
            "| ID | Requirement | Status |\n"
            "|---|---|---|\n"
            f"| TEST-01 | {self.statement} | planned |\n"
        )
        (root / MODULE.MATRIX_PATH).write_text(matrix, encoding="utf-8")
        policy = {
            "schema_version": 1,
            "baseline_commit": "baseline",
            "legacy_bootstrap_ids": [],
            "legacy_manifests_added_by": "DEL-08",
            "strict_protocol_starts_at": "TEST-01",
            "protected_carrier_ids": protected_carriers or [],
            "protected_path_globs": ["scripts/verify_requirement_evidence.py"],
            "evidence_only_path_globs": [
                "docs/verification/requirements/**",
                "docs/implementation-plan/16-requirements-traceability-matrix.md",
            ],
            "category_classes": {"TEST": "product"},
            "class_defaults": {
                "product": {
                    "minimum_claims": 2,
                    "minimum_gates": 1,
                    "required_group_kinds": ["implementation", "test"],
                    "connection_witness": False,
                }
            },
            "requirement_overrides": {},
        }
        (root / MODULE.POLICY_PATH).write_text(json.dumps(policy), encoding="utf-8")
        if baseline_assets:
            self._write_control_and_test("disabled")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        self.git("tag", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args: str, check: bool = True, env=None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=check,
            capture_output=True,
            text=True,
            env=env,
        )

    def _write_control_and_test(self, control: str = "enabled", *, exit_code: int = 0, sleep: int = 0) -> None:
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "src" / "control.txt").write_text(control + "\n", encoding="utf-8")
        body = "# TEST-01\n"
        if sleep:
            body += f"import time\ntime.sleep({sleep})\n"
        body += f"raise SystemExit({exit_code})\n"
        (self.root / "tests" / "test_test_01.py").write_text(body, encoding="utf-8")

    def manifest(self, **changes):
        digest = hashlib.sha256(self.statement.encode("utf-8")).hexdigest()
        value = {
            "$schema": "../schema/requirement-evidence.schema.json",
            "schema_version": 1,
            "requirement_id": "TEST-01",
            "matrix_statement_sha256": digest,
            "evidence_class": "product",
            "scope": {
                "allowed_path_globs": [
                    "src/**",
                    "tests/**",
                    "scripts/**",
                    "docs/verification/requirements/TEST-01.json",
                ],
                "required_path_groups": [
                    {"kind": "implementation", "one_of": ["src/**", "scripts/**"]},
                    {"kind": "test", "one_of": ["tests/test_test_01.py"]},
                ],
            },
            "claims": [
                {
                    "id": "control-enabled",
                    "type": "positive",
                    "statement": "The synthetic implementation enables the declared control.",
                    "test_refs": ["tests/test_test_01.py::TEST-01"],
                },
                {
                    "id": "control-regression",
                    "type": "negative_control",
                    "statement": "The synthetic test is retained as a regression control.",
                    "test_refs": ["tests/test_test_01.py::TEST-01"],
                },
            ],
            "gates": [
                {
                    "id": "test-01-gate",
                    "runner": "argv",
                    "argv": ["python3", "tests/test_test_01.py"],
                    "cwd": ".",
                    "timeout_seconds": 5,
                    "network_requirement": "not_enforced",
                    "covers": ["control-enabled", "control-regression"],
                }
            ],
            "limitations": [],
        }
        value.update(changes)
        return value

    def write_manifest(self, value) -> None:
        path = self.root / MODULE.MANIFEST_ROOT / "TEST-01.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def feature(self, *, subject="[TEST-01] implement synthetic control", manifest=None, write_assets=True, exit_code=0, sleep=0, branch="req/test-01-control") -> str:
        self.git("switch", "-c", branch)
        if write_assets:
            self._write_control_and_test("enabled", exit_code=exit_code, sleep=sleep)
        self.write_manifest(manifest or self.manifest())
        self.git("add", ".")
        self.git("commit", "-m", subject)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def merge(self, branch="req/test-01-control", *, subject="merge: TEST-01 synthetic control") -> str:
        self.git("switch", "main")
        self.git("merge", "--no-ff", branch, "-m", subject)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def land_valid(self, **feature_options) -> None:
        self.feature(**feature_options)
        self.merge(branch=feature_options.get("branch", "req/test-01-control"))


class RequirementEvidenceToolTests(unittest.TestCase):
    """Requirement DEL-08: history/evidence tooling rejects cosmetic or forged branches."""

    def make_repo(self, *, baseline_assets=False, protected_carriers=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = SyntheticRepository(Path(temporary.name), baseline_assets=baseline_assets, protected_carriers=protected_carriers)
        return repo

    def validate(self, repo, *, run_all=False, run_witness=False):
        return MODULE.validate_history(
            repo.root,
            "main",
            allow_incomplete=False,
            check_branches=True,
            run_all=run_all,
            run_latest=False,
            run_witness=run_witness,
        )

    def assert_invalid(self, repo, message=None, **options):
        with self.assertRaises(MODULE.EvidenceError) as caught:
            self.validate(repo, **options)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_del_08_make_targets_run_real_nonzero_test_commands(self) -> None:
        text = (ROOT / "Makefile").read_text(encoding="utf-8")
        for target in ("test-tooling:", "verify-history:", "verify-requirement:"):
            self.assertIn(target, text)
        self.assertIn("tests.tooling.test_verify_requirement_evidence", text)
        self.assertIn("scripts/verify_requirement_evidence.py history", text)
        self.assertIn("scripts/verify_requirement_evidence.py branch", text)
        self.assertNotRegex(text, r"(?m)^\t(?:echo|true)(?:\s|$)")

    def test_del_08_accepts_one_direct_feature_commit_and_two_parent_merge(self) -> None:
        repo = self.make_repo()
        repo.land_valid()
        result = self.validate(repo, run_all=True)
        self.assertEqual(["TEST-01"], result.completed)
        self.assertEqual(1, len(result.results[0].gate_results))

    def test_del_08_rejects_one_parent_fake_merge_subject(self) -> None:
        repo = self.make_repo()
        repo.git("commit", "--allow-empty", "-m", "merge: TEST-01 fake")
        self.assert_invalid(repo, "exactly two parents")

    def test_del_08_rejects_wrong_second_parent_subject(self) -> None:
        repo = self.make_repo()
        repo.feature(subject="[TEST-02] wrong requirement")
        repo.merge()
        self.assert_invalid(repo, "feature subject")

    def test_del_08_rejects_multiple_feature_commits(self) -> None:
        repo = self.make_repo()
        repo.feature()
        (repo.root / "src" / "second.txt").write_text("second\n", encoding="utf-8")
        repo.git("add", ".")
        repo.git("commit", "-m", "extra feature commit")
        repo.merge()
        self.assert_invalid(repo, "one direct")

    def test_del_08_rejects_empty_feature_tree(self) -> None:
        repo = self.make_repo(baseline_assets=True)
        repo.git("switch", "-c", "req/test-01-control")
        repo.git("commit", "--allow-empty", "-m", "[TEST-01] empty control")
        repo.merge()
        self.assert_invalid(repo, "empty feature")

    def test_del_08_rejects_merge_only_payload(self) -> None:
        repo = self.make_repo()
        feature = repo.feature()
        (repo.root / "merge-only.txt").write_text("forbidden\n", encoding="utf-8")
        repo.git("add", "merge-only.txt")
        tree = repo.git("write-tree").stdout.strip()
        repo.git("reset", "--hard", feature)
        environment = dict(os.environ)
        environment.update({"GIT_AUTHOR_NAME": "Evidence Tests", "GIT_AUTHOR_EMAIL": "evidence@example.invalid", "GIT_COMMITTER_NAME": "Evidence Tests", "GIT_COMMITTER_EMAIL": "evidence@example.invalid"})
        manual = repo.git("commit-tree", tree, "-p", repo.baseline, "-p", feature, "-m", "merge: TEST-01 manual", env=environment).stdout.strip()
        repo.git("update-ref", "refs/heads/main", manual)
        self.assert_invalid(repo, "merge tree differs")

    def test_del_08_rejects_evidence_only_delta(self) -> None:
        repo = self.make_repo(baseline_assets=True)
        repo.feature(write_assets=False)
        repo.merge()
        self.assert_invalid(repo, "evidence-only")

    def test_del_08_rejects_manifest_id_or_statement_digest_mismatch(self) -> None:
        repo = self.make_repo()
        manifest = repo.manifest(matrix_statement_sha256="0" * 64)
        repo.land_valid(manifest=manifest)
        self.assert_invalid(repo, "digest mismatch")

    def test_del_08_rejects_uncovered_claim(self) -> None:
        repo = self.make_repo()
        manifest = repo.manifest()
        manifest["claims"].append({"id": "uncovered", "type": "contract", "statement": "This valid-looking claim has no executable gate coverage.", "test_refs": ["tests/test_test_01.py::TEST-01"]})
        repo.land_valid(manifest=manifest)
        self.assert_invalid(repo, "uncovered claims")

    def test_del_08_rejects_noop_or_shell_gate(self) -> None:
        repo = self.make_repo()
        manifest = repo.manifest()
        manifest["gates"][0]["argv"] = ["true", "ignored"]
        repo.land_valid(manifest=manifest)
        self.assert_invalid(repo, "forbidden executable")

    def test_del_08_rejects_failing_and_timeout_gates(self) -> None:
        failing = self.make_repo()
        failing.land_valid(exit_code=7)
        self.assert_invalid(failing, "failed with exit 7", run_all=True)

        timeout = self.make_repo()
        manifest = timeout.manifest()
        manifest["gates"][0]["timeout_seconds"] = 1
        timeout.land_valid(manifest=manifest, sleep=2)
        self.assert_invalid(timeout, "timed out", run_all=True)

    def test_del_08_rejects_unauthorized_protocol_edit(self) -> None:
        repo = self.make_repo()
        repo.git("switch", "-c", "req/test-01-control")
        repo._write_control_and_test()
        repo.write_manifest(repo.manifest())
        path = repo.root / "scripts" / "verify_requirement_evidence.py"
        path.parent.mkdir(parents=True)
        path.write_text("# TEST-01 unauthorized\n", encoding="utf-8")
        repo.git("add", ".")
        repo.git("commit", "-m", "[TEST-01] edit protected verifier")
        repo.merge()
        self.assert_invalid(repo, "unauthorized evidence-protocol change")

    def test_del_08_rejects_duplicate_and_unknown_ids(self) -> None:
        duplicate = self.make_repo()
        duplicate.land_valid()
        duplicate.git("switch", "-c", "req/test-01-second")
        (duplicate.root / "src" / "second.txt").write_text("TEST-01 second\n", encoding="utf-8")
        duplicate.write_manifest(duplicate.manifest())
        duplicate.git("add", ".")
        duplicate.git("commit", "-m", "[TEST-01] duplicate feature")
        duplicate.merge(branch="req/test-01-second")
        self.assert_invalid(duplicate, "duplicate requirement merge")

        unknown = self.make_repo()
        unknown.feature(subject="[TEST-02] unknown feature")
        unknown.merge(subject="merge: TEST-02 unknown")
        self.assert_invalid(unknown, "unknown requirement merge")

    def test_del_08_connection_witness_must_fail_when_control_is_reverted(self) -> None:
        repo = self.make_repo(baseline_assets=True, protected_carriers=["TEST-01"])
        manifest = repo.manifest()
        manifest["connection_witness"] = {"implementation_path_globs": ["src/**"], "gate_id": "test-01-gate"}
        policy_path = repo.root / MODULE.POLICY_PATH
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["class_defaults"]["product"]["connection_witness"] = True
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        repo.git("add", str(MODULE.POLICY_PATH))
        repo.git("commit", "-m", "test policy setup")
        repo.git("tag", "-f", "baseline")
        repo.baseline = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.git("switch", "-c", "req/test-01-control")
        (repo.root / "src" / "control.txt").write_text("enabled\n", encoding="utf-8")
        (repo.root / "tests" / "test_test_01.py").write_text(
            "# TEST-01\nfrom pathlib import Path\nraise SystemExit(0 if Path('src/control.txt').read_text().strip() == 'enabled' else 9)\n",
            encoding="utf-8",
        )
        repo.write_manifest(manifest)
        repo.git("add", ".")
        repo.git("commit", "-m", "[TEST-01] connect test to control")
        repo.merge()
        result = self.validate(repo, run_all=True, run_witness=True)
        self.assertEqual(9, result.results[0].witness_exit_code)


if __name__ == "__main__":
    unittest.main()
