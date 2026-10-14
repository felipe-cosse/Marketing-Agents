"""Scope redaction counting to the current request or worker advancement."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar

redaction_observer: ContextVar[Callable[[str], None] | None] = ContextVar(
    "local_redaction_observer", default=None
)


def observe_redaction(boundary: str) -> None:
    observer = redaction_observer.get()
    if observer is not None:
        with suppress(Exception):
            observer(boundary)
