"""Explicit native migrations and repeatable catalog seed/check with safe JSON output."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from marketing_agents.config import Settings
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import CatalogSeedError, seed_catalog
from marketing_agents.infrastructure.db.local_installation import (
    migrate_local_database,
    verify_local_installation,
)
from marketing_agents.infrastructure.db.migrations import DatabaseMigrationError
from marketing_agents.infrastructure.db.session import create_database_runtime
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import DigestKeyError


async def _run(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    database_url = args.database_url or settings.database_url
    key_path = args.key_path or settings.marketing_agents_digest_key_path
    if args.command == "migrate":
        return {"revision": await migrate_local_database(database_url, key_path)}
    catalog = compile_catalog(args.root or settings.catalog_root)
    await verify_local_installation(database_url, key_path)
    runtime = create_database_runtime(database_url)
    try:
        result = await seed_catalog(
            catalog, runtime, CroniterRecurrenceCalculator(), check=args.check
        )
        return {"check": args.check, **asdict(result)}
    finally:
        await runtime.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketing-agents-db")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "seed"):
        command = commands.add_parser(name)
        command.add_argument("--database-url")
        command.add_argument("--key-path", type=Path)
        if name == "seed":
            command.add_argument("--root", type=Path)
            command.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except (CatalogSeedError, DatabaseMigrationError) as exc:
        code = exc.code
    except DigestKeyError:
        code = "local_secret_invalid"
    except Exception:
        code = "database_command_failed"
    else:
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    print(json.dumps({"ok": False, "code": code}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
