import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_source_evidence.py"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_source_evidence", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load source-evidence verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SourceInspectionTests(unittest.TestCase):
    """Requirement EXEC-01: local guidance, prompt, and frames are inventoried."""

    def test_exec_01_verifies_the_complete_local_evidence_set(self) -> None:
        module = load_module()
        result = module.verify(ROOT)
        self.assertEqual(6, result.frame_count)
        self.assertEqual(8, result.inspected_subject_count)


if __name__ == "__main__":
    unittest.main()
