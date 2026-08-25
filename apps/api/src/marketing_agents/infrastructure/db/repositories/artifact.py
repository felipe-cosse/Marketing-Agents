"""Fail-closed immutable artifact persistence and lineage hydration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    ArtifactInsertResult,
    ArtifactRepositoryConflict,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.provenance import ArtifactEnvelope
from marketing_agents.infrastructure.db.models.artifact import (
    ArtifactParentRecord,
    ArtifactRecord,
)
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.step import (
    RunPlanRecord,
    RunStepDependencyRecord,
    RunStepRecord,
)
from marketing_agents.infrastructure.db.models.work import WorkItemRecord

_ARTIFACT_FINGERPRINT_DOMAIN = b"marketing-agents:artifact-envelope:persistence:v1\x00"


class ArtifactPersistenceConflict(ArtifactRepositoryConflict):
    """Infrastructure-specific artifact conflict with a stable application code."""


def _plain_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    decoded = json.loads(canonical_json_bytes(value))
    if not isinstance(decoded, dict):  # pragma: no cover - Mapping guarantees this shape
        raise ArtifactPersistenceConflict(
            "artifact_invalid", "artifact persistence requires one JSON object"
        )
    return cast(dict[str, Any], decoded)


def _envelope_snapshot(artifact: ArtifactEnvelope) -> dict[str, Any]:
    return _plain_mapping(artifact.model_dump(mode="json"))


def _fingerprint(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(_ARTIFACT_FINGERPRINT_DOMAIN + canonical_json_bytes(snapshot)).hexdigest()


def _catalog_digest(value: str) -> str:
    return value.removeprefix("catalog-sha256-v1:")


def _workflow_version_matches(value: str, expected: int) -> bool:
    return value in {str(expected), f"v{expected}"}


def _to_record(artifact: ArtifactEnvelope) -> ArtifactRecord:
    snapshot = _envelope_snapshot(artifact)
    provenance = artifact.provenance
    return ArtifactRecord(
        id=provenance.artifact_id,
        work_item_id=provenance.work_item_id,
        run_id=provenance.run_id,
        step_id=provenance.step_id,
        output_schema_id=provenance.output_schema_id,
        output_schema_version=provenance.output_schema_version,
        output_schema_hash=provenance.output_schema_hash,
        configuration_revision=provenance.instance_config_revision,
        classification=provenance.classification.value,
        payload=_plain_mapping(artifact.payload),
        payload_hash=provenance.payload_hash,
        provenance_snapshot=cast(dict[str, Any], snapshot["provenance"]),
        envelope_fingerprint=_fingerprint(snapshot),
        created_at=provenance.created_at,
    )


def _to_domain(
    record: ArtifactRecord,
    parent_artifact_ids: tuple[str, ...],
) -> ArtifactEnvelope:
    snapshot = {
        "payload": record.payload,
        "provenance": record.provenance_snapshot,
    }
    try:
        observed_fingerprint = _fingerprint(snapshot)
    except (TypeError, ValueError) as exc:
        raise ArtifactPersistenceConflict(
            "artifact_tampered", "persisted artifact is not canonical JSON"
        ) from exc
    if observed_fingerprint != record.envelope_fingerprint:
        raise ArtifactPersistenceConflict(
            "artifact_tampered", "persisted artifact envelope fingerprint does not match"
        )
    try:
        artifact = ArtifactEnvelope.model_validate_json(canonical_json_bytes(snapshot))
    except (TypeError, ValueError, ValidationError) as exc:
        raise ArtifactPersistenceConflict(
            "artifact_tampered", "persisted artifact envelope is invalid"
        ) from exc
    provenance = artifact.provenance
    if (
        not artifact.verify_payload()
        or record.id != provenance.artifact_id
        or record.work_item_id != provenance.work_item_id
        or record.run_id != provenance.run_id
        or record.step_id != provenance.step_id
        or record.output_schema_id != provenance.output_schema_id
        or record.output_schema_version != provenance.output_schema_version
        or record.output_schema_hash != provenance.output_schema_hash
        or record.configuration_revision != provenance.instance_config_revision
        or record.classification != provenance.classification.value
        or record.payload_hash != provenance.payload_hash
        or record.created_at != provenance.created_at
        or parent_artifact_ids != provenance.parent_artifact_ids
    ):
        raise ArtifactPersistenceConflict(
            "artifact_tampered", "persisted artifact columns disagree with its envelope"
        )
    return artifact


class SQLAlchemyArtifactRepository:
    """Append-only artifact repository with exact replay and verified hydration."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, artifact_id: str) -> ArtifactEnvelope | None:
        record = await self._session.get(ArtifactRecord, artifact_id)
        if record is None:
            return None
        return await self._hydrate(record)

    async def list_for_run(self, run_id: str) -> tuple[ArtifactEnvelope, ...]:
        statement = (
            select(ArtifactRecord)
            .where(ArtifactRecord.run_id == run_id)
            .order_by(ArtifactRecord.created_at, ArtifactRecord.id)
        )
        records = tuple((await self._session.execute(statement)).scalars())
        return tuple([await self._hydrate(record) for record in records])

    async def add_or_get(self, artifact: ArtifactEnvelope) -> ArtifactInsertResult:
        if type(artifact) is not ArtifactEnvelope or not artifact.verify_payload():
            raise ArtifactPersistenceConflict(
                "artifact_invalid", "artifact payload does not match its provenance hash"
            )
        await self._validate_runtime_scope(artifact)
        parents = await self._load_parents(artifact.provenance.parent_artifact_ids)
        await self._validate_parent_lineage(artifact, parents)

        record = _to_record(artifact)
        parent_records = tuple(
            ArtifactParentRecord(
                artifact_id=artifact.provenance.artifact_id,
                parent_artifact_id=parent.provenance.artifact_id,
                run_id=artifact.provenance.run_id,
                artifact_step_id=artifact.provenance.step_id,
                parent_step_id=parent.provenance.step_id,
                ordinal=index,
            )
            for index, parent in enumerate(parents, start=1)
        )
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                # The parent-edge table has two self-referential artifact FKs;
                # make the new child identity durable in the savepoint first.
                await self._session.flush((record,))
                self._session.add_all(parent_records)
                await self._session.flush()
        except IntegrityError as exc:
            existing = await self.get(artifact.provenance.artifact_id)
            if existing is None:
                raise ArtifactPersistenceConflict(
                    "artifact_insert_conflict",
                    "artifact could not be inserted with its exact runtime lineage",
                ) from exc
            if existing != artifact:
                raise ArtifactPersistenceConflict(
                    "artifact_id_conflict",
                    "artifact ID already identifies a different immutable envelope",
                ) from exc
            return ArtifactInsertResult(existing, inserted=False)
        return ArtifactInsertResult(artifact, inserted=True)

    async def _hydrate(self, record: ArtifactRecord) -> ArtifactEnvelope:
        parent_ids = await self._parent_ids(record.id)
        artifact = _to_domain(record, parent_ids)
        await self._validate_runtime_scope(artifact)
        parents = await self._load_parents(parent_ids, hydrate=False)
        await self._validate_parent_lineage(artifact, parents)
        return artifact

    async def _parent_ids(self, artifact_id: str) -> tuple[str, ...]:
        statement = (
            select(ArtifactParentRecord)
            .where(ArtifactParentRecord.artifact_id == artifact_id)
            .order_by(ArtifactParentRecord.ordinal)
        )
        rows = tuple((await self._session.execute(statement)).scalars())
        if tuple(row.ordinal for row in rows) != tuple(range(1, len(rows) + 1)):
            raise ArtifactPersistenceConflict(
                "artifact_tampered", "artifact parent lineage order is not contiguous"
            )
        return tuple(row.parent_artifact_id for row in rows)

    async def _load_parents(
        self,
        parent_ids: tuple[str, ...],
        *,
        hydrate: bool = True,
    ) -> tuple[ArtifactEnvelope, ...]:
        if not parent_ids:
            return ()
        statement = select(ArtifactRecord).where(ArtifactRecord.id.in_(parent_ids))
        records = tuple((await self._session.execute(statement)).scalars())
        by_id = {record.id: record for record in records}
        if set(by_id) != set(parent_ids):
            raise ArtifactPersistenceConflict(
                "artifact_parent_missing", "artifact parent lineage is incomplete"
            )
        parents: list[ArtifactEnvelope] = []
        for parent_id in parent_ids:
            record = by_id[parent_id]
            parent_parent_ids = await self._parent_ids(parent_id)
            parent = _to_domain(record, parent_parent_ids)
            if hydrate:
                await self._validate_runtime_scope(parent)
            parents.append(parent)
        return tuple(parents)

    async def _validate_runtime_scope(self, artifact: ArtifactEnvelope) -> None:
        provenance = artifact.provenance
        run = await self._session.get(RunRecord, provenance.run_id)
        work = await self._session.get(WorkItemRecord, provenance.work_item_id)
        step = await self._session.get(RunStepRecord, provenance.step_id)
        plan = await self._session.get(RunPlanRecord, provenance.run_id)
        if run is None or work is None or step is None or plan is None:
            raise ArtifactPersistenceConflict(
                "artifact_scope_missing", "artifact runtime snapshot is incomplete"
            )
        work_sources = tuple(source for source in provenance.sources if source.kind == "work_input")
        if (
            run.work_item_id != work.id
            or step.run_id != run.id
            or step.plan_hash != plan.plan_hash
            or provenance.work_item_id != run.work_item_id
            or provenance.workflow_id != plan.workflow_id
            or not _workflow_version_matches(provenance.workflow_version, plan.workflow_version)
            or _catalog_digest(provenance.catalog_hash) != _catalog_digest(run.catalog_hash)
            or _catalog_digest(provenance.catalog_hash)
            != _catalog_digest(plan.catalog_content_hash)
            or provenance.admitted_input_digest != work.input_digest
            or len(work_sources) != 1
            or work_sources[0].source_id != work.id
            or work_sources[0].integrity_digest != work.input_digest
            or work_sources[0].classification.value != work.input_classification
            or provenance.template_id != step.template_id
            or provenance.instance_id != step.selected_instance_id
            or provenance.instance_config_revision != step.configuration_revision
            or provenance.output_schema_id != step.result_schema_id
            or provenance.output_schema_hash != step.result_schema_hash
            or provenance.created_at < work.created_at
            or provenance.created_at < run.created_at
            or provenance.created_at < plan.created_at
            or provenance.created_at < step.created_at
        ):
            raise ArtifactPersistenceConflict(
                "artifact_scope_mismatch",
                "artifact provenance disagrees with its immutable runtime snapshot",
            )

    async def _validate_parent_lineage(
        self,
        artifact: ArtifactEnvelope,
        parents: tuple[ArtifactEnvelope, ...],
    ) -> None:
        provenance = artifact.provenance
        if tuple(parent.provenance.artifact_id for parent in parents) != (
            provenance.parent_artifact_ids
        ):
            raise ArtifactPersistenceConflict(
                "artifact_parent_mismatch", "artifact parents disagree with provenance"
            )
        if not parents:
            return

        parent_sources = {
            source.source_id: source
            for source in provenance.sources
            if source.kind == "parent_artifact"
        }
        for parent in parents:
            parent_provenance = parent.provenance
            source = parent_sources.get(parent_provenance.artifact_id)
            if (
                parent_provenance.run_id != provenance.run_id
                or parent_provenance.work_item_id != provenance.work_item_id
                or parent_provenance.created_at > provenance.created_at
                or source is None
                or source.integrity_digest != parent_provenance.payload_hash
                or source.classification is not parent_provenance.classification
            ):
                raise ArtifactPersistenceConflict(
                    "artifact_parent_mismatch",
                    "artifact parent is not an exact earlier same-Run source",
                )

        statement = select(RunStepRecord.id, RunStepRecord.key).where(
            RunStepRecord.run_id == provenance.run_id
        )
        step_rows = tuple((await self._session.execute(statement)).all())
        key_to_id = {row.key: row.id for row in step_rows}
        dependency_rows = tuple(
            (
                await self._session.execute(
                    select(RunStepDependencyRecord).where(
                        RunStepDependencyRecord.run_id == provenance.run_id
                    )
                )
            ).scalars()
        )
        dependencies: dict[str, set[str]] = {}
        for row in dependency_rows:
            dependency_id = key_to_id.get(row.dependency_key)
            if dependency_id is None:
                raise ArtifactPersistenceConflict(
                    "artifact_lineage_invalid", "artifact Run graph is incomplete"
                )
            dependencies.setdefault(row.step_id, set()).add(dependency_id)

        ancestors: set[str] = set()
        pending = list(dependencies.get(provenance.step_id, ()))
        while pending:
            candidate = pending.pop()
            if candidate in ancestors:
                continue
            ancestors.add(candidate)
            pending.extend(dependencies.get(candidate, ()))
        if any(parent.provenance.step_id not in ancestors for parent in parents):
            raise ArtifactPersistenceConflict(
                "artifact_parent_not_ancestor",
                "artifact parent was not produced by an ancestor step",
            )
