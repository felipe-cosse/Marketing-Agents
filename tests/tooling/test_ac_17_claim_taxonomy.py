"""AC-17 copied-root negative controls; stdlib only, no product/network execution."""

from __future__ import annotations

import contextlib
import io
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_ac_17_claims as claims
from scripts.verify_product_docs import GUIDES, DocumentationError, local_links

ROOT = Path(__file__).resolve().parents[2]


class ClaimTaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="marketing-agents-ac17-claims-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        files = {
            *GUIDES,
            "Makefile",
            "catalog/v1/release.lock.json",
            ".python-version",
            ".nvmrc",
            "package.json",
            "apps/web/scripts/browser-evidence-inventory.mjs",
            "apps/api/src/marketing_agents/infrastructure/db/migrations.py",
        }
        # Copy actual documentation and its local evidence, not a synthetic
        # self-consistent document assembled from the checker's expected phrases.
        for relative in GUIDES:
            for path, _ in local_links(ROOT, relative, (ROOT / relative).read_text()):
                if path.is_file():
                    files.add(path.relative_to(ROOT).as_posix())
                elif path.is_dir():
                    (self.root / path.relative_to(ROOT)).mkdir(parents=True, exist_ok=True)
        for relative in files:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())

    def rewrite(self, relative: str, old: str, new: str) -> None:
        path = self.root / relative
        text = path.read_text()
        self.assertIn(old, text, f"negative control must alter real content: {relative}")
        path.write_text(text.replace(old, new))

    def change_section(self, relative: str, heading: str, transform) -> None:
        path = self.root / relative
        text = path.read_text()
        pattern = re.compile(
            r"(^## " + re.escape(heading) + r"\n)(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(text)
        self.assertIsNotNone(match, "negative control needs an existing current section")
        assert match is not None
        replacement = transform(match.group(2))
        self.assertNotEqual(match.group(2), replacement)
        path.write_text(text[: match.start(2)] + replacement + text[match.end(2) :])

    def reject(self, code: str) -> None:
        with self.assertRaisesRegex(DocumentationError, code):
            claims.verify(self.root)

    def test_ac_17_current_guides_and_independent_source_inventory_pass(self) -> None:
        original = claims.verify(ROOT)
        self.assertEqual(original, claims.verify(self.root))
        self.assertEqual(13, original["guides"])
        self.assertEqual(8, original["claim_sections"])
        # These independently reviewed expectations are not generated from the
        # checker's contracts; source mutations below must invalidate the docs.
        self.assertEqual(16, original["browser_runners"])
        self.assertEqual("0009", original["migration_head"])
        self.assertEqual(43, original["instances"])
        self.assertIs(False, original["acceptance_verified"])

    def test_ac_17_every_current_section_is_required_exactly_once(self) -> None:
        for relative, heading in (
            ("README.md", "Current workflow scope"),
            ("README.md", "Current verification commands"),
            ("docs/architecture.md", "Current workflow scope"),
            ("docs/architecture.md", "Current migration scope"),
            ("docs/catalog-authoring.md", "Current executable catalog boundary"),
            ("docs/operations.md", "Current workflow scope"),
            ("docs/testing.md", "Current browser verification scope"),
            ("docs/verification.md", "AC-17 current documentation review"),
        ):
            path = self.root / relative
            original = path.read_text()
            with self.subTest(path=relative, heading=heading, mutation="missing"):
                path.write_text(original.replace("## " + heading, "## Undeclared scope"))
                self.reject("ac17_section")
            with self.subTest(path=relative, heading=heading, mutation="duplicate"):
                path.write_text(original + "\n## " + heading + "\nDuplicate.\n")
                self.reject("ac17_section")
            path.write_text(original)

    def test_ac_17_mock_label_cannot_be_promoted_to_live_verification(self) -> None:
        for relative, heading in (
            ("README.md", "Current workflow scope"),
            ("docs/architecture.md", "Current workflow scope"),
            ("docs/catalog-authoring.md", "Current executable catalog boundary"),
            ("docs/operations.md", "Current workflow scope"),
        ):
            path = self.root / relative
            original = path.read_text()
            with self.subTest(path=relative):
                self.change_section(
                    relative,
                    heading,
                    lambda text: text.replace(
                        "Deterministic mock behavior", "Implemented and verified"
                    ),
                )
                self.reject("ac17_claim")
            path.write_text(original)

    def test_ac_17_current_source_review_cannot_be_promoted_to_a_fresh_pass(self) -> None:
        for relative, heading in (
            ("README.md", "Current verification commands"),
            ("docs/architecture.md", "Current migration scope"),
            ("docs/testing.md", "Current browser verification scope"),
            ("docs/verification.md", "AC-17 current documentation review"),
        ):
            path = self.root / relative
            original = path.read_text()
            with self.subTest(path=relative):
                self.change_section(
                    relative,
                    heading,
                    lambda text: text.replace(
                        "Implemented but not live-tested", "Implemented and verified"
                    ),
                )
                self.reject("ac17_claim")
            path.write_text(original)

    def test_ac_17_workflow_counts_and_preview_authority_are_not_interchangeable(self) -> None:
        for old, new in (
            ("43 catalog instances", "42 catalog instances"),
            ("37 read-role", "36 read-role"),
            ("six inert", "six authorized"),
            ("exact-action approval", "run-wide permission"),
            ("no dispatch", "full dispatch"),
            ("not external delivery", "verified external delivery"),
        ):
            path = self.root / "README.md"
            original = path.read_text()
            with self.subTest(change=old):
                self.rewrite("README.md", old, new)
                self.reject("ac17_(?:claim|preview_authority)")
            path.write_text(original)

    def test_ac_17_stale_current_claims_are_rejected_even_with_correct_section_present(
        self,
    ) -> None:
        cases = (
            (
                "README.md",
                "The worker currently advances only the five implemented demo workflows.",
            ),
            ("README.md", "The current make test is only the source/tooling/network aggregate."),
            (
                "README.md",
                "The planned full make verify and acceptance aggregation are not implemented.",
            ),
            (
                "docs/architecture.md",
                "The deployed run worker executes the five registered demo workflows.",
            ),
            ("docs/architecture.md", "The current local schema includes DEL-05 revision 0006."),
            ("docs/architecture.md", "Worker[Run worker: implemented demo workflows]"),
            (
                "docs/catalog-authoring.md",
                "The current runtime executes five registered demo workflows.",
            ),
            ("docs/operations.md", "The current worker executes only registered demo workflows."),
            ("docs/testing.md", "The command runs all 15 owned browser specs."),
            ("docs/testing.md", "All browser specs now use the automatic fixture."),
        )
        for relative, stale in cases:
            path = self.root / relative
            original = path.read_text()
            with self.subTest(path=relative, stale=stale):
                path.write_text(original + "\n" + stale + "\n")
                self.reject("ac17_stale_claim")
            path.write_text(original)

    def test_ac_17_command_presence_is_not_aggregate_success(self) -> None:
        for old, new in (
            ("backend, network, frontend", "backend, frontend"),
            ("source-drift checks", "optional checks"),
            ("matching the selected commit", "using any local checkout"),
            ("paired\nCompose backup/restore", "optional storage checks"),
            ("not a\nfresh passing result", "a fresh passing result"),
            ("does not establish AC-15", "establishes AC-15"),
        ):
            path = self.root / "README.md"
            original = path.read_text()
            with self.subTest(change=old):
                self.rewrite("README.md", old, new)
                self.reject("ac17_claim")
            path.write_text(original)

    def test_ac_17_browser_real_runtime_and_route_mocks_remain_distinct(self) -> None:
        for old, new in (
            ("16 browser runners", "15 browser runners"),
            (
                "native browser transport, not route-mocked",
                "native browser transport with route-mocked",
            ),
            ("providers remain deterministic mocks", "providers are live services"),
            ("do not prove external delivery", "prove external delivery"),
            ("no\nfresh full-suite pass", "a fresh full-suite pass"),
            (
                "not a universal\noperating-system egress boundary",
                "a universal operating-system egress boundary",
            ),
        ):
            path = self.root / "docs/testing.md"
            original = path.read_text()
            with self.subTest(change=old):
                self.rewrite("docs/testing.md", old, new)
                self.reject("ac17_claim")
            path.write_text(original)

    def test_ac_17_source_browser_inventory_drift_invalidates_current_count(self) -> None:
        self.rewrite(
            "apps/web/scripts/browser-evidence-inventory.mjs",
            '  "run-obj-06-e2e.mjs",\n',
            '  "run-obj-06-e2e.mjs",\n  "run-new-scope-e2e.mjs",\n',
        )
        self.reject("ac17_claim:browser")

    def test_ac_17_duplicate_or_dynamic_browser_source_is_not_evaluated(self) -> None:
        relative = "apps/web/scripts/browser-evidence-inventory.mjs"
        path = self.root / relative
        original = path.read_text()
        for replacement in ('  "run-web-01-e2e.mjs",\n', "  executeUnreviewedCode(),\n"):
            with self.subTest(replacement=replacement):
                self.rewrite(relative, '  "run-obj-06-e2e.mjs",\n', replacement)
                self.reject("ac17_browser_source")
            path.write_text(original)

    def test_ac_17_commented_or_quoted_browser_export_cannot_supply_inventory(self) -> None:
        path = self.root / "apps/web/scripts/browser-evidence-inventory.mjs"
        original = path.read_text()
        declaration = re.search(
            r"export const BROWSER_RUNNERS = Object\.freeze\(\[.*?\]\);",
            original,
            re.DOTALL,
        )
        self.assertIsNotNone(declaration)
        assert declaration is not None
        literal = declaration.group()
        for fake in (
            "/*\n" + literal + "\n*/",
            "const description = `\n" + literal + "\n`;",
        ):
            with self.subTest(wrapper=fake.splitlines()[0]):
                path.write_text(
                    original.replace(
                        literal, fake + "\nexport const BROWSER_RUNNERS = computeInventory();"
                    )
                )
                self.reject("ac17_browser_source")
        path.write_text(original)

    def test_ac_17_browser_binding_requires_exact_executable_prefix_and_uses(self) -> None:
        path = self.root / "apps/web/scripts/browser-evidence-inventory.mjs"
        original = path.read_text()
        for changed in (
            "/* unreviewed header */\n" + original,
            "const prefix = `\n" + original + "\n`;",
            original.replace("export const BROWSER_RUNNERS", "export let BROWSER_RUNNERS"),
            original + "\nBROWSER_RUNNERS = computeInventory();\n",
            original + "\nconst { BROWSER_RUNNERS } = otherSource;\n",
            original + "\nexport { differentInventory as BROWSER_RUNNERS };\n",
            original.replace("runners = BROWSER_RUNNERS", "runners = differentInventory"),
        ):
            with self.subTest(source=changed[:55]):
                path.write_text(changed)
                self.reject("ac17_browser_source")
        path.write_text(original)

    def test_ac_17_source_migration_head_drift_invalidates_current_revision(self) -> None:
        self.rewrite(
            "apps/api/src/marketing_agents/infrastructure/db/migrations.py",
            'HEAD_REVISION = "0009"',
            'HEAD_REVISION = "0010"',
        )
        self.reject("ac17_claim:migration")

    def test_ac_17_migration_reassignments_and_nested_bindings_fail_closed(self) -> None:
        path = self.root / "apps/api/src/marketing_agents/infrastructure/db/migrations.py"
        original = path.read_text()
        additions = (
            'HEAD_REVISION = "0010"',
            "HEAD_REVISION = choose_revision()",
            'HEAD_REVISION: str = "0010"',
            "HEAD_REVISION: str",
            'HEAD_REVISION += "1"',
            'HEAD_REVISION, other = ("0010", None)',
            'other = HEAD_REVISION = "0010"',
            "del HEAD_REVISION",
            '(HEAD_REVISION := "0010")',
            'if condition:\n    HEAD_REVISION = "0010"',
            'def nested():\n    HEAD_REVISION = "0010"',
            "def nested(HEAD_REVISION):\n    pass",
            "def HEAD_REVISION():\n    pass",
            "class HEAD_REVISION:\n    pass",
            "for HEAD_REVISION in values:\n    pass",
            "with context() as HEAD_REVISION:\n    pass",
            "import module as HEAD_REVISION",
            "from module import HEAD_REVISION",
            "try:\n    pass\nexcept Exception as HEAD_REVISION:\n    pass",
            'match value:\n    case {"head": HEAD_REVISION}:\n        pass',
            "match value:\n    case {**HEAD_REVISION}:\n        pass",
            'module.HEAD_REVISION = "0010"',
            'HEAD_REVISION[0] = "1"',
            'globals()["HEAD_REVISION"] = "0010"',
        )
        for addition in additions:
            with self.subTest(mutation=addition):
                path.write_text(original + "\n" + addition + "\n")
                self.reject("ac17_migration_source")
        path.write_text(original)

    def test_ac_17_migration_initial_binding_must_be_one_direct_literal_string(self) -> None:
        path = self.root / "apps/api/src/marketing_agents/infrastructure/db/migrations.py"
        original = path.read_text()
        for binding in (
            'HEAD_REVISION: str = "0009"',
            "HEAD_REVISION = compute_revision()",
            'HEAD_REVISION = "000" + "9"',
            "HEAD_REVISION = 1009",
            'if condition:\n    HEAD_REVISION = "0009"',
            'OTHER = HEAD_REVISION = "0009"',
        ):
            with self.subTest(binding=binding):
                path.write_text(original.replace('HEAD_REVISION = "0009"', binding))
                self.reject("ac17_migration_source")
        path.write_text(original)

    def test_ac_17_no_product_module_is_imported_for_source_facts(self) -> None:
        path = self.root / "apps/api/src/marketing_agents/infrastructure/db/migrations.py"
        path.write_text(
            path.read_text() + '\nraise RuntimeError("product_source_must_not_execute")\n'
        )
        self.assertEqual("0009", claims.verify(self.root)["migration_head"])

    def test_ac_17_evidence_must_be_linked_from_the_relevant_section(self) -> None:
        cases = (
            (
                "README.md",
                "Current workflow scope",
                "apps/api/src/marketing_agents/infrastructure/executable_workflows.py",
            ),
            ("README.md", "Current verification commands", "scripts/verify_local.py"),
            (
                "docs/architecture.md",
                "Current migration scope",
                "../apps/api/src/marketing_agents/infrastructure/db/migrations.py",
            ),
            (
                "docs/testing.md",
                "Current browser verification scope",
                "../apps/web/scripts/browser-evidence-inventory.mjs",
            ),
        )
        for relative, heading, destination in cases:
            path = self.root / relative
            original = path.read_text()
            with self.subTest(path=relative):
                self.change_section(
                    relative,
                    heading,
                    lambda body, link=destination: body.replace(
                        link, "https://example.invalid/unreviewed"
                    ),
                )
                self.reject("ac17_evidence")
            path.write_text(original)

    def test_ac_17_missing_evidence_is_rejected_by_reused_complete_link_check(self) -> None:
        (self.root / "docs/verification/requirements/DEL-05.md").unlink()
        self.reject("broken_link")

    def test_ac_17_missing_current_source_evidence_is_rejected(self) -> None:
        (self.root / "docs/verification/requirements/OBJ-03.md").unlink()
        self.reject("ac17_missing_input")

    def test_ac_17_pending_acceptance_and_diagnostic_limit_cannot_be_promoted(self) -> None:
        for old, new in (
            (
                "Claim: **Acceptance target not yet verified** — AC-15",
                "Claim: **Implemented and verified** — AC-15",
            ),
            ("AC-15, AC-16 and final-source", "Other requirements and final-source"),
            ("acceptance remain unverified", "acceptance are verified"),
            ("source review only, not product reruns", "source review and complete product reruns"),
        ):
            path = self.root / "docs/verification.md"
            original = path.read_text()
            with self.subTest(change=old):
                self.rewrite("docs/verification.md", old, new)
                self.reject("ac17_claim:review")
            path.write_text(original)

    def test_ac_17_credentials_and_real_provider_qualification_remain_deferred(self) -> None:
        self.change_section(
            "docs/adapter-contracts.md",
            "Adding a real integration",
            lambda body: body.replace("Deferred real-adapter work", "Implemented and verified"),
        )
        self.reject("ac17_claim:real-adapters")

    def test_ac_17_provider_credentials_cannot_be_removed_from_deferred_work(self) -> None:
        self.change_section(
            "docs/adapter-contracts.md",
            "Adding a real integration",
            lambda body: body.replace("provider credentials", "automatic authorization"),
        )
        self.reject("ac17_claim:real-adapters")

    def test_ac_17_registry_acquisition_is_not_runtime_provider_networking(self) -> None:
        self.rewrite(
            "docs/testing.md",
            "dependency acquisition requires registry access unless caches are prewarmed",
            "dependency acquisition never needs networking",
        )
        self.reject("ac17_claim:prerequisites")

    def test_ac_17_intake_and_expected_metrics_cannot_be_promoted_to_delivery(self) -> None:
        path = self.root / "docs/demos.md"
        original = path.read_text()
        for old, new in (
            (
                "proves intake, not execution, completion, delivery, or call counts",
                "proves execution, completion, delivery, and call counts",
            ),
            ("contractual expectations, not live metrics", "verified live metrics"),
        ):
            with self.subTest(change=old):
                self.rewrite("docs/demos.md", old, new)
                self.reject("ac17_claim:receipt")
            path.write_text(original)

    def test_ac_17_retention_projection_is_not_physical_erasure(self) -> None:
        path = self.root / "docs/data-handling.md"
        original = path.read_text()
        for old, new in (
            ("policy and projection only", "automatic deletion"),
            (
                "Acceptance target not yet verified / unimplemented maintenance",
                "Implemented and verified maintenance",
            ),
            ("There is no physical retention sweeper", "There is a physical retention sweeper"),
            ("Hiding an API field is not disk erasure", "Hiding an API field is disk erasure"),
        ):
            with self.subTest(change=old):
                self.rewrite("docs/data-handling.md", old, new)
                self.reject("ac17_claim:retention")
            path.write_text(original)

    def test_ac_17_skipped_postgresql_cases_cannot_be_promoted_to_passes(self) -> None:
        self.rewrite("docs/testing.md", "**skipped**, not passed", "passed")
        self.reject("ac17_claim:postgres")

    def test_ac_17_mock_receipts_do_not_establish_remote_exactly_once(self) -> None:
        self.rewrite(
            "docs/architecture.md",
            "no universal distributed exactly-once guarantee",
            "a universal distributed exactly-once guarantee",
        )
        self.reject("ac17_claim:delivery")

    def test_ac_17_host_trust_boundary_is_not_production_qualification(self) -> None:
        self.rewrite(
            "docs/operations.md", "Do not expose the UI publicly", "Expose the UI publicly"
        )
        self.reject("ac17_claim:host-trust")

    def test_ac_17_local_self_approval_is_still_an_assumption_not_dual_control(self) -> None:
        self.rewrite(
            "docs/identity-and-authorization.md",
            "do not describe local-v1 self-approval as dual control",
            "describe local-v1 self-approval as dual control",
        )
        self.reject("ac17_claim:identity")

    def test_ac_17_outbound_risk_cannot_be_deleted_or_promoted(self) -> None:
        self.change_section(
            "docs/security.md",
            "Secrets, defaults and network isolation",
            lambda body: body.replace("Residual risk", "Implemented and verified"),
        )
        self.reject("ac17_claim:network-risk")

    def test_ac_17_all_thirteen_guides_and_seven_labels_still_matter(self) -> None:
        (self.root / "docs/demos.md").unlink()
        self.reject("ac17_missing_input:docs/demos.md")
        (self.root / "docs/demos.md").write_bytes((ROOT / "docs/demos.md").read_bytes())
        self.rewrite("docs/verification.md", "| Residual risk |", "| Unclassified |")
        # Other prose can still contain the label; removing every occurrence is
        # required by DEL-06's intentionally broad taxonomy presence check.
        path = self.root / "docs/verification.md"
        path.write_text(
            re.sub("residual risk", "unclassified", path.read_text(), flags=re.IGNORECASE)
        )
        self.reject("missing_taxonomy:residual risk")

    def test_ac_17_historical_counts_are_preserved_not_treated_as_current(self) -> None:
        path = self.root / "docs/verification.md"
        source = path.read_text()
        self.assertIn("all 15 unfiltered runners", source)
        self.assertIn("32 cases with zero skips", source)
        path.write_text(
            source + "\n## Older evidence\nHistorical: 15 runners, 32 cases, revision 0006.\n"
        )
        self.assertEqual(16, claims.verify(self.root)["browser_runners"])

    def test_ac_17_historical_browser_counts_cannot_be_rewritten_as_current(self) -> None:
        self.change_section(
            "docs/verification.md",
            "DEL-07 local verification",
            lambda body: body.replace("all 15 unfiltered runners", "all 16 unfiltered runners"),
        )
        self.reject("ac17_claim:historical-browser")

    def test_ac_17_restored_stale_section_fails_for_claim_not_missing_dependencies(self) -> None:
        self.change_section(
            "README.md",
            "Current workflow scope",
            lambda _: (
                "\nClaim: Deterministic mock behavior — the worker currently advances "
                "only the five implemented demo workflows.\n\n"
            ),
        )
        self.reject("ac17_stale_claim:README.md")

    def test_ac_17_contract_text_in_comments_or_code_is_not_evidence(self) -> None:
        for wrapper in (
            lambda body: "<!--" + body + "-->",
            lambda body: "```text\n" + body + "\n```\n",
        ):
            path = self.root / "README.md"
            original = path.read_text()
            self.change_section("README.md", "Current workflow scope", wrapper)
            self.reject("ac17_claim")
            path.write_text(original)

    def test_ac_17_inputs_are_bounded_and_symlinks_are_rejected(self) -> None:
        path = self.root / "README.md"
        original = path.read_bytes()
        path.write_bytes(b"x" * (claims.MAX_INPUT_BYTES + 1))
        self.reject("ac17_input_bound")
        path.unlink()
        path.symlink_to(ROOT / "README.md")
        self.reject("ac17_missing_input")
        path.unlink()
        path.write_bytes(original)

    def test_ac_17_offline_cli_reports_no_acceptance_and_sanitizes_failures(self) -> None:
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, claims.main(["--root", str(self.root)]))
            self.assertIs(False, json.loads(output.getvalue())["acceptance_verified"])
            (self.root / "README.md").unlink()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, claims.main(["--root", str(self.root)]))
            self.assertEqual(
                "AC-17 documentation claim verification failed: ac17_missing_input\n",
                output.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
