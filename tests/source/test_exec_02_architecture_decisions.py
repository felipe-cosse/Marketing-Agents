import json
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
            self.assertNotRegex(text, re.compile(r"\b(?:TODO|TBD|placeholder)\b", re.IGNORECASE), path.name)
            for heading in ("Context", "Decision", "Consequences", "Verification"):
                match = re.search(rf"## {heading}\n\n(.+?)(?=\n## |\Z)", text, re.DOTALL)
                self.assertIsNotNone(match, f"{path.name}: missing {heading}")
                self.assertGreaterEqual(len(match.group(1).strip()), 40, f"{path.name}: empty {heading}")

    def test_exec_02_decision_gate_maps_dependent_paths_to_accepted_adrs(self) -> None:
        gate_path = ADR_ROOT / "decision-gates.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        self.assertEqual(1, gate["schema_version"])
        accepted = {path.name[:4] for path in ADR_ROOT.glob("[0-9][0-9][0-9][0-9]-*.md") if "- Status: Accepted" in path.read_text(encoding="utf-8")}
        self.assertGreaterEqual(len(gate["path_prefixes"]), 5)
        for prefix, adr_ids in gate["path_prefixes"].items():
            self.assertTrue(prefix.endswith("/"))
            self.assertTrue(adr_ids)
            self.assertEqual(set(), set(adr_ids) - accepted, prefix)


if __name__ == "__main__":
    unittest.main()
