"""SQLAlchemy primary-Run repository with append-only optimistic transitions."""

from __future__ import annotations

import sqlite3

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import RunInsertResult
from marketing_agents.domain.entities import Run
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.run_lifecycle import (
    RunLifecycleCommand,
    RunStateTransition,
    RunTransitionResult,
    initial_received_transition,
)
from marketing_agents.infrastructure.db.models.run import RunRecord, RunStateTransitionRecord


class RunPersistenceInvariantError(RuntimeError):
    """Raised when durable Run state and transition history disagree."""


def _run_to_record(run: Run) -> RunRecord:
    return RunRecord(
        id=run.id,
        work_item_id=run.work_item_id,
        state=run.state.value,
        catalog_hash=run.catalog_hash,
        configuration_revision=run.configuration_revision,
        approval_required=run.approval_required,
        terminal_reason_code=run.terminal_reason_code,
        created_at=run.created_at,
        updated_at=run.updated_at,
        version=run.version,
    )


def _run_to_domain(record: RunRecord) -> Run:
    return Run(
        id=record.id,
        work_item_id=record.work_item_id,
        state=RunState(record.state),
        catalog_hash=record.catalog_hash,
        configuration_revision=record.configuration_revision,
        created_at=record.created_at,
        version=record.version,
        updated_at=record.updated_at,
        approval_required=record.approval_required,
        terminal_reason_code=record.terminal_reason_code,
    )


def _transition_to_record(transition: RunStateTransition) -> RunStateTransitionRecord:
    return RunStateTransitionRecord(
        run_id=transition.run_id,
        sequence=transition.sequence,
        command=transition.command.value,
        previous_state=(
            None if transition.previous_state is None else transition.previous_state.value
        ),
        new_state=transition.new_state.value,
        reason_code=transition.reason_code,
        occurred_at=transition.occurred_at,
        expected_version=transition.expected_version,
        resulting_version=transition.resulting_version,
        completed_effect_count=transition.completed_effect_count,
        outcome_unknown_effect_count=transition.outcome_unknown_effect_count,
    )


def _transition_to_domain(record: RunStateTransitionRecord) -> RunStateTransition:
    return RunStateTransition(
        run_id=record.run_id,
        sequence=record.sequence,
        command=RunLifecycleCommand(record.command),
        previous_state=(None if record.previous_state is None else RunState(record.previous_state)),
        new_state=RunState(record.new_state),
        reason_code=record.reason_code,
        occurred_at=record.occurred_at,
        expected_version=record.expected_version,
        resulting_version=record.resulting_version,
        completed_effect_count=record.completed_effect_count,
        outcome_unknown_effect_count=record.outcome_unknown_effect_count,
    )


class SQLAlchemyRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: str) -> Run | None:
        record = await self._session.get(RunRecord, run_id)
        return None if record is None else _run_to_domain(record)

    async def get_by_work_item_id(self, work_item_id: str) -> Run | None:
        statement = select(RunRecord).where(RunRecord.work_item_id == work_item_id)
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else _run_to_domain(record)

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult:
        if initial_transition != initial_received_transition(run):
            raise ValueError("primary Run insertion requires its initial received transition")
        try:
            async with self._session.begin_nested():
                self._session.add(_run_to_record(run))
                self._session.add(_transition_to_record(initial_transition))
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_by_work_item_id(run.work_item_id)
            if existing is None:
                raise
            history = await self.list_transitions(existing.id)
            if not history or history[0].sequence != 1:
                raise RunPersistenceInvariantError(
                    "existing primary Run has no initial received transition"
                ) from None
            return RunInsertResult(existing, inserted=False)
        return RunInsertResult(run, inserted=True)

    async def fence(
        self,
        *,
        run_id: str,
        expected_version: int,
        expected_state: RunState,
    ) -> bool:
        statement = (
            update(RunRecord)
            .where(
                RunRecord.id == run_id,
                RunRecord.version == expected_version,
                RunRecord.state == expected_state.value,
            )
            .values(version=RunRecord.version)
            .returning(RunRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            return (await self._session.execute(statement)).scalar_one_or_none() is not None
        except OperationalError as exc:
            sqlite_error_code = getattr(exc.orig, "sqlite_errorcode", None)
            if self._session.get_bind().dialect.name == "sqlite" and sqlite_error_code in {
                sqlite3.SQLITE_BUSY,
                getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
            }:
                return False
            raise

    async def apply_transition(
        self,
        *,
        expected_version: int,
        expected_state: RunState,
        result: RunTransitionResult,
    ) -> bool:
        transition = result.transition
        if (
            transition.expected_version != expected_version
            or transition.previous_state is not expected_state
            or result.run.version != transition.resulting_version
            or result.run.state is not transition.new_state
        ):
            raise ValueError("transition result does not match its CAS predicate")
        statement = (
            update(RunRecord)
            .where(
                RunRecord.id == result.run.id,
                RunRecord.version == expected_version,
                RunRecord.state == expected_state.value,
            )
            .values(
                state=result.run.state.value,
                approval_required=result.run.approval_required,
                terminal_reason_code=result.run.terminal_reason_code,
                updated_at=result.run.updated_at,
                version=result.run.version,
            )
            .returning(RunRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            update_result = await self._session.execute(statement)
        except OperationalError as exc:
            sqlite_error_code = getattr(exc.orig, "sqlite_errorcode", None)
            sqlite_busy_codes = {
                sqlite3.SQLITE_BUSY,
                getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
            }
            if (
                self._session.get_bind().dialect.name == "sqlite"
                and sqlite_error_code in sqlite_busy_codes
            ):
                # A concurrent writer can invalidate this connection's WAL read
                # snapshot before SQLite can evaluate the CAS predicate. It is the
                # same lost-race outcome as a zero-row conditional update.
                return False
            raise
        if update_result.scalar_one_or_none() is None:
            return False
        self._session.add(_transition_to_record(transition))
        await self._session.flush()
        return True

    async def list_transitions(self, run_id: str) -> tuple[RunStateTransition, ...]:
        statement = (
            select(RunStateTransitionRecord)
            .where(RunStateTransitionRecord.run_id == run_id)
            .order_by(RunStateTransitionRecord.sequence)
        )
        records = (await self._session.execute(statement)).scalars()
        return tuple(_transition_to_domain(record) for record in records)
