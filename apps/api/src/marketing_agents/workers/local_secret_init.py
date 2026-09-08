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
    parser.add_argument(
        "--defer-database-check",
        action="store_true",
        help="For the read-only SQLite initializer only; migration must verify the pair next",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            initialize_local_secret(
                args.database_url, args.key_path, defer_database_check=args.defer_database_check
            )
        )
    except Exception:
        print(json.dumps({"ok": False, "code": "local_secret_invalid"}))
        return 1
    result: dict[str, object] = {"ok": True, "code": "local_secret_verified"}
    if args.defer_database_check:
        result["database_check"] = "deferred"
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
