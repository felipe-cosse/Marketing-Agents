import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "assumptions.md"
ROW = re.compile(r"^\| (ASM-(\d{3})) \| (accepted|provisional) \| (.+?) \| (.+?) \|$", re.MULTILINE)


class AssumptionRegisterTests(unittest.TestCase):
    """Requirement SRC-03: every material implementation assumption is explicit."""

    def test_src_03_has_complete_stable_assumption_ids(self) -> None:
        text = REGISTER.read_text(encoding="utf-8")
        matches = ROW.findall(text)
        expected = [f"ASM-{number:03d}" for number in range(1, 25)]
        self.assertEqual(expected, [match[0] for match in matches])
        self.assertTrue(all(match[3].strip() and match[4].strip() for match in matches))
        self.assertEqual(len(matches), len({match[0] for match in matches}))

    def test_src_03_records_limitations_and_review_triggers(self) -> None:
        text = REGISTER.read_text(encoding="utf-8")
        for required in (
            "Known local-v1 limitations",
            "not enterprise authentication",
            "not universal exactly-once delivery",
            "No production deployment",
            "Review rule",
        ):
            self.assertIn(required, text)
        self.assertNotRegex(text, re.compile(r"\b(?:TODO|TBD)\b", re.IGNORECASE))

    def test_src_03_every_adr_assumption_reference_resolves(self) -> None:
        known = {match[0] for match in ROW.findall(REGISTER.read_text(encoding="utf-8"))}
        referenced: set[str] = set()
        for path in sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
            referenced.update(re.findall(r"ASM-\d{3}", path.read_text(encoding="utf-8")))
        self.assertTrue(referenced)
        self.assertEqual(set(), referenced - known)


if __name__ == "__main__":
    unittest.main()
