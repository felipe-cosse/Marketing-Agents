"""Fast EXEC-08 guide-only fixtures; no catalog copies or product execution."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_exec_08_handoff as handoff
from scripts.verify_product_docs import DocumentationError

ROOT = Path(__file__).resolve().parents[2]
# Independent, deliberately small documentation fixture. Do not assemble this
# from the checker's phrase inventory or copy the repository into every test.
GUIDE = """# Verification

## EXEC-08 current verification handoff

### Source and qualification boundary

Inherited EXEC-07 baseline, not fresh EXEC-08 verification:
main a8e197da4ca9f4a7abbc842af737b1c8fbecb2ef;
feature 63df4bb4f39f22d31e9c3638b5e7e6e4049a4650;
tree b8eccaaf6cd91a1cb7ac64bbb850187e13e8e93f.

### Files changed

- `Makefile`
- `docs/verification.md`
- `docs/assumptions.md`
- `scripts/verify_exec_08_handoff.py`
- `tests/tooling/test_exec_08_handoff.py`
- `docs/implementation-plan/16-requirements-traceability-matrix.md`
- `docs/verification/requirements/EXEC-08.json`
- `docs/verification/requirements/EXEC-08.md`

### Commands and observed baseline results

`python -B -m scripts.ac_16_quality run`: 81 catalog tests passed;
4,932 other backend tests passed; 33 optional PostgreSQL tests skipped.
472 frontend tests in 55 files; 17 safety modules; 16 browser runners.
`make verify-clean REF=HEAD`: 5,013 backend tests passed and 416 subtests passed;
33 optional PostgreSQL tests skipped. `make test-del-05-compose-backup` passed.
Failures, errors, skips and subtests are reported separately;
subtests are not additional test cases. These passed baseline runs had zero
failures and errors, not a statement about candidate execution.

### Candidate verification and handoff

Candidate verification is pending: all 20 quality gates, complete clean-state
journey, paired backup/restore and connection witness must run for the candidate.
The external source-bound attestation records the actual candidate result;
documentation checks do not authenticate execution.

### Assumptions and remaining limits

CI remains disabled, not green. Historical AC-15 evidence is not a fresh
four-suite run. DEL-01 remains pending. Deterministic mock behavior is not
live-provider delivery. PostgreSQL skips are not passes. Credentials, raw
payloads, databases and secret-bearing backups are not verification artifacts.

## AC-17 current documentation review

