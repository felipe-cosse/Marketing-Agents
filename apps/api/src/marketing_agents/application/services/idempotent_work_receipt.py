"""Atomic idempotent receipt of one admitted WorkItem and its primary Run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.entities._validation import require_digest
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.run_lifecycle import (
    RunLifecycleCommand,
    RunStateTransition,
    initial_received_transition,
)
from marketing_agents.security.digest_key import DigestKey

from .incoming_work_validation import (
    IncomingWorkValidationError,
    ValidatedIncomingWork,
    _validated_snapshot,
)
from .run_lifecycle import ReceiveRunRequest, RunLifecycleService
from .work_admission import AdmissionDisposition, WorkAdmissionService


class WorkRunReceiptError(RuntimeError):
    """Fail-closed error for a mixed or corrupted WorkItem/Run receipt."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        work_item_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.work_item_id = work_item_id
        self.run_id = run_id


class WorkRunReceiptDisposition(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class WorkRunReceiptResult:
    work_item: WorkItem
    run: Run
    disposition: WorkRunReceiptDisposition
    initial_transition: RunStateTransition | None


def _expected_initial_transition(run: Run) -> RunStateTransition:
    received = Run(
        id=run.id,
        work_item_id=run.work_item_id,
        state=RunState.RECEIVED,
        catalog_hash=run.catalog_hash,
        configuration_revision=run.configuration_revision,
        created_at=run.created_at,
        version=1,
        updated_at=run.created_at,
    )
    expected = initial_received_transition(received)
    if expected.command is not RunLifecycleCommand.RECEIVE:  # pragma: no cover
        raise AssertionError("initial transition helper returned a non-receive command")
    return expected


class IdempotentWorkRunReceiptService:
    """Commit a WorkItem, primary Run, and initial transition as one receipt."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
        *,
        current_catalog_hash: str,
    ) -> None:
        if not current_catalog_hash.startswith("catalog-sha256-v1:"):
            raise ValueError("current catalog hash version is invalid")
        require_digest(
            current_catalog_hash.removeprefix("catalog-sha256-v1:"),
            "current catalog hash",
        )
        self._dependencies = dependencies
        self._current_catalog_hash = current_catalog_hash
        self._work_admission = WorkAdmissionService(dependencies, digest_key)
        self._run_lifecycle = RunLifecycleService(dependencies)

    async def receive(
        self,
        incoming: ValidatedIncomingWork,
        *,
        audit_context: AuditContext,
    ) -> WorkRunReceiptResult:
        """Create or replay the authoritative pair without accepting caller IDs or time."""

        self._require_current_snapshot(incoming)
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self._receive_in_uow(
                unit_of_work,
                incoming,
                audit_context=audit_context,
            )
            await unit_of_work.commit()
            return result

    async def receive_in_uow(
        self,
        unit_of_work: UnitOfWork,
        incoming: ValidatedIncomingWork,
        *,
        audit_context: AuditContext,
    ) -> WorkRunReceiptResult:
        """Create or replay inside a caller-owned transaction without committing it."""

        self._require_current_snapshot(incoming)
        return await self._receive_in_uow(
            unit_of_work,
            incoming,
            audit_context=audit_context,
        )

    def _require_current_snapshot(self, incoming: ValidatedIncomingWork) -> None:
        snapshot = _validated_snapshot(incoming)
        if snapshot.catalog_hash != self._current_catalog_hash:
            raise IncomingWorkValidationError(
                "catalog_drift",
                "incoming work was not validated against the current catalog release",
            )

    async def _receive_in_uow(
        self,
        unit_of_work: UnitOfWork,
        incoming: ValidatedIncomingWork,
        *,
        audit_context: AuditContext,
    ) -> WorkRunReceiptResult:
        snapshot = _validated_snapshot(incoming)
        admitted = await self._work_admission.admit_in_uow(unit_of_work, incoming)
        received = await self._run_lifecycle.receive_in_uow(
            unit_of_work,
            ReceiveRunRequest(
                work_item=admitted.work_item,
                catalog_hash=snapshot.catalog_hash,
            ),
            audit_context=audit_context,
        )
        work_created = admitted.disposition is AdmissionDisposition.CREATED
        if work_created != received.created:
            raise WorkRunReceiptError(
                "mixed_receipt_disposition",
                "WorkItem and primary Run must be created or replayed together",
                work_item_id=admitted.work_item.id,
                run_id=received.run.id,
            )
        if (
            received.run.work_item_id != admitted.work_item.id
            or received.run.configuration_revision != admitted.work_item.configuration_revision
            or (received.created and received.run.catalog_hash != snapshot.catalog_hash)
        ):
            raise WorkRunReceiptError(
                "receipt_correlation_mismatch",
                "primary Run does not bind the authoritative admitted WorkItem",
                work_item_id=admitted.work_item.id,
                run_id=received.run.id,
            )

        expected_initial = _expected_initial_transition(received.run)
        if received.created != (received.initial_transition is not None):
            raise WorkRunReceiptError(
                "initial_transition_disposition_mismatch",
                "primary Run creation and initial transition disposition disagree",
                work_item_id=admitted.work_item.id,
                run_id=received.run.id,
            )
        if (
            received.initial_transition is not None
            and received.initial_transition != expected_initial
        ):
            raise WorkRunReceiptError(
                "initial_transition_mismatch",
                "new primary Run does not retain its exact initial transition",
                work_item_id=admitted.work_item.id,
                run_id=received.run.id,
            )
        history = await unit_of_work.runs.list_transitions(received.run.id)
        if not history or history[0] != expected_initial:
            raise WorkRunReceiptError(
                "initial_transition_missing",
                "primary Run lacks its contiguous initial received transition",
                work_item_id=admitted.work_item.id,
                run_id=received.run.id,
            )
        if received.created and history != (expected_initial,):
            raise WorkRunReceiptError(
                "initial_transition_not_contiguous",
                "new primary Run receipt contains unexpected transition history",
                work_item_id=admitted.work_item.id,
                run_id=received.run.id,
            )

        return WorkRunReceiptResult(
            work_item=admitted.work_item,
            run=received.run,
            disposition=(
                WorkRunReceiptDisposition.CREATED
                if work_created
                else WorkRunReceiptDisposition.REPLAYED
            ),
            initial_transition=received.initial_transition,
        )
