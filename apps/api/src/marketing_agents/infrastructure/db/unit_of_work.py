"""SQLAlchemy transaction adapter with injected domain repository implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from marketing_agents.application.ports.repositories import (
    AuditRepository,
    RunRepository,
    WorkRepository,
)


class SQLAlchemyUnitOfWorkError(RuntimeError):
    """Raised when the infrastructure transaction boundary is used out of order."""


@dataclass(frozen=True, slots=True)
class RepositoryBundle:
    works: WorkRepository
    runs: RunRepository
    audits: AuditRepository


@dataclass(frozen=True, slots=True)
class SQLAlchemyRepositoryFactories:
    works: Callable[[AsyncSession], WorkRepository]
    runs: Callable[[AsyncSession], RunRepository]
    audits: Callable[[AsyncSession], AuditRepository]

    def build(self, session: AsyncSession) -> RepositoryBundle:
        return RepositoryBundle(
            works=self.works(session),
            runs=self.runs(session),
            audits=self.audits(session),
        )


class SQLAlchemyUnitOfWork:
    """Fail-closed explicit-commit unit of work backed by one async session."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository_factories: SQLAlchemyRepositoryFactories,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factories = repository_factories
        self._session: AsyncSession | None = None
        self._repositories: RepositoryBundle | None = None
        self._finished = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise SQLAlchemyUnitOfWorkError("unit of work has not been entered")
        return self._session

    def _require_repositories(self) -> RepositoryBundle:
        if self._repositories is None:
            raise SQLAlchemyUnitOfWorkError("unit of work has not been entered")
        return self._repositories

    @property
    def works(self) -> WorkRepository:
        return self._require_repositories().works

    @property
    def runs(self) -> RunRepository:
        return self._require_repositories().runs

    @property
    def audits(self) -> AuditRepository:
        return self._require_repositories().audits

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        if self._session is not None:
            raise SQLAlchemyUnitOfWorkError("unit of work cannot be entered more than once")
        self._session = self._session_factory()
        await self._session.begin()
        self._repositories = self._repository_factories.build(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        session = self._require_session()
        try:
            if not self._finished:
                await session.rollback()
        finally:
            await session.close()

    async def commit(self) -> None:
        session = self._require_session()
        if self._finished:
            raise SQLAlchemyUnitOfWorkError("unit of work transaction is already finished")
        await session.commit()
        self._finished = True

    async def rollback(self) -> None:
        session = self._require_session()
        if self._finished:
            raise SQLAlchemyUnitOfWorkError("unit of work transaction is already finished")
        await session.rollback()
        self._finished = True


@dataclass(frozen=True, slots=True)
class SQLAlchemyUnitOfWorkFactory:
    session_factory: async_sessionmaker[AsyncSession]
    repository_factories: SQLAlchemyRepositoryFactories

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(self.session_factory, self.repository_factories)
