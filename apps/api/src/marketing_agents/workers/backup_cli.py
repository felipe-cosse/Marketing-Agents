"""Explicit local SQLite backup/restore commands with sanitized JSON diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from marketing_agents.config import Settings
from marketing_agents.infrastructure.db.local_backup import (
    LocalBackupError,
    backup_local_installation,
    restore_local_installation,
)
from marketing_agents.infrastructure.db.migrations import DatabaseMigrationError
from marketing_agents.security.digest_key import DigestKeyError


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "backup":
        settings = Settings()
        manifest = await backup_local_installation(
            args.database_url or settings.database_url,
            args.key_path or settings.marketing_agents_digest_key_path,
            args.destination,
        )
    else:
        manifest = await restore_local_installation(args.backup, args.destination)
    return {
        "code": f"local_{args.command}_complete",
        "schema_revision": manifest.schema_revision,
        "secret_bearing": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketing-agents-backup")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--database-url")
    backup.add_argument("--key-path", type=Path)
    backup.add_argument("--destination", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except (LocalBackupError, DatabaseMigrationError) as exc:
        code = exc.code
    except DigestKeyError:
        code = "local_secret_invalid"
    except FileNotFoundError:
        code = "local_backup_component_missing"
    except Exception:
        code = "local_backup_command_failed"
    else:
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    print(json.dumps({"ok": False, "code": code}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
