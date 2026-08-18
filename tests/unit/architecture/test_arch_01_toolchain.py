import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class ToolchainTests(unittest.TestCase):
    """Requirement ARCH-01: the greenfield default backend stack is explicit and importable."""

    def test_arch_01_python_runtime_is_pinned_to_312(self) -> None:
        self.assertEqual("3.12", (ROOT / ".python-version").read_text(encoding="utf-8").strip())
        self.assertEqual((3, 12), sys.version_info[:2])

    def test_arch_01_declares_and_imports_required_frameworks(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(">=3.12,<3.13", project["requires-python"])
        declared = "\n".join(project["dependencies"]).lower()
        for dependency in ("fastapi", "pydantic", "sqlalchemy", "alembic"):
            self.assertIn(dependency, declared)

        import alembic  # noqa: F401
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
        import sqlalchemy  # noqa: F401


if __name__ == "__main__":
    unittest.main()
