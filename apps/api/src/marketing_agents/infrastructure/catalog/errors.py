"""Safe, deterministic catalog compiler error types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class CatalogIssue:
    source_path: str
    json_pointer: str
    code: str
    message: str
    related_id: str | None = None


class CatalogCompilationError(ValueError):
    def __init__(self, issues: tuple[CatalogIssue, ...]) -> None:
        self.issues = tuple(sorted(issues))
        super().__init__(f"catalog compilation failed with {len(self.issues)} issue(s)")
