"""OBJ-04 prepend a guard to a real Python module without changing its environment."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.acceptance.obj_04_guard import GuardRecorder, assert_clean_environment  # noqa: E402


def main() -> None:
    report, module, *arguments = sys.argv[1:]
    assert module.startswith("marketing_agents.workers.")
    assert_clean_environment(dict(os.environ), runtime=True)
    recorder = GuardRecorder(Path(report), module)
    recorder.install()
    sys.argv = [module, *arguments]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    finally:
        recorder.finish()


if __name__ == "__main__":
    main()
