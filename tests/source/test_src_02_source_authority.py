import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = ROOT / "catalog" / "source-evidence.json"


class SourceAuthorityTests(unittest.TestCase):
    """Requirement SRC-02: source evidence has a narrow, testable authority boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))

    def test_src_02_indexes_all_six_immutable_local_frames(self) -> None:
        assets = self.index["assets"]
        self.assertEqual(6, len(assets))
        self.assertEqual(6, len({asset["path"] for asset in assets}))

        for asset in assets:
            path = ROOT / asset["path"]
            self.assertTrue(path.is_file(), asset["path"])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(asset["sha256"], digest, asset["path"])

    def test_src_02_rejects_hidden_integration_and_duplicate_claims(self) -> None:
        boundary = self.index["authority"]
        excluded = set(boundary["non_authoritative"])
        self.assertIn("exact third-party integrations", excluded)
        self.assertIn("reasons for duplicated Community cards", excluded)

        duplicates = self.index["community_duplicates"]
        self.assertEqual(7, duplicates["template_count"])
        self.assertEqual(14, duplicates["instance_count"])
        self.assertIsNone(duplicates["business_reason"])
        self.assertIsNone(duplicates["variant_label"])


if __name__ == "__main__":
    unittest.main()
