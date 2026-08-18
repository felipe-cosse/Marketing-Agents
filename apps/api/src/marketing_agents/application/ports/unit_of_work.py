"""Transaction boundary port shared by all state-changing application services."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

from marketing_agents.application.ports.repositories import (
    AuditRepository,
    RunRepository,
    WorkRepository,
)


class UnitOfWork(Protocol):
    @property
    def works(self) -> WorkRepository: ...

    @property
    def runs(self) -> RunRepository: ...

    @property
    def audits(self) -> AuditRepository: ...

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
