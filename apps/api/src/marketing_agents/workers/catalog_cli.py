"""Read-only command-line interface for catalog validation and compilation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from marketing_agents.infrastructure.catalog import compile_catalog, validate_catalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketing-agents-catalog")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--root", type=Path, default=Path("catalog/v1"))
    compile_command = subparsers.add_parser("compile")
    compile_command.add_argument("--root", type=Path, default=Path("catalog/v1"))
    compile_command.add_argument("--format", choices=["json"], default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        report = validate_catalog(args.root)
        if report.valid:
            print(json.dumps({"valid": True, "content_hash": report.content_hash}))
            return 0
        for issue in report.issues:
            print(
                json.dumps(
                    {
                        "code": issue.code,
                        "source_path": issue.source_path,
                        "json_pointer": issue.json_pointer,
                        "related_id": issue.related_id,
                        "message": issue.message,
                    },
                    sort_keys=True,
                )
            )
        return 1
    compiled = compile_catalog(args.root)
    print(
        json.dumps(
            {
                "content_hash": compiled.content_hash,
                "counts": {
                    "departments": len(compiled.departments),
                    "functions": len(compiled.functions),
                    "templates": len(compiled.templates),
                    "instances": len(compiled.instances),
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
