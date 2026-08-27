"""Read-only artifact metadata and redacted payload projections."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from marketing_agents.application.policies.runtime_resource_authorization import (
    RuntimeResourceAuthorizationError,
    authorize_runtime_resource_reader,
)
from marketing_agents.application.ports.repositories import (
    ArtifactRepositoryConflict,
    InspectableArtifact,
    InspectableRun,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import (
    DataClassification,
    highest_classification,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.validation import require_id, require_utc
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.redaction import redact_json_pointers

DEFAULT_ARTIFACT_PAGE_SIZE = 25
MAX_ARTIFACT_PAGE_SIZE = 100
MAX_ARTIFACT_CURSOR_LENGTH = 1_024
_CURSOR_PREFIX = "artifact-page-v1."
_FILTER_DOMAIN = b"marketing-agents:artifact-page-filter:v1\x00"
_DIGEST_DOMAIN = b"marketing-agents:artifact-api-pseudonym:hmac-sha256:v1\x00"
_DIGEST_PREFIX = "artifact-hmac-sha256-v1:"


class ArtifactResourceServiceError(ValueError):
    """Stable non-sensitive failure raised by the artifact query seam."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ArtifactListQuery:
    run_id: str
    cursor: str | None = field(default=None, repr=False)
    limit: int = DEFAULT_ARTIFACT_PAGE_SIZE

    def __post_init__(self) -> None:
        require_id(self.run_id, "artifact run ID")
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_ARTIFACT_PAGE_SIZE:
            raise ValueError("artifact page limit is outside the supported range")
        if self.cursor is not None and (
            type(self.cursor) is not str
            or not self.cursor
            or len(self.cursor) > MAX_ARTIFACT_CURSOR_LENGTH
        ):
            raise ValueError("artifact page cursor is invalid")


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    artifact_id: str
    work_item_id: str
    run_id: str
    step_id: str
    workflow_id: str
    workflow_version: str
    template_id: str
    instance_id: str
    output_schema_id: str
    output_schema_version: str
    classification: str
    created_at: datetime
    artifact_url: str
    run_url: str
    step_url: str
    template_url: str
    instance_url: str


@dataclass(frozen=True, slots=True)
class ArtifactSourceResource:
    kind: str
    source_id: str
    classification: str


@dataclass(frozen=True, slots=True)
class ArtifactProviderResource:
    provider_kind: str
    mode: str
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ArtifactResource:
    artifact_id: str
    work_item_id: str
    run_id: str
    step_id: str
    workflow_id: str
    workflow_version: str
    template_id: str
    instance_id: str
    catalog_hash: str
    instance_config_revision: int
    sources: tuple[ArtifactSourceResource, ...]
    parent_artifact_ids: tuple[str, ...]
    providers: tuple[ArtifactProviderResource, ...]
    output_schema_id: str
    output_schema_version: str
    output_schema_hash: str
    classification: str
    created_at: datetime
    redacted_payload: Mapping[str, Any] = field(repr=False)
    payload_digest: str = field(repr=False)
    artifact_url: str
    run_url: str
    step_url: str
    template_url: str
    instance_url: str


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    run_id: str
    items: tuple[ArtifactSummary, ...]
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _ArtifactCursorBoundary:
    created_at: datetime
    artifact_id: str


def _filter_fingerprint(query: ArtifactListQuery) -> str:
    return hashlib.sha256(
        _FILTER_DOMAIN + canonical_json_bytes({"run_id": query.run_id})
    ).hexdigest()


def _encode_cursor(resource: ArtifactSummary, query: ArtifactListQuery) -> str:
    payload = canonical_json_bytes(
        {
            "created_at": resource.created_at.isoformat(timespec="microseconds"),
            "endpoint": f"run-artifacts:{query.run_id}",
            "filter": _filter_fingerprint(query),
            "id": resource.artifact_id,
            "version": 1,
        }
    )
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_CURSOR_PREFIX}{token}"


