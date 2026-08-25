"""Application service for restart-stable, source-key-idempotent work admission."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import WorkItem
from marketing_agents.domain.retention import RetentionCategory, RetentionPolicy
from marketing_agents.security.admission_digest import derive_admission_digests
from marketing_agents.security.digest_key import DigestKey

from .incoming_work_validation import ValidatedIncomingWork, _validated_parts

_INPUT_PROJECTION_INTEGRITY_DOMAIN = (
    b"marketing-agents:admitted-input-projection:hmac-sha256:v1\x00"
)


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


def _input_projection_integrity_digest(work_item: WorkItem, key: DigestKey) -> str:
    projection = {
        "work_id": work_item.id,
        "source": work_item.source,
        "event_id": work_item.event_id,
        "instance_id": work_item.instance_id,
        "trigger_id": work_item.trigger_id,
        "workflow_id": work_item.workflow_id,
        "mode": work_item.mode.value,
        "campaign_brief": (
            None
            if work_item.brief_id is None
            else {"id": work_item.brief_id, "revision": work_item.brief_revision}
        ),
        "configuration_revision": work_item.configuration_revision,
        "admitted_payload": work_item.admitted_payload,
        "input_digest": work_item.input_digest,
        "admission_digest": work_item.admission_digest,
        "digest_key_version": work_item.digest_key_version,
        "redacted_input_projection": work_item.redacted_input_projection,
        "input_schema_id": work_item.input_schema_id,
        "input_schema_hash": work_item.input_schema_hash,
        "input_classification": work_item.input_classification.value,
        "input_projection_created_at": work_item.input_projection_created_at.isoformat(),
        "input_projection_expires_at": work_item.input_projection_expires_at.isoformat(),
    }
    return hmac.new(
        key.bytes_for_digest(),
        _INPUT_PROJECTION_INTEGRITY_DOMAIN + canonical_json_bytes(projection),
        hashlib.sha256,
    ).hexdigest()


def _same_json(left: object, right: object) -> bool:
    return hmac.compare_digest(canonical_json_bytes(left), canonical_json_bytes(right))


class WorkAdmissionService:
    """Admit work without interpreting payload fields as routing instructions."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
        retention_policy: RetentionPolicy | None = None,
    ) -> None:
        self._dependencies = dependencies
        self._digest_key = digest_key
        self._retention_policy = retention_policy or RetentionPolicy()

    async def admit(self, incoming: ValidatedIncomingWork) -> WorkAdmissionResult:
        envelope, snapshot, redacted_projection, classification = _validated_parts(incoming)
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self._admit_envelope_in_uow(
                unit_of_work,
                envelope,
                input_schema_id=snapshot.input_schema_id,
                input_schema_hash=snapshot.input_schema_hash,
                redacted_input_projection=redacted_projection,
                input_classification=classification,
            )
            await unit_of_work.commit()
            return result

    async def admit_in_uow(
        self,
        unit_of_work: UnitOfWork,
        incoming: ValidatedIncomingWork,
    ) -> WorkAdmissionResult:
        """Insert or replay inside the caller transaction without committing it."""

        envelope, snapshot, redacted_projection, classification = _validated_parts(incoming)
        return await self._admit_envelope_in_uow(
            unit_of_work,
            envelope,
            input_schema_id=snapshot.input_schema_id,
            input_schema_hash=snapshot.input_schema_hash,
            redacted_input_projection=redacted_projection,
            input_classification=classification,
        )

    async def _admit_envelope_in_uow(
        self,
        unit_of_work: UnitOfWork,
        envelope: AdmissionEnvelope,
        *,
        input_schema_id: str,
        input_schema_hash: str,
        redacted_input_projection: Mapping[str, Any],
        input_classification: DataClassification,
    ) -> WorkAdmissionResult:
        digests = derive_admission_digests(envelope, self._digest_key)
        created_at = self._dependencies.utc_now()
        candidate_without_projection_digest = WorkItem(
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
            created_at=created_at,
            brief_revision=envelope.brief_revision,
            digest_key_version=digests.digest_key_version,
            admitted_payload=envelope.admitted_payload,
            redacted_input_projection=redacted_input_projection,
            input_schema_id=input_schema_id,
            input_schema_hash=input_schema_hash,
            input_classification=input_classification,
            input_projection_created_at=created_at,
            input_projection_expires_at=self._retention_policy.expires_at(
                RetentionCategory.ADMITTED_PAYLOAD,
                created_at,
                input_classification,
            ),
        )
        candidate = replace(
            candidate_without_projection_digest,
            input_projection_integrity_digest=_input_projection_integrity_digest(
                candidate_without_projection_digest,
                self._digest_key,
            ),
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
        expected_projection_digest = _input_projection_integrity_digest(
            existing,
            self._digest_key,
        )
        if not hmac.compare_digest(
            existing.input_projection_integrity_digest,
            expected_projection_digest,
        ):
            raise WorkIdempotencyError(
                "input_projection_integrity_mismatch",
                "existing work has an invalid admitted-input projection integrity binding",
                existing_work_item_id=existing.id,
            )
        if hmac.compare_digest(existing.input_digest, digests.input_digest) and hmac.compare_digest(
            existing.admission_digest, digests.admission_digest
        ):
            if (
                existing.input_schema_id != candidate.input_schema_id
                or existing.input_schema_hash != candidate.input_schema_hash
            ):
                raise WorkIdempotencyError(
                    "input_projection_schema_mismatch",
                    "existing work is bound to a different input schema identity",
                    existing_work_item_id=existing.id,
                )
            if (
                existing.input_classification is not candidate.input_classification
                or not _same_json(
                    existing.redacted_input_projection,
                    candidate.redacted_input_projection,
                )
            ):
                raise WorkIdempotencyError(
                    "input_projection_mismatch",
                    "existing work has a different redacted admitted-input projection",
                    existing_work_item_id=existing.id,
                )
            return WorkAdmissionResult(existing, AdmissionDisposition.REPLAYED)
        raise WorkIdempotencyError(
            "idempotency_conflict",
            "source event identity is already bound to different admitted work",
            existing_work_item_id=existing.id,
        )
