"""Read a bounded local worker heartbeat and verify its owning process lives."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def healthy(path: Path, *, max_age_seconds: float = 90) -> bool:
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload["pid"]
        age = time.time() - payload["updated_at"]
        if type(pid) is not int or pid < 1 or payload["status"] != "ready":
            return False
        if not 0 <= age <= max_age_seconds:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a local worker heartbeat")
    parser.add_argument("--health-file", required=True, type=Path)
    args = parser.parse_args()
    return 0 if healthy(args.health_file) else 1


if __name__ == "__main__":
    raise SystemExit(main())
