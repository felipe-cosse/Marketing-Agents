"""The explicit CI maintenance approval cannot waive requirement history."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.tooling.test_verify_requirement_evidence import MODULE, SyntheticRepository

MERGE = "merge: CI-MAINT-01 repair retained refs and CI diagnostics"
FEATURE = "ci: repair retained refs and failure diagnostics"
SECOND_MERGE = "merge: CI-MAINT-02 isolate offline verification and record timings"
SECOND_FEATURE = "ci: isolate offline verification and record timings"
SECOND_BRANCH = "codex/ci-maintenance-two"


class MaintenanceHistoryTests(unittest.TestCase):
    def repository(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = SyntheticRepository(Path(temporary.name))
        repo.land_valid()
        return repo

    def approval(self, repo):
        return {
            "base_commit": repo.git("rev-parse", "main").stdout.strip(),
            "feature_subject": FEATURE,
            "allowed_paths": ["ci.txt", str(MODULE.POLICY_PATH)],
            "approval": "Explicit synthetic one-off CI maintenance approval.",
        }

    def write_policy(self, repo, approval, subject=MERGE):
        path = repo.root / MODULE.POLICY_PATH
        policy = json.loads(path.read_text())
        policy.setdefault("maintenance_merge_exceptions", {})[subject] = approval
        path.write_text(json.dumps(policy))

    def feature(
        self,
        repo,
        *,
        approval=None,
        extra_path=None,
        subject=FEATURE,
        merge_subject=MERGE,
        branch="codex/ci-maintenance",
        path="ci.txt",
    ):
        approval = approval or self.approval(repo)
        repo.git("switch", "-c", branch)
        self.write_policy(repo, approval, merge_subject)
        (repo.root / path).write_text("repair\n")
        if extra_path:
            (repo.root / extra_path).write_text("outside approval\n")
        repo.git("add", ".")
        repo.git("commit", "-m", subject)
        return approval

    def merge(self, repo, subject=MERGE, *, branch="codex/ci-maintenance"):
        repo.git("switch", "main")
        repo.git("merge", "--no-ff", branch, "-m", subject)

    def second_feature(self, repo, *, extra_path=None):
        approval = self.approval(repo)
        approval["feature_subject"] = SECOND_FEATURE
        approval["allowed_paths"] = ["ci-budget.txt", str(MODULE.POLICY_PATH)]
        self.feature(
            repo,
            approval=approval,
            extra_path=extra_path,
            subject=SECOND_FEATURE,
            merge_subject=SECOND_MERGE,
            branch=SECOND_BRANCH,
            path="ci-budget.txt",
        )
        return approval

    def validate(self, repo, ref="main"):
        return MODULE.validate_history(
            repo.root,
            ref,
            allow_incomplete=False,
            check_branches=True,
            run_all=False,
            run_latest=False,
            run_witness=False,
        )

    def test_approved_merge_preserves_requirement_counts_and_branches(self):
        repo = self.repository()
        self.feature(repo)
        # Policy may be present before this approved merge is landed.
        before = self.validate(repo)
        self.assertEqual(["TEST-01"], before.completed)
        self.assertEqual([], before.maintenance_merges)
        self.merge(repo)
        result = self.validate(repo)
        self.assertEqual(1, result.requirement_count)
        self.assertEqual(["TEST-01"], result.completed)
        self.assertEqual([], result.missing)
        self.assertEqual([repo.git("rev-parse", "main").stdout.strip()], result.maintenance_merges)
        # Neither the exception nor hydration can excuse missing requirement refs.
        repo.git("update-ref", "-d", "refs/heads/req/test-01-control")
        with self.assertRaisesRegex(MODULE.EvidenceError, "retained req/ branch"):
            self.validate(repo)

    def test_historical_first_parent_snapshot_before_approval_remains_valid(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo)
        self.assertEqual(["TEST-01"], self.validate(repo, "main^1").completed)
        earlier = MODULE.validate_history(
            repo.root,
            repo.baseline,
            allow_incomplete=True,
            check_branches=True,
            run_all=False,
            run_latest=False,
            run_witness=False,
        )
        self.assertEqual([], earlier.completed)
        self.assertEqual([], earlier.maintenance_merges)

    def test_distinct_sequential_approvals_preserve_requirements_and_first_approval(self):
        repo = self.repository()
        original = self.validate(repo)
        retained_refs = repo.git(
            "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/req/"
        ).stdout
        first_approval = self.feature(repo)
        self.merge(repo)
        first_merge = repo.git("rev-parse", "main").stdout.strip()
        second_approval = self.second_feature(repo)
        self.assertEqual(first_merge, second_approval["base_commit"])
        self.assertNotEqual(first_approval["base_commit"], second_approval["base_commit"])
        pending = self.validate(repo)
        self.assertEqual([first_merge], pending.maintenance_merges)
        self.merge(repo, SECOND_MERGE, branch=SECOND_BRANCH)
        second_merge = repo.git("rev-parse", "main").stdout.strip()

        result = self.validate(repo)
        self.assertEqual(original.requirement_count, result.requirement_count)
        self.assertEqual(original.completed, result.completed)
        self.assertEqual(original.missing, result.missing)
        self.assertEqual(original.results, result.results)
        self.assertEqual([first_merge, second_merge], result.maintenance_merges)
        self.assertEqual(
            retained_refs,
            repo.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/req/").stdout,
        )
        self.assertEqual(
            {MERGE: first_approval, SECOND_MERGE: second_approval},
            json.loads((repo.root / MODULE.POLICY_PATH).read_text())[
                "maintenance_merge_exceptions"
            ],
        )
        self.assertEqual(
            {MERGE: first_approval},
            json.loads(repo.git("show", f"{first_merge}:{MODULE.POLICY_PATH}").stdout)[
                "maintenance_merge_exceptions"
            ],
        )
        repo.git("update-ref", "-d", "refs/heads/req/test-01-control")
        with self.assertRaisesRegex(MODULE.EvidenceError, "retained req/ branch"):
            self.validate(repo)

    def test_two_approvals_keep_all_historical_first_parent_snapshots_valid(self):
        repo = self.repository()
        requirement_merge = repo.git("rev-parse", "main").stdout.strip()
        self.feature(repo)
        self.merge(repo)
        first_merge = repo.git("rev-parse", "main").stdout.strip()
        self.second_feature(repo)
        self.merge(repo, SECOND_MERGE, branch=SECOND_BRANCH)
        second_merge = repo.git("rev-parse", "main").stdout.strip()

        for ref, completed, maintenance in (
            (repo.baseline, [], []),
            (requirement_merge, ["TEST-01"], []),
            (first_merge, ["TEST-01"], [first_merge]),
            (second_merge, ["TEST-01"], [first_merge, second_merge]),
        ):
            with self.subTest(ref=ref):
                result = MODULE.validate_history(
                    repo.root,
                    ref,
                    allow_incomplete=True,
                    check_branches=True,
                    run_all=False,
                    run_latest=False,
                    run_witness=False,
                )
                self.assertEqual(1, result.requirement_count)
                self.assertEqual(completed, result.completed)
                self.assertEqual([] if completed else ["TEST-01"], result.missing)
                self.assertEqual(maintenance, result.maintenance_merges)

    def test_second_approval_cannot_reuse_first_merge_subject(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo)
        self.second_feature(repo)
        self.merge(repo, MERGE, branch=SECOND_BRANCH)
        with self.assertRaisesRegex(MODULE.EvidenceError, "duplicate approved maintenance"):
            self.validate(repo)

    def test_second_approval_does_not_inherit_first_path_allowlist(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo)
        # ci.txt was approved for the first repair, not the second repair.
        self.second_feature(repo, extra_path="ci.txt")
        self.merge(repo, SECOND_MERGE, branch=SECOND_BRANCH)
        with self.assertRaisesRegex(MODULE.EvidenceError, "approved exact paths"):
            self.validate(repo)

    def test_second_approval_cannot_replace_first_history_exception(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo)
        self.second_feature(repo)
        path = repo.root / MODULE.POLICY_PATH
        policy = json.loads(path.read_text())
        del policy["maintenance_merge_exceptions"][MERGE]
        path.write_text(json.dumps(policy))
        repo.git("add", str(MODULE.POLICY_PATH))
        repo.git("commit", "--amend", "--no-edit")
        self.merge(repo, SECOND_MERGE, branch=SECOND_BRANCH)
        with self.assertRaisesRegex(MODULE.EvidenceError, "not a requirement merge"):
            self.validate(repo)

    def test_pending_approval_base_must_resolve_to_commit(self):
        repo = self.repository()
        tree = repo.git("rev-parse", "main^{tree}").stdout.strip()
        for base in ("f" * 40, tree):
            with self.subTest(base=base):
                approval = self.approval(repo)
                approval["base_commit"] = base
                self.write_policy(repo, approval)
                with self.assertRaises(MODULE.EvidenceError):
                    self.validate(repo)

    def test_duplicate_base_approvals_are_rejected(self):
        repo = self.repository()
        with self.assertRaisesRegex(MODULE.EvidenceError, "duplicate approved maintenance base"):
            MODULE.maintenance_merge_exceptions(
                {
                    "maintenance_merge_exceptions": {
                        MERGE: self.approval(repo),
                        "merge: CI-MAINT-02 second approval": self.approval(repo),
                    }
                }
            )

    def test_non_requirement_mainline_commit_is_still_forbidden(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo)
        repo.git("commit", "--allow-empty", "-m", "ci: unapproved follow-up")
        with self.assertRaisesRegex(MODULE.EvidenceError, "not a requirement merge"):
            self.validate(repo)

    def test_scope_violation_is_rejected(self):
        repo = self.repository()
        self.feature(repo, extra_path="application.py")
        self.merge(repo)
        with self.assertRaisesRegex(MODULE.EvidenceError, "approved exact paths"):
            self.validate(repo)

    def test_rename_cannot_hide_out_of_scope_deletion(self):
        repo = self.repository()
        self.feature(repo)
        repo.git("rm", "ci.txt")
        repo.git("mv", "src/control.txt", "ci.txt")
        repo.git("commit", "--amend", "--no-edit")
        self.merge(repo)
        with self.assertRaisesRegex(MODULE.EvidenceError, "approved exact paths"):
            self.validate(repo)

    def test_feature_subject_must_match_exactly(self):
        repo = self.repository()
        self.feature(repo, subject=FEATURE + " extra")
        self.merge(repo)
        with self.assertRaisesRegex(MODULE.EvidenceError, "approved feature commit"):
            self.validate(repo)

    def test_merge_subject_must_match_exactly(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo, subject=MERGE + " extra")
        with self.assertRaisesRegex(MODULE.EvidenceError, "not a requirement merge"):
            self.validate(repo)

    def test_multiple_feature_commits_are_rejected(self):
        repo = self.repository()
        self.feature(repo)
        repo.git("commit", "--allow-empty", "-m", FEATURE)
        self.merge(repo)
        with self.assertRaisesRegex(MODULE.EvidenceError, "exactly one directly based"):
            self.validate(repo)

    def test_empty_maintenance_feature_is_rejected(self):
        repo = self.repository()
        approval = self.approval(repo)
        repo.git("switch", "-c", "codex/ci-maintenance")
        repo.git("commit", "--allow-empty", "-m", FEATURE)
        self.merge(repo)
        self.write_policy(repo, approval)
        with self.assertRaisesRegex(MODULE.EvidenceError, "nonempty"):
            self.validate(repo)

    def test_single_parent_commit_cannot_impersonate_approved_merge(self):
        repo = self.repository()
        self.feature(repo, subject=MERGE)
        repo.git("switch", "main")
        repo.git("merge", "--ff-only", "codex/ci-maintenance")
        with self.assertRaisesRegex(MODULE.EvidenceError, "two parents"):
            self.validate(repo)

    def test_wrong_approved_base_is_rejected(self):
        repo = self.repository()
        approval = self.approval(repo)
        approval["base_commit"] = repo.baseline
        self.feature(repo, approval=approval)
        self.merge(repo)
        with self.assertRaisesRegex(MODULE.EvidenceError, "exact approved base"):
            self.validate(repo)

    def test_merge_only_changes_are_rejected(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo)
        (repo.root / "ci.txt").write_text("unreviewed merge resolution\n")
        repo.git("add", "ci.txt")
        repo.git("commit", "--amend", "--no-edit")
        with self.assertRaisesRegex(MODULE.EvidenceError, "merge tree differs"):
            self.validate(repo)

    def test_approval_cannot_be_skipped_after_its_base(self):
        repo = self.repository()
        approval = self.approval(repo)
        approval["base_commit"] = repo.baseline
        self.write_policy(repo, approval)
        with self.assertRaisesRegex(MODULE.EvidenceError, "unused maintenance"):
            self.validate(repo)

    def test_duplicate_approved_merge_is_rejected(self):
        repo = self.repository()
        self.feature(repo)
        self.merge(repo)
        repo.git("switch", "-c", "codex/duplicate")
        repo.git("commit", "--allow-empty", "-m", FEATURE)
        repo.git("switch", "main")
        repo.git("merge", "--no-ff", "codex/duplicate", "-m", MERGE)
        with self.assertRaisesRegex(MODULE.EvidenceError, "duplicate approved maintenance"):
            self.validate(repo)

    def test_policy_rejects_broad_or_ambiguous_approvals(self):
        repo = self.repository()
        cases = [
            ("base_commit", "main"),
            ("base_commit", "a" * 39),
            ("feature_subject", "[TEST-01] steal a requirement"),
            ("feature_subject", FEATURE + "\nmore"),
            ("approval", ""),
            ("allowed_paths", ["src/**"]),
            ("allowed_paths", ["../outside"]),
            ("allowed_paths", ["/absolute"]),
            ("allowed_paths", [".git/config"]),
            ("allowed_paths", ["ci.txt", "ci.txt"]),
            ("unexpected", True),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                approval = self.approval(repo)
                approval[key] = value
                with self.assertRaises(MODULE.EvidenceError):
                    MODULE.maintenance_merge_exceptions(
                        {"maintenance_merge_exceptions": {MERGE: approval}}
                    )
        for subject in ("merge: TEST-01 override", "merge: arbitrary", MERGE + "\nextra"):
            with self.subTest(subject=subject), self.assertRaises(MODULE.EvidenceError):
                MODULE.maintenance_merge_exceptions(
                    {"maintenance_merge_exceptions": {subject: self.approval(repo)}}
                )


if __name__ == "__main__":
    unittest.main()
