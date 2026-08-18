import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ValueError(f"not a PNG with IHDR: {path}")
    return struct.unpack(">II", payload[16:24])


class DesignEvidenceTests(unittest.TestCase):
    """Requirement SRC-01: accepted desktop/mobile concepts and source corrections exist."""

    def test_src_01_has_desktop_and_mobile_concepts(self) -> None:
        concept_root = ROOT / "docs" / "design" / "concepts"
        self.assertEqual((1536, 1024), png_dimensions(concept_root / "marketing-agents-desktop.png"))
        self.assertEqual((852, 1846), png_dimensions(concept_root / "marketing-agents-mobile.png"))

    def test_src_01_design_spec_records_authoritative_corrections(self) -> None:
        text = (ROOT / "docs" / "design" / "README.md").read_text(encoding="utf-8")
        for expected in ("36 templates", "43 deployed instances", "seven shared templates", "vendor-neutral", "approval"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
