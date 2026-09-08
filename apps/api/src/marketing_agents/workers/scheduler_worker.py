"""Executable durable scheduler worker."""

from .process import main

if __name__ == "__main__":
    raise SystemExit(main("scheduler"))
