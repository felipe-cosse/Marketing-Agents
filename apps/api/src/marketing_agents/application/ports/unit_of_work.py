"""Transaction boundary port shared by all state-changing application services."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

from marketing_agents.application.ports.instance_configuration import (
    InstanceConfigurationRepository,
)
from marketing_agents.application.ports.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AuditRepository,
    ConnectorReceiptRepository,
    ExecutionControlRepository,
    ExternalActionRepository,
    RunRepository,
    RunStepRepository,
    ScheduleRepository,
    WebhookReceiptRepository,
    WorkRepository,
)


class UnitOfWork(Protocol):
    @property
    def works(self) -> WorkRepository: ...

    @property
    def webhook_receipts(self) -> WebhookReceiptRepository: ...

    @property
    def configurations(self) -> InstanceConfigurationRepository: ...

    @property
    def runs(self) -> RunRepository: ...

    @property
    def audits(self) -> AuditRepository: ...

    @property
    def artifacts(self) -> ArtifactRepository: ...

    @property
    def approvals(self) -> ApprovalRepository: ...

    @property
    def run_steps(self) -> RunStepRepository: ...

    @property
    def external_actions(self) -> ExternalActionRepository: ...

    @property
    def connector_receipts(self) -> ConnectorReceiptRepository: ...

    @property
    def execution_control(self) -> ExecutionControlRepository: ...

    @property
    def schedules(self) -> ScheduleRepository: ...

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
