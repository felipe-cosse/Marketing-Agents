#!/usr/bin/env python3
"""Compatibility entry point for the topology-aware requirement validator."""

from __future__ import annotations

import argparse

from verify_requirement_evidence import main as evidence_main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default="main")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--check-branches", action="store_true")
    args = parser.parse_args()
    forwarded = ["history", "--ref", args.ref]
    if args.allow_incomplete:
        forwarded.append("--allow-incomplete")
    if args.check_branches:
        forwarded.append("--check-branches")
    return evidence_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
