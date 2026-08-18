import importlib.util
import json
import shutil
import sys
import tempfile
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

    def _copy_evidence_root(self, destination: Path) -> None:
        (destination / "catalog").mkdir(parents=True)
        (destination / "docs" / "implementation").mkdir(parents=True)
        shutil.copy2(ROOT / "IMPLEMENTATION_PROMPT.md", destination / "IMPLEMENTATION_PROMPT.md")
        shutil.copy2(ROOT / "catalog" / "source-evidence.json", destination / "catalog" / "source-evidence.json")
        shutil.copy2(
            ROOT / "docs" / "implementation" / "repository-inspection.json",
            destination / "docs" / "implementation" / "repository-inspection.json",
        )
        shutil.copytree(ROOT / "references", destination / "references")

    def _mutated_root(self, mutate) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._copy_evidence_root(root)
            mutate(root)
            with self.assertRaises(ValueError):
                module.verify(root)

    def test_exec_01_rejects_altered_frame_content(self) -> None:
        def mutate(root: Path) -> None:
            path = root / "references" / "linkedin-ai-agents-org-chart-overview.png"
            path.write_bytes(path.read_bytes() + b"altered")

        self._mutated_root(mutate)

    def test_exec_01_rejects_duplicate_or_reordered_subjects(self) -> None:
        def mutate(root: Path) -> None:
            path = root / "docs" / "implementation" / "repository-inspection.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["inspected_subjects"][-1] = value["inspected_subjects"][-2]
            path.write_text(json.dumps(value), encoding="utf-8")

        self._mutated_root(mutate)

    def test_exec_01_rejects_unrecorded_guidance(self) -> None:
        self._mutated_root(lambda root: (root / "AGENTS.md").write_text("# Guidance\n", encoding="utf-8"))

    def test_exec_01_rejects_configurable_prompt_or_index_paths(self) -> None:
        def mutate(root: Path) -> None:
            path = root / "docs" / "implementation" / "repository-inspection.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["required_prompt"] = "docs/other.md"
            value["reference_index"] = "catalog/other.json"
            path.write_text(json.dumps(value), encoding="utf-8")

        self._mutated_root(mutate)


if __name__ == "__main__":
    unittest.main()
