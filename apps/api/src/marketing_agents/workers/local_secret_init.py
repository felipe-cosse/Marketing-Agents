"""Shared local-secret entrypoint for native commands and future container composition."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from marketing_agents.infrastructure.db.local_installation import initialize_local_secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketing-agents-local-secret")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--key-path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        asyncio.run(initialize_local_secret(args.database_url, args.key_path))
    except Exception:
        print(json.dumps({"ok": False, "code": "local_secret_invalid"}))
        return 1
    print(json.dumps({"ok": True, "code": "local_secret_verified"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
