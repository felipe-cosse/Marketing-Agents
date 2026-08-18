"""Application service for restart-stable, source-key-idempotent work admission."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.entities import WorkItem
from marketing_agents.security.admission_digest import derive_admission_digests
from marketing_agents.security.digest_key import DigestKey

from .incoming_work_validation import ValidatedIncomingWork, _validated_envelope


class WorkIdempotencyError(RuntimeError):
    """Safe stable failure for a source-key collision or key-version mismatch."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        existing_work_item_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.existing_work_item_id = existing_work_item_id


class AdmissionDisposition(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class WorkAdmissionResult:
    work_item: WorkItem
    disposition: AdmissionDisposition


class WorkAdmissionService:
    """Admit work without interpreting payload fields as routing instructions."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
    ) -> None:
        self._dependencies = dependencies
        self._digest_key = digest_key

    async def admit(self, incoming: ValidatedIncomingWork) -> WorkAdmissionResult:
        envelope = _validated_envelope(incoming)
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self._admit_envelope_in_uow(unit_of_work, envelope)
            await unit_of_work.commit()
            return result

    async def admit_in_uow(
        self,
        unit_of_work: UnitOfWork,
        incoming: ValidatedIncomingWork,
    ) -> WorkAdmissionResult:
        """Insert or replay inside the caller transaction without committing it."""

        envelope = _validated_envelope(incoming)
        return await self._admit_envelope_in_uow(unit_of_work, envelope)

    async def _admit_envelope_in_uow(
        self,
        unit_of_work: UnitOfWork,
        envelope: AdmissionEnvelope,
    ) -> WorkAdmissionResult:
        digests = derive_admission_digests(envelope, self._digest_key)
        candidate = WorkItem(
            id=self._dependencies.new_id("work"),
            source=envelope.source,
            event_id=envelope.event_id,
            instance_id=envelope.instance_id,
            trigger_id=envelope.trigger_id,
            workflow_id=envelope.workflow_id,
            mode=envelope.mode,
            brief_id=envelope.brief_id,
            configuration_revision=envelope.configuration_revision,
            input_digest=digests.input_digest,
            admission_digest=digests.admission_digest,
            created_at=self._dependencies.utc_now(),
            brief_revision=envelope.brief_revision,
            digest_key_version=digests.digest_key_version,
            admitted_payload=envelope.admitted_payload,
        )
        stored = await unit_of_work.works.add_or_get(candidate)
        if stored.inserted:
            return WorkAdmissionResult(stored.work_item, AdmissionDisposition.CREATED)

        existing = stored.work_item
        if existing.digest_key_version != digests.digest_key_version:
            raise WorkIdempotencyError(
                "digest_key_version_mismatch",
                "existing work was admitted under a different digest key version",
                existing_work_item_id=existing.id,
            )
        if hmac.compare_digest(existing.input_digest, digests.input_digest) and hmac.compare_digest(
            existing.admission_digest, digests.admission_digest
        ):
            return WorkAdmissionResult(existing, AdmissionDisposition.REPLAYED)
        raise WorkIdempotencyError(
            "idempotency_conflict",
            "source event identity is already bound to different admitted work",
            existing_work_item_id=existing.id,
        )