def _decode_cursor(query: ArtifactListQuery) -> _ArtifactCursorBoundary | None:
    if query.cursor is None:
        return None
    if not query.cursor.startswith(_CURSOR_PREFIX):
        raise _cursor_error()
    encoded = query.cursor[len(_CURSOR_PREFIX) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        decoded = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise _cursor_error() from None
    if (
        type(decoded) is not dict
        or set(decoded) != {"created_at", "endpoint", "filter", "id", "version"}
        or decoded.get("version") != 1
        or decoded.get("endpoint") != f"run-artifacts:{query.run_id}"
        or type(decoded.get("filter")) is not str
        or type(decoded.get("id")) is not str
        or type(decoded.get("created_at")) is not str
    ):
        raise _cursor_error()
    try:
        if not hmac.compare_digest(decoded["filter"], _filter_fingerprint(query)):
            raise ValueError("artifact cursor filters changed")
        created_at = datetime.fromisoformat(decoded["created_at"])
        require_utc(created_at, "artifact cursor time")
        require_id(decoded["id"], "artifact cursor ID")
    except (TypeError, ValueError):
        raise _cursor_error() from None
    canonical = _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if not hmac.compare_digest(canonical, query.cursor):
        raise _cursor_error()
    return _ArtifactCursorBoundary(
        created_at=created_at,
        artifact_id=decoded["id"],
    )


def _cursor_error() -> ArtifactResourceServiceError:
    return ArtifactResourceServiceError(
        "artifact_cursor_invalid",
        "artifact page cursor is invalid",
    )


def _validate_inspectable(item: InspectableArtifact) -> None:
    if type(item) is not InspectableArtifact:
        raise ValueError("artifact inspection contract is invalid")
    artifact = item.artifact
    provenance = artifact.provenance
    step = item.step
    if not artifact.verify_payload():
        raise ValueError("artifact payload no longer binds its provenance")
    if (
        provenance.run_id != step.run_id
        or provenance.step_id != step.id
        or provenance.template_id != step.template_id
        or provenance.instance_id != step.selected_instance_id
        or provenance.instance_config_revision != step.configuration_revision
        or provenance.output_schema_id != step.result_schema_id
        or provenance.output_schema_hash != step.result_schema_hash
        or provenance.classification
        is not highest_classification(
            provenance.classification,
            step.data_classification,
        )
    ):
        raise ValueError("artifact no longer binds its producer step")


def project_artifact_summary(item: InspectableArtifact) -> ArtifactSummary:
    """Project bounded artifact metadata without payload or payload digest material."""

    _validate_inspectable(item)
    provenance = item.artifact.provenance
    return ArtifactSummary(
        artifact_id=provenance.artifact_id,
        work_item_id=provenance.work_item_id,
        run_id=provenance.run_id,
        step_id=provenance.step_id,
        workflow_id=provenance.workflow_id,
        workflow_version=provenance.workflow_version,
        template_id=provenance.template_id,
        instance_id=provenance.instance_id,
        output_schema_id=provenance.output_schema_id,
        output_schema_version=provenance.output_schema_version,
        classification=provenance.classification.value,
        created_at=provenance.created_at,
        artifact_url=f"/api/v1/artifacts/{provenance.artifact_id}",
        run_url=f"/api/v1/runs/{provenance.run_id}",
        step_url=f"/api/v1/runs/{provenance.run_id}/steps/{provenance.step_id}",
        template_url=f"/api/v1/agent-templates/{provenance.template_id}",
        instance_url=f"/api/v1/agent-instances/{provenance.instance_id}",
    )


def _plain_redacted_payload(item: InspectableArtifact) -> Mapping[str, Any]:
    if item.artifact.provenance.classification is DataClassification.SECRET:
        raise ValueError("secret-classified artifacts are not retainable")
    projected = redact_json_pointers(
        item.artifact.payload,
        item.step.result_redaction_fields,
    )
    if type(projected) is not dict:
        raise ValueError("artifact payload projection must remain an object")
    return cast(dict[str, Any], projected)


class ArtifactResourceService:
    """Authorize artifact reads and apply the persisted producer redaction policy."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        *,
        digest_key: DigestKey,
    ) -> None:
        if not callable(unit_of_work) or type(digest_key) is not DigestKey:
            raise ValueError("artifact resources require exact callable dependencies")
        self._unit_of_work = unit_of_work
        self._digest_key = digest_key

    async def list_for_run(
        self,
        query: ArtifactListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ArtifactPage:
        self._authorize(principal)
        if type(query) is not ArtifactListQuery:
            raise ArtifactResourceServiceError(
                "artifact_query_invalid",
                "artifact list query is invalid",
            )
        boundary = _decode_cursor(query)
        try:
            async with self._unit_of_work() as unit_of_work:
                run = await unit_of_work.runs.get_inspectable(query.run_id)
                stored = (
                    ()
                    if run is None
                    else await unit_of_work.artifacts.list_for_run_page(
                        query.run_id,
                        after_created_at=(None if boundary is None else boundary.created_at),
                        after_artifact_id=(None if boundary is None else boundary.artifact_id),
                        limit=query.limit + 1,
                    )
                )
        except (ArtifactRepositoryConflict, TypeError, ValueError, RuntimeError):
            raise ArtifactResourceServiceError(
                "artifact_record_corrupt",
                "artifact resources could not be validated",
            ) from None
        if run is None:
            raise ArtifactResourceServiceError(
                "run_not_found",
                "run was not found",
            )
        if (
            type(run) is not InspectableRun
            or run.run.id != query.run_id
            or run.run.work_item_id != run.work_item.id
        ):
            raise ArtifactResourceServiceError(
                "artifact_record_corrupt",
                "artifact resources could not be validated",
            )
        try:
            projected = tuple(project_artifact_summary(item) for item in stored[: query.limit])
            boundaries = tuple((value.created_at, value.artifact_id) for value in projected)
            if (
                any(value.run_id != query.run_id for value in projected)
                or boundaries != tuple(sorted(boundaries))
                or len(boundaries) != len(set(boundaries))
                or (
                    boundary is not None
                    and any(
                        value <= (boundary.created_at, boundary.artifact_id) for value in boundaries
                    )
                )
            ):
                raise ValueError("artifact page violates its deterministic query boundary")
        except (TypeError, ValueError):
            raise ArtifactResourceServiceError(
                "artifact_record_corrupt",
                "artifact resources could not be validated",
            ) from None
        next_cursor = (
            _encode_cursor(projected[-1], query)
            if len(stored) > query.limit and projected
            else None
        )
        return ArtifactPage(
            run_id=query.run_id,
            items=projected,
            next_cursor=next_cursor,
        )

    async def read(
        self,
        artifact_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ArtifactResource:
        self._authorize(principal)
        try:
            require_id(artifact_id, "artifact ID")
        except (TypeError, ValueError):
            raise ArtifactResourceServiceError(
                "artifact_id_invalid",
                "artifact ID is invalid",
            ) from None
        try:
            async with self._unit_of_work() as unit_of_work:
                stored = await unit_of_work.artifacts.get_inspectable(artifact_id)
                run = (
                    None
                    if stored is None
                    else await unit_of_work.runs.get_inspectable(stored.artifact.provenance.run_id)
                )
        except (ArtifactRepositoryConflict, TypeError, ValueError, RuntimeError):
            raise ArtifactResourceServiceError(
                "artifact_record_corrupt",
                "artifact resources could not be validated",
            ) from None
        if stored is None:
            raise ArtifactResourceServiceError(
                "artifact_not_found",
                "artifact was not found",
            )
        if run is None or type(run) is not InspectableRun:
            raise ArtifactResourceServiceError(
                "artifact_record_corrupt",
                "artifact resources could not be validated",
            )
        try:
            summary = project_artifact_summary(stored)
            provenance = stored.artifact.provenance
            if (
                run.run.id != provenance.run_id
                or run.run.work_item_id != provenance.work_item_id
                or run.work_item.id != provenance.work_item_id
                or run.work_item.workflow_id != provenance.workflow_id
                or run.run.catalog_hash != provenance.catalog_hash
            ):
                raise ValueError("artifact provenance no longer binds its Run")
            payload = _plain_redacted_payload(stored)
            digest = self._derive_payload_digest(stored)
        except (TypeError, ValueError):
            raise ArtifactResourceServiceError(
                "artifact_record_corrupt",
                "artifact resources could not be validated",
            ) from None
        return ArtifactResource(
            artifact_id=summary.artifact_id,
            work_item_id=summary.work_item_id,
            run_id=summary.run_id,
            step_id=summary.step_id,
            workflow_id=summary.workflow_id,
            workflow_version=summary.workflow_version,
            template_id=summary.template_id,
            instance_id=summary.instance_id,
            catalog_hash=provenance.catalog_hash,
            instance_config_revision=provenance.instance_config_revision,
            sources=tuple(
                ArtifactSourceResource(
                    kind=source.kind,
                    source_id=source.source_id,
                    classification=source.classification.value,
                )
                for source in provenance.sources
            ),
            parent_artifact_ids=provenance.parent_artifact_ids,
            providers=tuple(
                ArtifactProviderResource(
                    provider_kind=provider.provider_kind,
                    mode=provider.mode,
                    name=provider.name,
                    version=provider.version,
                )
                for provider in provenance.providers
            ),
            output_schema_id=summary.output_schema_id,
            output_schema_version=summary.output_schema_version,
            output_schema_hash=provenance.output_schema_hash,
            classification=summary.classification,
            created_at=summary.created_at,
            redacted_payload=payload,
            payload_digest=digest,
            artifact_url=summary.artifact_url,
            run_url=summary.run_url,
            step_url=summary.step_url,
            template_url=summary.template_url,
            instance_url=summary.instance_url,
        )

    def _derive_payload_digest(self, item: InspectableArtifact) -> str:
        provenance = item.artifact.provenance
        material = canonical_json_bytes(
            {
                "artifact_id": provenance.artifact_id,
                "output_schema_hash": provenance.output_schema_hash,
                "payload_hash": provenance.payload_hash,
                "run_id": provenance.run_id,
                "step_id": provenance.step_id,
            }
        )
        value = hmac.new(
            self._digest_key.bytes_for_digest(),
            _DIGEST_DOMAIN + material,
            hashlib.sha256,
        ).hexdigest()
        return f"{_DIGEST_PREFIX}{value}"

    @staticmethod
    def _authorize(principal: AuthenticatedPrincipal) -> None:
        try:
            authorize_runtime_resource_reader(principal)
        except RuntimeResourceAuthorizationError as exc:
            raise ArtifactResourceServiceError(exc.code, str(exc)) from None


__all__ = [
    "DEFAULT_ARTIFACT_PAGE_SIZE",
    "MAX_ARTIFACT_PAGE_SIZE",
    "ArtifactListQuery",
    "ArtifactPage",
    "ArtifactProviderResource",
    "ArtifactResource",
    "ArtifactResourceService",
    "ArtifactResourceServiceError",
    "ArtifactSourceResource",
    "ArtifactSummary",
    "project_artifact_summary",
]
