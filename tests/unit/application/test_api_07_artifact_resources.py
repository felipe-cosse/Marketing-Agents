"""API-07 artifact redaction and pseudonymous digest behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType

import pytest
from marketing_agents.application.ports.repositories import InspectableArtifact
from marketing_agents.application.services.artifact_resources import (
    ArtifactListQuery,
    ArtifactResourceService,
    ArtifactResourceServiceError,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import RunStep
from marketing_agents.domain.enums import Effect, StepState
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.provenance import (
    ArtifactEnvelope,
    ProvenanceSource,
    ProviderVersion,
)
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RateLimitScope,
    RetryBackoff,
    RetryPolicySnapshot,
    StepRuntimePolicy,
    TimeoutPolicySnapshot,
    runtime_rate_limit_key,
)
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.redaction import REDACTED

from tests.support.identity import human_principal, service_principal

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)
TEMPLATE_ID = "template.api-07.reader"
INSTANCE_ID = "instance.api-07.reader"
SCHEMA_ID = "schema.api-07.result"
SCHEMA_HASH = "schema-sha256-v1:" + ("e" * 64)


def _step() -> RunStep:
    policy = StepRuntimePolicy(
        operation_key="operation.api-07.read",
        attempt_kind=AttemptKind.TOOL,
        retry=RetryPolicySnapshot(1, RetryBackoff.NONE),
        timeout=TimeoutPolicySnapshot(30, 120),
        budget=BudgetPolicySnapshot(20, 10, 20),
        rate_limit=RateLimitPolicySnapshot(
            RateLimitScope.TEMPLATE,
            runtime_rate_limit_key(
                template_id=TEMPLATE_ID,
                max_calls=10,
                window_seconds=60,
            ),
            10,
            60,
        ),
    )
    return RunStep(
        id="step.api-07.read",
        run_id="run.api-07",
        key="read",
        kind="connector.read",
        selected_instance_id=INSTANCE_ID,
        dependency_keys=(),
        capability_id="capability.api-07.read",
        effect=Effect.READ,
        state=StepState.PENDING,
        plan_hash="a" * 64,
        graph_hash="b" * 64,
        ordinal=1,
        source_order=1,
        template_id=TEMPLATE_ID,
        configuration_revision=1,
        connector_family="analytics",
        routing_slot_key=None,
        binding_id="binding.api-07.analytics",
        binding_configuration_revision=1,
        request_schema_id="schema.api-07.request",
        result_schema_id=SCHEMA_ID,
        result_schema_hash=SCHEMA_HASH,
        request_redaction_fields=(),
        result_redaction_fields=("/private/value",),
        data_classification=DataClassification.INTERNAL,
        idempotency_support="not_applicable",
        timeout_seconds=30,
        runtime_policy=policy,
        approval_policy_id="approval.none",
        approval_required_roles=(),
        approval_required_scopes=(),
        approval_expires_after_seconds=None,
        approval_allow_self_approval=None,
        terminal_result=True,
        created_at=NOW,
        updated_at=NOW,
        terminal_reason_code=None,
    )


def _artifact(
    *,
    classification: DataClassification = DataClassification.INTERNAL,
) -> InspectableArtifact:
    step = _step()
    artifact = ArtifactEnvelope.create(
        payload={
            "public": "visible",
            "private": {"value": "persisted-secret"},
            "api_key": "defense-in-depth-secret",
        },
        artifact_id="artifact.api-07",
        work_item_id="work.api-07",
        run_id=step.run_id,
        step_id=step.id,
        workflow_id="workflow.api-07",
        workflow_version="v1",
        template_id=step.template_id,
        instance_id=step.selected_instance_id,
        admitted_input_digest="c" * 64,
        catalog_hash="catalog-sha256-v1:" + ("a" * 64),
        instance_config_revision=step.configuration_revision,
        sources=(
            ProvenanceSource(
                kind="work_input",
                source_id="work.api-07",
                integrity_digest="f" * 64,
                classification=classification,
            ),
        ),
        parent_artifact_ids=(),
        providers=(
            ProviderVersion(
                provider_kind="connector",
                mode="mock",
                name="analytics",
                version="v1",
            ),
        ),
        output_schema_id=SCHEMA_ID,
        output_schema_version="v1",
        output_schema_hash=SCHEMA_HASH,
        created_at=NOW,
        classification=classification,
    )
    return InspectableArtifact(artifact=artifact, step=step)


class _Artifacts:
    def __init__(self, item: InspectableArtifact) -> None:
        self.item = item

    async def get_inspectable(self, artifact_id: str) -> InspectableArtifact | None:
        return self.item if artifact_id == self.item.artifact.provenance.artifact_id else None

    async def list_for_run_page(self, run_id: str, **_: object):  # type: ignore[no-untyped-def]
        return (self.item,) if run_id == self.item.artifact.provenance.run_id else ()


class _Runs:
    async def get_inspectable(self, run_id: str):  # type: ignore[no-untyped-def]
        if run_id != "run.api-07":
            return None
        from tests.unit.application.test_api_07_run_resources import _inspectable

        return _inspectable(
            INSTANCE_ID,
            "api-07",
            NOW,
        )


class _UnitOfWork:
    def __init__(self, item: InspectableArtifact) -> None:
        self.artifacts = _Artifacts(item)
        self.runs = _Runs()

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        raise AssertionError("artifact reads must never commit")


class _Factory:
    def __init__(self, item: InspectableArtifact) -> None:
        self.unit = _UnitOfWork(item)
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        return self.unit


class _ExplodingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        raise AssertionError("authorization must precede unit-of-work access")


def _reader() -> AuthenticatedPrincipal:
    return human_principal(roles=frozenset({"viewer"}), scopes=frozenset())


@pytest.mark.asyncio
async def test_api_07_artifact_detail_uses_persisted_redaction_and_keyed_digest() -> None:
    item = _artifact()
    service = ArtifactResourceService(
        _Factory(item),  # type: ignore[arg-type]
        digest_key=DigestKey(b"k" * 32),
    )

    resource = await service.read("artifact.api-07", principal=_reader())
    page = await service.list_for_run(
        ArtifactListQuery(run_id="run.api-07"),
        principal=_reader(),
    )

    assert resource.redacted_payload == {
        "api_key": REDACTED,
        "private": {"value": REDACTED},
        "public": "visible",
    }
    assert resource.payload_digest.startswith("artifact-hmac-sha256-v1:")
    assert item.artifact.provenance.payload_hash not in resource.payload_digest
    assert not hasattr(resource, "payload_hash")
    assert not hasattr(page.items[0], "payload")
    assert not hasattr(page.items[0], "payload_hash")


@pytest.mark.asyncio
async def test_api_07_secret_artifact_detail_fails_closed() -> None:
    item = _artifact(classification=DataClassification.SECRET)
    service = ArtifactResourceService(
        _Factory(item),  # type: ignore[arg-type]
        digest_key=DigestKey(b"k" * 32),
    )

    with pytest.raises(ArtifactResourceServiceError) as captured:
        await service.read("artifact.api-07", principal=_reader())

    assert captured.value.code == "artifact_record_corrupt"


def test_api_07_artifact_classification_cannot_be_lower_than_producer_step() -> None:
    item = _artifact()

    with pytest.raises(ValueError, match="classification cannot be lower"):
        InspectableArtifact(
            artifact=item.artifact,
            step=replace(
                item.step,
                data_classification=DataClassification.SECRET,
            ),
        )


@pytest.mark.asyncio
async def test_api_07_artifact_authorization_precedes_repository_access() -> None:
    units = _ExplodingFactory()
    service = ArtifactResourceService(
        units,  # type: ignore[arg-type]
        digest_key=DigestKey(b"k" * 32),
    )

    with pytest.raises(ArtifactResourceServiceError) as captured:
        await service.read("artifact.api-07", principal=service_principal())

    assert captured.value.code == "runtime_human_required"
    assert units.calls == 0
