"""Deterministic execution-control repository wiring for persistence tests."""

from __future__ import annotations

from marketing_agents.infrastructure.db import SQLAlchemyExecutionControlRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy.ext.asyncio import AsyncSession

TEST_EXECUTION_CONTROL_KEY = DigestKey(bytes(reversed(range(32))))


def execution_control_repository(
    session: AsyncSession,
) -> SQLAlchemyExecutionControlRepository:
    return SQLAlchemyExecutionControlRepository(session, TEST_EXECUTION_CONTROL_KEY)


__all__ = ["TEST_EXECUTION_CONTROL_KEY", "execution_control_repository"]
