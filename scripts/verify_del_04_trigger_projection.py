#!/usr/bin/env python3
"""DEL-04 causal gate: load this checkout's production source with installed test tools."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The evidence witness is a Git archive without a virtualenv. Resolve application
# imports from that archive, not an editable install pointing at the real checkout.
# Invoke with the bootstrapped Python environment on PATH; never install during a gate.
sys.path[:0] = [str(ROOT / "apps/api/src"), str(ROOT)]

import pytest  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [
                "-q",
                "-x",
                "--disable-socket",
                "--allow-unix-socket",
                str(ROOT / "tests/integration/db/test_del_04_trigger_projection.py"),
            ]
        )
    )