Historical snapshot: the prior review retains its original scoped observations.
"""


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="marketing-agents-exec08-guide-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "docs/verification.md"
        self.path.parent.mkdir()
        self.path.write_text(GUIDE, encoding="utf-8")

    def rewrite(self, old: str, new: str) -> None:
        self.assertIn(old, GUIDE)
        self.path.write_text(GUIDE.replace(old, new), encoding="utf-8")

    def reject(self, code: str) -> None:
        with self.assertRaisesRegex(DocumentationError, code):
            handoff.verify(self.root)

    def test_minimal_document_is_read_only_and_documentation_only(self) -> None:
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)
        with (
            patch("subprocess.run", side_effect=AssertionError("no execution")),
            patch("socket.create_connection", side_effect=AssertionError("no network")),
        ):
            result = handoff.verify(self.root)
        self.assertEqual("documentation-only", result["scope"])
        self.assertEqual(1, result["guides"])
        self.assertEqual(5, result["handoff_sections"])
        self.assertEqual(8, result["changed_files"])
        self.assertIs(False, result["execution_authenticated"])
        self.assertIs(False, result["candidate_verification_established"])
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))

    def test_live_guide_satisfies_documentation_only_contract(self) -> None:
        self.assertEqual(handoff.verify(self.root), handoff.verify(ROOT))

    def test_every_literal_heading_is_required_once(self) -> None:
        headings = (
            "## EXEC-08 current verification handoff",
            "## AC-17 current documentation review",
            "### Source and qualification boundary",
            "### Files changed",
            "### Commands and observed baseline results",
            "### Candidate verification and handoff",
            "### Assumptions and remaining limits",
        )
        for heading in headings:
            for replacement in ("## Missing", heading + "\n" + heading):
                with self.subTest(heading=heading, replacement=replacement):
                    self.rewrite(heading, replacement)
                    self.reject("exec08_section")

    def test_hidden_headings_do_not_satisfy_sections(self) -> None:
        for wrapper in ("<!--\n{}\n-->", "```markdown\n{}\n```", "~~~\n{}\n~~~", "    {}"):
            with self.subTest(wrapper=wrapper):
                self.rewrite(
                    "## EXEC-08 current verification handoff",
                    wrapper.format("## EXEC-08 current verification handoff"),
                )
                self.reject("exec08_section")

    def test_hidden_limit_phrases_do_not_satisfy_claims(self) -> None:
        for wrapper in ("<!--{}-->", "```text\n{}\n```", "~~~text\n{}\n~~~"):
            with self.subTest(wrapper=wrapper):
                self.rewrite(
                    "DEL-01 remains pending.",
                    "\n" + wrapper.format("DEL-01 remains pending.") + "\n",
                )
                self.reject("exec08_claim")

    def test_comment_without_close_hides_following_claims(self) -> None:
        self.path.write_text("<!--\n" + GUIDE, encoding="utf-8")
        self.reject("exec08_section")

    def test_shorter_fence_does_not_end_longer_fenced_example(self) -> None:
        self.path.write_text("````markdown\n```\n" + GUIDE + "\n````", encoding="utf-8")
        self.reject("exec08_section")

    def test_wrong_heading_level_does_not_satisfy_subsection(self) -> None:
        self.rewrite("### Files changed", "#### Files changed")
        self.reject("exec08_section")

    def test_each_baseline_identity_is_required(self) -> None:
        for identity in (
            "a8e197da4ca9f4a7abbc842af737b1c8fbecb2ef",
            "63df4bb4f39f22d31e9c3638b5e7e6e4049a4650",
            "b8eccaaf6cd91a1cb7ac64bbb850187e13e8e93f",
        ):
            with self.subTest(identity=identity):
                self.rewrite(identity, "0" * 40)
                self.reject("exec08_claim")

    def test_baseline_counts_and_case_subtest_distinction_are_not_omitted(self) -> None:
        for phrase in (
            "81 catalog tests passed",
            "4,932 other backend tests passed",
            "33 optional PostgreSQL tests skipped",
            "472 frontend tests in 55 files",
            "17 safety modules",
            "16 browser runners",
            "5,013 backend tests passed and 416 subtests passed",
            "Failures, errors, skips and subtests are reported separately",
            "subtests are not additional test cases",
        ):
            with self.subTest(phrase=phrase):
                self.rewrite(phrase, "observation omitted")
                self.reject("exec08_claim")

    def test_changed_file_inventory_rejects_missing_and_duplicate_paths(self) -> None:
        for replacement in ("- `somewhere-else`", "- `Makefile`\n- `Makefile`"):
            with self.subTest(replacement=replacement):
                self.rewrite("- `Makefile`", replacement)
                self.reject("exec08_(claim|changed_files)")

    def test_qualification_and_limit_removals_fail(self) -> None:
        for phrase in (
            "not fresh EXEC-08 verification",
            "Candidate verification is pending",
            "all 20 quality gates",
            "complete clean-state\njourney",
            "paired backup/restore",
            "connection witness",
            "external source-bound attestation",
            "documentation checks do not authenticate execution",
            "CI remains disabled, not green",
            "Historical AC-15 evidence is not a fresh\nfour-suite run",
            "DEL-01 remains pending",
            "Deterministic mock behavior is not\nlive-provider delivery",
            "PostgreSQL skips are not passes",
            "Credentials, raw\npayloads, databases and secret-bearing backups "
            "are not verification artifacts",
        ):
            with self.subTest(phrase=phrase):
                self.rewrite(phrase, "unsupported scope")
                self.reject("exec08_claim")

    def test_known_scope_promotions_fail_even_when_limits_remain(self) -> None:
        for phrase in (
            "EXEC-08 full verification passed",
            "CI is green",
            "DEL-01 is complete",
            "PostgreSQL is qualified",
            "AC-15 four-suite verification was rerun",
            "live-provider delivery verified",
        ):
            with self.subTest(phrase=phrase):
                self.rewrite(
                    "## AC-17 current documentation review",
                    phrase + "\n\n## AC-17 current documentation review",
                )
                self.reject("exec08_unsupported_promotion")

    def test_historical_snapshot_marker_is_required_in_historical_section(self) -> None:
        self.rewrite("Historical snapshot:", "Current certification:")
        self.reject("exec08_historical_marker")

    def test_historical_claims_are_not_treated_as_current(self) -> None:
        self.path.write_text(GUIDE + "\nHistorical wording: CI is green.\n", encoding="utf-8")
        self.assertEqual("documentation-only", handoff.verify(self.root)["scope"])

    def test_missing_or_nonregular_input_fails(self) -> None:
        self.path.unlink()
        self.reject("exec08_input_path")
        self.path.mkdir()
        self.reject("exec08_input_path")

    def test_file_and_parent_symlinks_fail(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text(GUIDE, encoding="utf-8")
        self.path.unlink()
        self.path.symlink_to(outside)
        self.reject("exec08_input_path")
        self.path.unlink()
        self.path.parent.rmdir()
        other = self.root / "other"
        other.mkdir()
        (other / "verification.md").write_text(GUIDE, encoding="utf-8")
        self.path.parent.symlink_to(other, target_is_directory=True)
        self.reject("exec08_input_path")

    def test_root_symlink_and_missing_root_fail(self) -> None:
        link = self.root / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        for root in (link, self.root / "missing"):
            with (
                self.subTest(root=root),
                self.assertRaisesRegex(DocumentationError, "exec08_input_root"),
            ):
                handoff.verify(root)

    def test_oversized_and_invalid_utf8_inputs_fail(self) -> None:
        self.path.write_bytes(b"x" * (128 * 1024 + 1))
        self.reject("exec08_input_bound")
        self.path.write_bytes(b"\xff")
        self.reject("exec08_input_encoding")

    def test_cli_emits_only_documentation_scope_or_bounded_error(self) -> None:
        output = io.StringIO()
        with (
            patch("sys.argv", ["verify", "--root", str(self.root)]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, handoff.main())
        self.assertEqual("documentation-only", json.loads(output.getvalue())["scope"])
        output = io.StringIO()
        with (
            patch("sys.argv", ["verify", "--root", str(self.root)]),
            patch.object(handoff, "verify", side_effect=OSError("secret-bearing-path")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(1, handoff.main())
        self.assertIn("exec08_input_unreadable", output.getvalue())
        self.assertNotIn("secret-bearing-path", output.getvalue())


if __name__ == "__main__":
    unittest.main()
