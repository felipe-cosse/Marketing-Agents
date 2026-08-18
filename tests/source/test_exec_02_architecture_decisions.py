import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADR_ROOT = ROOT / "docs" / "adr"


class ArchitectureDecisionTests(unittest.TestCase):
    """Requirement EXEC-02: decisions are explicit before dependent code lands."""

    def test_exec_02_has_the_complete_accepted_adr_baseline(self) -> None:
        files = sorted(ADR_ROOT.glob("[0-9][0-9][0-9][0-9]-*.md"))
        self.assertEqual([f"{number:04d}" for number in range(1, 11)], [path.name[:4] for path in files])
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIn("- Status: Accepted", text, path.name)
            self.assertIn("## Context", text, path.name)
            self.assertIn("## Decision", text, path.name)
            self.assertIn("## Consequences", text, path.name)
            self.assertIn("## Verification", text, path.name)
            self.assertRegex(text, re.compile(r"ASM-\d{3}"), path.name)


if __name__ == "__main__":
    unittest.main()
