"""DEL-06 documentation consumers and failure-path checks; stdlib/offline only."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.verify_product_docs import (
    CLAIM_LABELS,
    GUIDES,
    DocumentationError,
    heading_anchors,
    local_links,
    shell_make_targets,
    verify,
)

ROOT = Path(__file__).resolve().parents[2]


class ProductDocumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="marketing-agents-del06-docs-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        files = {
            *GUIDES,
            "Makefile",
            "catalog/v1/release.lock.json",
            ".python-version",
            ".nvmrc",
            "package.json",
        }
        directories: set[str] = set()
        # Copy only the guides, their contract inputs and linked evidence, not
        # arbitrary working files, dependencies, databases or credentials.
        for relative in GUIDES:
            for target, _ in local_links(ROOT, relative, (ROOT / relative).read_text()):
                if target.is_file():
                    files.add(str(target.relative_to(ROOT)))
                elif target.is_dir():
                    directories.add(str(target.relative_to(ROOT)))
        for relative in directories:
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        for relative in files:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / relative).read_bytes())

    def rewrite(self, relative: str, old: str, new: str) -> None:
        path = self.root / relative
        text = path.read_text()
        self.assertIn(old, text, f"negative control must mutate existing content: {relative}")
        path.write_text(text.replace(old, new))

    def append(self, text: str) -> None:
        path = self.root / "docs/testing.md"
        path.write_text(path.read_text() + "\n" + text + "\n")

    def reject(self, code: str) -> None:
        with self.assertRaisesRegex(DocumentationError, code):
            verify(self.root)

    def test_required_current_product_guides_pass_from_repository_and_copied_root(self) -> None:
        original = verify(ROOT)
        self.assertEqual(original, verify(self.root))
        self.assertEqual(13, original["guides"])
        self.assertGreater(original["local_links"], 30)
        self.assertGreater(original["make_targets"], 5)

    def test_every_required_guide_is_a_causal_input(self) -> None:
        for relative in GUIDES:
            with self.subTest(guide=relative):
                path = self.root / relative
                text = path.read_text()
                path.unlink()
                with self.assertRaises(DocumentationError):
                    verify(self.root)
                path.write_text(text)

    def test_rejects_placeholder_guide(self) -> None:
        (self.root / "docs/security.md").write_text("# Security\n\nComing soon.\n")
        self.reject("missing_sections")

    def test_rejects_unresolved_placeholder_inside_complete_guide(self) -> None:
        self.append("TBD: replace this with actual evidence.")
        self.reject("placeholder")

    def test_rejects_unclassified_guide(self) -> None:
        path = self.root / "docs/testing.md"
        text = path.read_text()
        for label in CLAIM_LABELS:
            text = re.sub(re.escape(label), "Undeclared status", text, flags=re.IGNORECASE)
        path.write_text(text)
        self.reject("unclassified_claims")

    def test_rejects_missing_local_evidence(self) -> None:
        self.append("[missing proof](missing-del06-proof.json)")
        self.reject("broken_link")

    def test_rejects_broken_local_heading(self) -> None:
        self.append("[missing section](testing.md#nonexistent-proof)")
        self.reject("broken_anchor")

    def test_accepts_existing_and_duplicate_heading_anchors(self) -> None:
        self.append("## Inspection\n\n## Inspection\n\n[proof](testing.md#inspection-1)")
        self.assertEqual(13, verify(self.root)["guides"])
        self.assertEqual({"a-b", "a-b-1"}, heading_anchors("## A `B`\n## A B\n"))

    def test_rejects_links_outside_repository_even_when_destination_exists(self) -> None:
        self.append("[outside](../../outside-proof.md)")
        self.reject("escaped_link")

    def test_rejects_executable_and_file_url_links(self) -> None:
        for link in ("javascript:alert", "file:///etc/hosts", "//outside.invalid/proof"):
            with self.subTest(link=link), self.assertRaises(DocumentationError):
                local_links(self.root, "README.md", f"[proof]({link})")

    def test_external_sources_are_not_fetched(self) -> None:
        self.append("[external source](https://example.invalid/del06)")
        self.assertEqual(13, verify(self.root)["guides"])

    def test_rejects_missing_readme_navigation(self) -> None:
        # Keep the guide valid while removing its actual entry-point link.
        self.rewrite("README.md", "docs/architecture.md", "docs/operations.md")
        self.reject("missing_readme_navigation:docs/architecture.md")

    def test_rejects_unknown_make_command_without_running_it(self) -> None:
        self.append("```sh\nmake del06-does-not-exist\n```")
        self.reject("unknown_make_target")

    def test_make_parser_handles_multiple_targets_assignments_and_continuations(self) -> None:
        text = (
            "```sh\nUV_OFFLINE=1 make migrate seed "
            "\\\n"
            "  CATALOG_ROOT=catalog/v1\nmake verify-clean REF=HEAD # comment\n```"
        )
        self.assertEqual({"migrate", "seed", "verify-clean"}, shell_make_targets(text))

    def test_all_compound_make_commands_are_checked(self) -> None:
        for separator in (" && ", ";", " || ", " | "):
            with self.subTest(separator=separator):
                text = "```sh\nmake up" + separator + "make del06-does-not-exist\n```"
                self.assertEqual({"up", "del06-does-not-exist"}, shell_make_targets(text))

    def test_rejects_nonliteral_make_target(self) -> None:
        self.append("```sh\nmake '${UNREVIEWED_TARGET}'\n```")
        self.reject("nonliteral_make_target")

    def test_release_count_changes_invalidate_documented_counts(self) -> None:
        path = self.root / "catalog/v1/release.lock.json"
        lock = json.loads(path.read_text())
        lock["counts"]["instances"] = 44
        path.write_text(json.dumps(lock))
        self.reject("catalog_count_drift:instances")

    def test_release_hash_changes_invalidate_verification_record(self) -> None:
        path = self.root / "catalog/v1/release.lock.json"
        lock = json.loads(path.read_text())
        lock["content_hash"] = "catalog-sha256-v1:" + "0" * 64
        path.write_text(json.dumps(lock))
        self.reject("catalog_hash_drift")

    def test_pinned_toolchain_changes_invalidate_readme(self) -> None:
        for relative, changed, code in (
            (".nvmrc", "25.0.0\n", "Node"),
            (".python-version", "3.13\n", "Python"),
            ("package.json", '{"packageManager":"pnpm@12.0.0"}', "pnpm"),
        ):
            with self.subTest(tool=code):
                path = self.root / relative
                original = path.read_text()
                path.write_text(changed)
                self.reject(f"toolchain_drift:{code}")
                path.write_text(original)

    def test_stable_assumptions_and_closed_lifecycle_decision_are_required(self) -> None:
        self.rewrite("docs/assumptions.md", "| ASM-017 | accepted |", "| ASM-017 | provisional |")
        self.reject("lifecycle_decision_not_closed")

    def test_all_claim_taxonomy_categories_are_required(self) -> None:
        self.rewrite("docs/verification.md", "Implemented but not live-tested", "Unreviewed code")
        self.reject("missing_taxonomy:implemented but not live-tested")

    def test_cli_checks_explicit_root_and_fails_when_a_guide_is_absent(self) -> None:
        command = [
            sys.executable,
            str(ROOT / "scripts/verify_product_docs.py"),
            "--root",
            str(self.root),
        ]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual(13, json.loads(result.stdout)["guides"])
        (self.root / "docs/demos.md").unlink()
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(1, result.returncode)
        self.assertIn("DEL-06 documentation verification failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
