"""CI hydrates retained branches from real origin refs, without changing history."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.tooling.test_verify_requirement_evidence import MODULE as EVIDENCE
from tests.tooling.test_verify_requirement_evidence import SyntheticRepository

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prepare_ci_history.py"


def load_module():
    spec = importlib.util.spec_from_file_location("prepare_ci_history", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load CI history preparation")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


class HistoryRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-b", "main")
        self.git("config", "user.name", "CI History Tests")
        self.git("config", "user.email", "ci-history@example.invalid")
        self.git("commit", "--allow-empty", "-m", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("commit", "--allow-empty", "-m", "feature")
        self.feature = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("update-ref", "refs/remotes/origin/main", self.feature)
        self.git("update-ref", "refs/remotes/origin/req/test-01-control", self.feature)

    def git(self, *args: str, input: str | None = None, check: bool = True):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            input=input,
            check=check,
            capture_output=True,
            text=True,
        )

    def refs(self) -> str:
        return self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

    def head(self) -> tuple[str, str]:
        return (
            self.git("rev-parse", "HEAD").stdout.strip(),
            self.git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip(),
        )


class PrepareHistoryRefsTests(unittest.TestCase):
    def make_repo(self) -> HistoryRepository:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return HistoryRepository(Path(temporary.name))

    def assert_rejected_without_changes(self, repo: HistoryRepository) -> None:
        refs = repo.refs()
        head = repo.head()
        with self.assertRaises(MODULE.HistoryRefsError):
            MODULE.prepare_history_refs(repo.root)
        self.assertEqual(refs, repo.refs())
        self.assertEqual(head, repo.head())

    def test_main_checkout_hydrates_exact_origin_tips_and_is_idempotent(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "refs/remotes/origin/req/test-02-other", repo.baseline)
        before_head = repo.head()
        dirty_file = repo.root / "untracked-user-work.txt"
        dirty_file.write_text("preserve me\n", encoding="utf-8")

        self.assertEqual(2, MODULE.prepare_history_refs(repo.root))

        self.assertEqual(
            repo.feature,
            repo.git("rev-parse", "refs/heads/req/test-01-control").stdout.strip(),
        )
        self.assertEqual(
            repo.baseline,
            repo.git("rev-parse", "refs/heads/req/test-02-other").stdout.strip(),
        )
        refs = repo.refs()
        self.assertEqual(0, MODULE.prepare_history_refs(repo.root))
        self.assertEqual(refs, repo.refs())
        self.assertEqual(before_head, repo.head())
        self.assertEqual("preserve me\n", dirty_file.read_text(encoding="utf-8"))

    def test_detached_pr_head_is_preserved_and_main_comes_only_from_origin(self) -> None:
        repo = self.make_repo()
        repo.git("checkout", "--detach", repo.baseline)
        repo.git("update-ref", "-d", "refs/heads/main")
        before_head = repo.head()

        self.assertEqual(2, MODULE.prepare_history_refs(repo.root))

        self.assertEqual(before_head, repo.head())
        self.assertEqual((repo.baseline, ""), repo.head())
        self.assertEqual(repo.feature, repo.git("rev-parse", "refs/heads/main").stdout.strip())

    def test_missing_origin_main_is_rejected(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assert_rejected_without_changes(repo)

    def test_missing_origin_requirement_refs_are_not_fabricated(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "-d", "refs/remotes/origin/req/test-01-control")
        self.assert_rejected_without_changes(repo)

    def test_other_remotes_do_not_substitute_for_origin(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "-d", "refs/remotes/origin/req/test-01-control")
        repo.git("update-ref", "refs/remotes/upstream/req/test-01-control", repo.feature)
        self.assert_rejected_without_changes(repo)

    def test_shallow_repository_is_rejected(self) -> None:
        repo = self.make_repo()
        (repo.root / ".git" / "shallow").write_text(repo.feature + "\n", encoding="ascii")
        self.assertEqual("true", repo.git("rev-parse", "--is-shallow-repository").stdout.strip())
        self.assert_rejected_without_changes(repo)

    def test_invalid_requirement_names_reject_all_pending_ref_creation(self) -> None:
        for branch in (
            "req/TEST-02-uppercase",
            "req/test-2-short-number",
            "req/test-002-long-number",
            "req/test-02",
            "req/test-02-bad_slug",
            "req/test-02-nested/name",
        ):
            with self.subTest(branch=branch):
                repo = self.make_repo()
                repo.git("checkout", "--detach")
                repo.git("update-ref", "-d", "refs/heads/main")
                repo.git("update-ref", f"refs/remotes/origin/{branch}", repo.feature)
                self.assert_rejected_without_changes(repo)

    def test_noncommit_origin_sources_are_rejected(self) -> None:
        for ref in ("refs/remotes/origin/main", "refs/remotes/origin/req/test-01-control"):
            with self.subTest(ref=ref):
                repo = self.make_repo()
                blob = repo.git(
                    "hash-object", "-w", "--stdin", input="not a commit\n"
                ).stdout.strip()
                repo.git("update-ref", ref, blob)
                self.assert_rejected_without_changes(repo)

    def test_conflicting_local_main_is_not_moved(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "refs/remotes/origin/main", repo.baseline)
        self.assert_rejected_without_changes(repo)

    def test_conflicting_local_requirement_tip_is_not_moved(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "refs/heads/req/test-01-control", repo.baseline)
        repo.git("update-ref", "refs/remotes/origin/req/test-02-other", repo.feature)
        self.assert_rejected_without_changes(repo)

    def test_orphan_local_requirement_ref_is_rejected(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "refs/heads/req/test-02-orphan", repo.feature)
        self.assert_rejected_without_changes(repo)

    def test_symbolic_source_or_local_history_refs_are_rejected(self) -> None:
        for ref in (
            "refs/remotes/origin/main",
            "refs/remotes/origin/req/test-01-control",
            "refs/heads/main",
            "refs/heads/req/test-01-control",
        ):
            with self.subTest(ref=ref):
                repo = self.make_repo()
                repo.git("update-ref", "refs/heads/reference-target", repo.feature)
                repo.git("symbolic-ref", ref, "refs/heads/reference-target")
                self.assert_rejected_without_changes(repo)

    def test_origin_source_race_aborts_the_whole_atomic_transaction(self) -> None:
        repo = self.make_repo()
        repo.git("checkout", "--detach", repo.baseline)
        repo.git("update-ref", "-d", "refs/heads/main")
        head = repo.head()
        real_run = subprocess.run
        raced = False

        def race_source(command, *args, **kwargs):
            nonlocal raced
            if "update-ref" in command and "--stdin" in command and not raced:
                raced = True
                real_run(
                    [
                        "git",
                        "update-ref",
                        "refs/remotes/origin/req/test-01-control",
                        repo.baseline,
                        repo.feature,
                    ],
                    cwd=repo.root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            return real_run(command, *args, **kwargs)

        with (
            mock.patch.object(MODULE.subprocess, "run", side_effect=race_source),
            self.assertRaises(MODULE.HistoryRefsError),
        ):
            MODULE.prepare_history_refs(repo.root)

        self.assertTrue(
            raced, "history preparation must use an atomic update-ref --stdin transaction"
        )
        self.assertEqual("", repo.git("for-each-ref", "refs/heads").stdout)
        self.assertEqual(head, repo.head())
        self.assertEqual(
            repo.baseline,
            repo.git("rev-parse", "refs/remotes/origin/req/test-01-control").stdout.strip(),
        )

    def test_symbolic_target_swap_cannot_create_an_unrelated_referent(self) -> None:
        repo = self.make_repo()
        repo.git("checkout", "--detach", repo.baseline)
        repo.git("update-ref", "-d", "refs/heads/main")
        head = repo.head()
        real_run = subprocess.run
        raced = False
        target = "refs/heads/req/test-01-control"
        unrelated = "refs/heads/unrelated-user-work"

        def race_target(command, *args, **kwargs):
            nonlocal raced
            if "update-ref" in command and "--stdin" in command and not raced:
                raced = True
                real_run(
                    ["git", "symbolic-ref", target, unrelated],
                    cwd=repo.root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            return real_run(command, *args, **kwargs)

        # Git may treat a dangling symref as absent even with --no-deref. It is
        # safe to replace that destination directly, but never its referent.
        with mock.patch.object(MODULE.subprocess, "run", side_effect=race_target):
            try:
                created = MODULE.prepare_history_refs(repo.root)
            except MODULE.HistoryRefsError:
                created = None

        self.assertTrue(raced)
        self.assertEqual(head, repo.head())
        self.assertNotEqual(0, repo.git("show-ref", "--verify", unrelated, check=False).returncode)
        if created is None:
            self.assertEqual(unrelated, repo.git("symbolic-ref", target).stdout.strip())
            self.assertNotEqual(
                0, repo.git("show-ref", "--verify", "refs/heads/main", check=False).returncode
            )
        else:
            self.assertEqual(2, created)
            self.assertNotEqual(0, repo.git("symbolic-ref", "-q", target, check=False).returncode)
            self.assertEqual(repo.feature, repo.git("rev-parse", target).stdout.strip())
            self.assertEqual(repo.feature, repo.git("rev-parse", "refs/heads/main").stdout.strip())


class PrepareHistoryVerifierIntegrationTests(unittest.TestCase):
    def make_repo(self) -> SyntheticRepository:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = SyntheticRepository(Path(temporary.name))
        repo.land_valid()
        main = repo.git("rev-parse", "refs/heads/main").stdout.strip()
        feature = repo.git("rev-parse", "refs/heads/req/test-01-control").stdout.strip()
        repo.git("update-ref", "refs/remotes/origin/main", main)
        repo.git("update-ref", "refs/remotes/origin/req/test-01-control", feature)
        repo.git("update-ref", "-d", "refs/heads/req/test-01-control")
        return repo

    def validate(self, repo: SyntheticRepository):
        return EVIDENCE.validate_history(
            repo.root,
            "main",
            allow_incomplete=False,
            check_branches=True,
            run_all=False,
            run_latest=False,
            run_witness=False,
        )

    def test_unchanged_history_checker_passes_only_after_actual_refs_are_hydrated(self) -> None:
        repo = self.make_repo()
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "retained req/ branch"):
            self.validate(repo)

        self.assertEqual(1, MODULE.prepare_history_refs(repo.root))
        self.assertEqual(["TEST-01"], self.validate(repo).completed)

        repo.git("update-ref", "-d", "refs/heads/req/test-01-control")
        repo.git("update-ref", "-d", "refs/remotes/origin/req/test-01-control")
        with self.assertRaises(MODULE.HistoryRefsError):
            MODULE.prepare_history_refs(repo.root)
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "retained req/ branch"):
            self.validate(repo)

    def test_wrong_remote_tip_is_copied_exactly_and_still_fails_history_checker(self) -> None:
        repo = self.make_repo()
        repo.git("update-ref", "refs/remotes/origin/req/test-01-control", repo.baseline)

        self.assertEqual(1, MODULE.prepare_history_refs(repo.root))
        self.assertEqual(
            repo.baseline,
            repo.git("rev-parse", "refs/heads/req/test-01-control").stdout.strip(),
        )
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "retained req/ branch"):
            self.validate(repo)

    def test_duplicate_id_refs_remain_visible_to_unchanged_history_checker(self) -> None:
        repo = self.make_repo()
        feature = repo.git("rev-parse", "refs/remotes/origin/req/test-01-control").stdout.strip()
        repo.git("update-ref", "refs/remotes/origin/req/test-01-alternate", feature)

        self.assertEqual(2, MODULE.prepare_history_refs(repo.root))
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "retained req/ branch"):
            self.validate(repo)

    def test_workflow_prepares_full_history_before_the_unchanged_governance_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        governance = workflow.split("\n  governance:\n", 1)[1]
        governance = re.split(r"\n  [a-zA-Z0-9_-]+:\n", governance, maxsplit=1)[0]
        self.assertIn("fetch-depth: 0", governance)
        self.assertLess(
            governance.index("actions/checkout@"), governance.index("scripts/prepare_ci_history.py")
        )
        self.assertLess(
            governance.index("scripts/prepare_ci_history.py"),
            governance.index("make verify-governance"),
        )
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("tests.tooling.test_prepare_ci_history", makefile)


if __name__ == "__main__":
    unittest.main()
