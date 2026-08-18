"""Injected stable ID generator port."""

from typing import Protocol


class IdGenerator(Protocol):
    def new(self, namespace: str) -> str:
        """Return a new stable ID within the requested namespace."""
        ...
