"""API-07 authenticated runtime inspection transport contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.dependencies import (
    ArtifactResourceExecutor,
    AuditResourceExecutor,
    RunResourceExecutor,
)
from marketing_agents.application.ports.identity import (
    AuthenticationEvidence,
    IdentityAuthenticationError,
)
from marketing_agents.application.services.artifact_resources import (
    ArtifactListQuery,
    ArtifactPage,
    ArtifactProviderResource,
    ArtifactResource,
    ArtifactSourceResource,
)
from marketing_agents.application.services.audit_resources import (
    AUDIT_FEED_ENDPOINT_VERSION,
    AuditListQuery,
    AuditPage,
    AuditResource,
    AuditResourceServiceError,
)
from marketing_agents.application.services.run_resources import (
    ExternalActionResource,
    InstanceRuntimeStatus,
    InstanceStatusSummary,
    RunListQuery,
    RunPage,
    RunResource,
    RunStepResource,
    RunStepTransitionResource,
    RunTimelineEvent,
    RunTimelinePage,
    RunTimelineQuery,
    RunTransitionResource,
)
from marketing_agents.config import Settings
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.identity import AuthenticatedPrincipal

from tests.support.identity import StaticIdentityProvider, human_principal, service_principal

NOW = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)
INSTANCE_ID = "inst.email.newsletter.newsletter-subscriber.01"
RUN_ID = "run.api-07.01"
ARTIFACT_ID = "artifact.api-07.01"
STEP_ID = "step.api-07.01"
ACTION_ID = "action.api-07.01"


class DenyingIdentityProvider:
    async def authenticate(
        self,
        evidence: AuthenticationEvidence,
    ) -> AuthenticatedPrincipal:
        del evidence
        raise IdentityAuthenticationError("missing")


def _run_resource(
    run_id: str = RUN_ID,
    *,
    instance_id: str = INSTANCE_ID,
    created_at: datetime = NOW,
) -> RunResource:
    return RunResource(
        run_id=run_id,
        work_item_id=f"work.{run_id}",
        instance_id=instance_id,
        workflow_id="workflow.api-07",
        trigger_id="trigger.api-07",
        source="manual",
        mode="dry_run",
        state="received",
        catalog_hash="a" * 64,
        configuration_revision=1,
        approval_required=None,
        terminal_reason_code=None,
        created_at=created_at,
        updated_at=created_at,
        version=1,
        transitions=(),
        plan=None,
        execution_control=None,
        pending_approvals=(),
        artifact_summaries=(),
        artifacts_truncated=False,
        external_actions=(),
        run_url=f"/api/v1/runs/{run_id}",
        timeline_url=f"/api/v1/runs/{run_id}/timeline",
        artifacts_url=f"/api/v1/runs/{run_id}/artifacts",
        instance_url=f"/api/v1/agent-instances/{instance_id}",
    )


def _run_detail_resource() -> RunResource:
    return replace(
        _run_resource(),
        transitions=(
            RunTransitionResource(
                sequence=1,
                command="receive",
                previous_state=None,
                new_state="received",
                reason_code="work_received",
                occurred_at=NOW,
                expected_version=0,
                resulting_version=1,
                completed_effect_count=0,
                outcome_unknown_effect_count=0,
            ),
        ),
    )


def _timeline_event() -> RunTimelineEvent:
    return RunTimelineEvent(
        event_id="audit.api-07.timeline.01",
        sequence=1,
        schema_version=1,
        event_type="run.received",
        aggregate_type="run",
        aggregate_id=RUN_ID,
        outcome="accepted",
        actor_id="principal.api-07.viewer",
        actor_source="local_user",
        auth_method="bearer",
        correlation_id="correlation.api-07.01",
        occurred_at=NOW,
        step_id=None,
        action_id=None,
        approval_request_id=None,
        artifact_id=None,
        attempted_command="receive",
        previous_state=None,
        new_state="received",
        reason_code="work_received",
        metadata={},
        metadata_classification="internal",
        metadata_expires_at=NOW + timedelta(days=1),
        metadata_expired=False,
        run_url=f"/api/v1/runs/{RUN_ID}",
        step_url=None,
        action_url=None,
        approval_url=None,
        artifact_url=None,
    )


def _step_resource() -> RunStepResource:
    template_id = "tpl.email.newsletter.newsletter-subscriber"
    return RunStepResource(
        step_id=STEP_ID,
        run_id=RUN_ID,
        key="inspect",
        kind="inspect",
        selected_instance_id=INSTANCE_ID,
        template_id=template_id,
        dependency_keys=(),
        capability_id="capability.inspect",
        effect="read",
        state="pending",
        ordinal=1,
        source_order=1,
        configuration_revision=1,
        connector_family="local",
        routing_slot_key=None,
        binding_id=None,
        binding_configuration_revision=None,
        request_schema_id=None,
        result_schema_id=None,
        result_schema_hash=None,
        data_classification="internal",
        idempotency_support="not_applicable",
        timeout_seconds=60,
        runtime_policy={
            "operation_key": "inspect",
            "attempt_kind": "no_call",
            "max_attempts": 1,
            "backoff": "none",
            "step_timeout_seconds": 60,
            "template_run_timeout_seconds": 300,
            "max_steps": 1,
            "max_model_calls": 0,
            "max_tool_calls": 0,
            "max_input_bytes": 1_024,
            "max_input_field_bytes": 512,
            "max_output_bytes": 1_024,
            "max_model_output_tokens": 100,
            "rate_limit_scope": "template",
            "rate_limit_key": "api-07-inspect",
            "rate_limit_max_calls": 10,
            "rate_limit_window_seconds": 60,
        },
        approval_policy_id="approval.none",
        approval_required_roles=(),
        approval_required_scopes=(),
        approval_expires_after_seconds=None,
        approval_allow_self_approval=None,
        terminal_result=False,
        created_at=NOW,
        updated_at=NOW,
        version=1,
        terminal_reason_code=None,
        transitions=(
            RunStepTransitionResource(
                sequence=1,
                command="create",
                previous_state=None,
                new_state="pending",
                reason_code="step_created",
                occurred_at=NOW,
                expected_version=0,
                resulting_version=1,
            ),
        ),
        step_url=f"/api/v1/runs/{RUN_ID}/steps/{STEP_ID}",
        run_url=f"/api/v1/runs/{RUN_ID}",
        instance_url=f"/api/v1/agent-instances/{INSTANCE_ID}",
        template_url=f"/api/v1/agent-templates/{template_id}",
    )


def _external_action_resource() -> ExternalActionResource:
    template_id = "tpl.email.newsletter.newsletter-subscriber"
    return ExternalActionResource(
        action_id=ACTION_ID,
        run_id=RUN_ID,
        proposal_revision=1,
        step_id=STEP_ID,
        step_key="inspect",
        template_id=template_id,
        instance_id=INSTANCE_ID,
        action_type="inspect",
        capability_id="capability.inspect",
        connector_family="local",
        binding_id="binding.api-07.inspect",
        destination_summary="local inspection",
        redacted_payload={"private_value": "[REDACTED]"},
        payload_schema_id="schema.api-07.action",
        state="proposed",
        created_at=NOW,
        updated_at=NOW,
        version=1,
        delivery_attempt_count=0,
        delivery_attempt_limit=3,
        approval_policy_id="approval.none",
        approval_required_roles=(),
        approval_required_scopes=(),
        approval_expires_after_seconds=3_600,
        approval_allow_self_approval=False,
        terminal_reason_code=None,
        superseded_by_action_id=None,
        superseded_at=None,
        receipt_id=None,
        result_status=None,
        result_safe_metadata=None,
        completed_at=None,
        action_url=f"/api/v1/external-actions/{ACTION_ID}",
        run_url=f"/api/v1/runs/{RUN_ID}",
        step_url=f"/api/v1/runs/{RUN_ID}/steps/{STEP_ID}",
        instance_url=f"/api/v1/agent-instances/{INSTANCE_ID}",
        template_url=f"/api/v1/agent-templates/{template_id}",
    )


class FakeRunExecutor:
    def __init__(self) -> None:
        self.page: object = RunPage(items=(_run_resource(),), next_cursor=None)
        self.read_result: object = object()
        self.timeline_result: object = RunTimelinePage(
            run_id=RUN_ID,
            items=(),
            next_cursor=None,
        )
        self.step_result: object = object()
        self.action_result: object = object()
        self.status_reads = 0
        self.drift_status = False
        self.list_calls: list[RunListQuery] = []

    async def list(
        self,
        query: RunListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunPage:
        del principal
        self.list_calls.append(query)
        return cast(RunPage, self.page)

    async def read(
        self,
        run_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunResource:
        del run_id, principal
        return cast(RunResource, self.read_result)

    async def read_timeline(
        self,
        run_id: str,
        query: RunTimelineQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunTimelinePage:
        del run_id, query, principal
        return cast(RunTimelinePage, self.timeline_result)

    async def read_step(
        self,
        run_id: str,
        step_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunStepResource:
        del run_id, step_id, principal
        return cast(RunStepResource, self.step_result)

    async def read_external_action(
        self,
        action_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> Any:
        del action_id, principal
        return self.action_result

    async def read_instance_status_summary(
        self,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceStatusSummary:
        del principal
        return self._status_summary(INSTANCE_ID)

    async def read_instance_statuses(
        self,
        instance_ids: tuple[str, ...],
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceStatusSummary:
        del principal
        self.status_reads += 1
        summary = self._status_summary(instance_ids[0])
        if self.drift_status and self.status_reads % 2 == 0:
            item = summary.items[0]
            changed = InstanceRuntimeStatus(
                instance_id=item.instance_id,
                status=item.status,
                latest_run_id=item.latest_run_id,
                latest_run_state=item.latest_run_state,
                latest_run_created_at=item.latest_run_created_at,
                latest_run_updated_at=cast(datetime, item.latest_run_updated_at)
                + timedelta(seconds=1),
                instance_url=item.instance_url,
                latest_run_url=item.latest_run_url,
            )
            return InstanceStatusSummary(
                scope=summary.scope,
                items=(changed,),
                etag='"instance-status-sha256-v1:' + "b" * 64 + '"',
            )
        return summary

    async def list_recent_instance_runs(
        self,
        instance_id: str,
        *,
        limit: int = 5,
        principal: AuthenticatedPrincipal,
    ) -> tuple[RunResource, ...]:
        del limit, principal
        return (_run_resource(instance_id=instance_id),)

    @staticmethod
    def _status_summary(instance_id: str) -> InstanceStatusSummary:
        return InstanceStatusSummary(
            scope="single-local-installation",
            items=(
                InstanceRuntimeStatus(
                    instance_id=instance_id,
                    status="received",
                    latest_run_id=RUN_ID,
                    latest_run_state="received",
                    latest_run_created_at=NOW,
                    latest_run_updated_at=NOW,
                    instance_url=f"/api/v1/agent-instances/{instance_id}",
                    latest_run_url=f"/api/v1/runs/{RUN_ID}",
                ),
            ),
            etag='"instance-status-sha256-v1:' + "a" * 64 + '"',
        )


class FakeArtifactExecutor:
    def __init__(self) -> None:
        self.page: object | None = None
        self.resource: object = _artifact_resource()

    async def list_for_run(
        self,
        query: ArtifactListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ArtifactPage:
        del principal
        if self.page is not None:
            return cast(ArtifactPage, self.page)
        return ArtifactPage(run_id=query.run_id, items=(), next_cursor=None)

    async def read(
        self,
        artifact_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ArtifactResource:
        del artifact_id, principal
        return cast(ArtifactResource, self.resource)


class FakeAuditExecutor:
    def __init__(self) -> None:
        self.page: object = AuditPage(
            endpoint_version=AUDIT_FEED_ENDPOINT_VERSION,
            high_watermark=0,
            items=(),
            next_cursor=None,
        )
        self.error: AuditResourceServiceError | None = None
        self.queries: list[AuditListQuery] = []

    async def list(
        self,
        query: AuditListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuditPage:
        del principal
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return cast(AuditPage, self.page)


def _artifact_resource() -> ArtifactResource:
    return ArtifactResource(
        artifact_id=ARTIFACT_ID,
        work_item_id="work.api-07.01",
        run_id=RUN_ID,
        step_id="step.api-07.01",
        workflow_id="workflow.api-07",
        workflow_version="1",
        template_id="tpl.email.newsletter.newsletter-subscriber",
        instance_id=INSTANCE_ID,
        catalog_hash="a" * 64,
        instance_config_revision=1,
        sources=(
            ArtifactSourceResource(
                kind="work_input",
                source_id="work.api-07.01",
                classification="internal",
            ),
        ),
        parent_artifact_ids=(),
        providers=(
            ArtifactProviderResource(
                provider_kind="planner",
                mode="mock",
                name="deterministic-planner",
                version="1",
            ),
        ),
        output_schema_id="schema.api-07.output",
        output_schema_version="1",
        output_schema_hash="schema-sha256-v1:" + "b" * 64,
        classification="internal",
        created_at=NOW,
        redacted_payload={
            "private_value": "[REDACTED]",
            "html_fragment": "<script>alert('inert')</script>",
        },
        payload_digest="artifact-hmac-sha256-v1:" + "c" * 64,
        artifact_url=f"/api/v1/artifacts/{ARTIFACT_ID}",
        run_url=f"/api/v1/runs/{RUN_ID}",
        step_url=f"/api/v1/runs/{RUN_ID}/steps/step.api-07.01",
        template_url="/api/v1/agent-templates/tpl.email.newsletter.newsletter-subscriber",
        instance_url=f"/api/v1/agent-instances/{INSTANCE_ID}",
    )


def _audit_resource(*, run_id: str = RUN_ID) -> AuditResource:
    return AuditResource(
        event_id="audit.api-07.01",
        schema_version=1,
        feed_sequence=42,
        run_sequence=1,
        run_id=run_id,
        schedule_id=None,
        occurrence_id=None,
        event_type="run.received",
        aggregate_type="run",
        aggregate_id=run_id,
        outcome="accepted",
        actor_id="principal.api-07.viewer",
        actor_source="local_user",
        auth_method="bearer",
        correlation_id="correlation.api-07.01",
        occurred_at=NOW,
        step_id=None,
        action_id=None,
        action_attempt_number=None,
        receipt_id=None,
        approval_request_id=None,
        approval_decision_id=None,
        artifact_id=None,
        attempt_id=None,
        attempted_command="receive",
        expected_version=0,
        observed_version=None,
        observed_state=None,
        requested_state="received",
        mutation_version=1,
        transition_sequence=1,
        previous_state=None,
        new_state="received",
        reason_code="work_received",
        metadata={},
        metadata_classification="internal",
        metadata_expires_at=NOW + timedelta(days=1),
        metadata_expired=False,
        run_url=f"/api/v1/runs/{run_id}",
        step_url=None,
        action_url=None,
        approval_url=None,
        artifact_url=None,
    )


def _viewer() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.api-07.viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset(),
    )


def _app(
    run_executor: object | None = None,
    *,
    artifact_executor: object | None = None,
    audit_executor: object | None = None,
    principal: AuthenticatedPrincipal | None = None,
    identity_provider: object | None = None,
) -> FastAPI:
    return create_app(
        Settings(_env_file=None),
        identity_provider=cast(
            Any,
            identity_provider or StaticIdentityProvider(principal or _viewer()),
        ),
        run_resource_service=cast(RunResourceExecutor | None, run_executor),
        artifact_resource_service=cast(
            ArtifactResourceExecutor | None,
            artifact_executor,
        ),
        audit_resource_service=cast(AuditResourceExecutor | None, audit_executor),
    )


async def _get(
    app: FastAPI,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.get(path, headers=headers)


def _assert_no_store(response: Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "authorization" in response.headers["vary"].casefold()


@pytest.mark.parametrize("role", ["viewer", "operator", "approver", "local_admin"])
@pytest.mark.asyncio
async def test_api_07_human_read_role_matrix(role: str) -> None:
    principal = human_principal(
        actor_id=f"principal.api-07.{role}",
        roles=frozenset({role}),
        scopes=frozenset(),
    )
    response = await _get(_app(FakeRunExecutor(), principal=principal), "/api/v1/runs")
    assert response.status_code == 200
    _assert_no_store(response)


@pytest.mark.asyncio
async def test_api_07_authentication_and_authorization_precede_executor_resolution() -> None:
    missing = await _get(
        _app(object(), identity_provider=DenyingIdentityProvider()),
        "/api/v1/runs",
    )
    service = await _get(
        _app(object(), principal=service_principal()),
        "/api/v1/runs",
    )
    unrelated = await _get(
        _app(
            object(),
            principal=human_principal(
                actor_id="principal.api-07.unrelated",
                roles=frozenset({"auditor"}),
                scopes=frozenset(),
            ),
        ),
        "/api/v1/runs",
    )
    assert missing.status_code == 401
    assert service.status_code == 403
    assert unrelated.status_code == 403
    _assert_no_store(missing)
    _assert_no_store(service)
    _assert_no_store(unrelated)


@pytest.mark.asyncio
async def test_api_07_duplicate_queries_are_rejected_before_executor_resolution() -> None:
    for path in (
        "/api/v1/runs?limit=1&limit=2",
        f"/api/v1/runs/{RUN_ID}/timeline?cursor=a&cursor=b",
        f"/api/v1/runs/{RUN_ID}/artifacts?limit=1&limit=2",
        "/api/v1/audit-events?run_id=a&run_id=b",
    ):
        response = await _get(
            _app(object(), artifact_executor=object(), audit_executor=object()),
            path,
        )
        assert response.status_code == 400
        _assert_no_store(response)


@pytest.mark.asyncio
async def test_api_07_list_filters_and_malformed_executor_results_fail_closed() -> None:
    executor = FakeRunExecutor()
    valid = await _get(_app(executor), "/api/v1/runs?state=received&limit=1")
    assert valid.status_code == 200
    assert valid.json()["items"][0]["id"] == RUN_ID
    assert executor.list_calls[0].state is RunState.RECEIVED
    _assert_no_store(valid)

    executor.page = RunPage(
        items=(_run_resource(instance_id="inst.wrong.runtime.reader.01"),),
        next_cursor=None,
    )
    malformed = await _get(
        _app(executor),
        f"/api/v1/runs?instance_id={INSTANCE_ID}",
    )
    assert malformed.status_code == 503
    assert "inst.wrong" not in malformed.text
    _assert_no_store(malformed)


@pytest.mark.asyncio
async def test_api_07_path_and_exact_result_types_are_validated() -> None:
    executor = FakeRunExecutor()
    executor.read_result = _run_resource("run.api-07.wrong")
    artifact_executor = FakeArtifactExecutor()
    artifact_executor.resource = object()
    run = await _get(_app(executor), f"/api/v1/runs/{RUN_ID}")
    step = await _get(_app(executor), f"/api/v1/runs/{RUN_ID}/steps/step.api-07.01")
    action = await _get(_app(executor), "/api/v1/external-actions/action.api-07.01")
    artifact = await _get(
        _app(executor, artifact_executor=artifact_executor),
        "/api/v1/artifacts/artifact.api-07.01",
    )
    assert {run.status_code, step.status_code, action.status_code, artifact.status_code} == {503}
    for response in (run, step, action, artifact):
        _assert_no_store(response)


@pytest.mark.asyncio
async def test_api_07_run_child_resources_are_path_and_cross_scope_bound() -> None:
    executor = FakeRunExecutor()
    executor.read_result = _run_detail_resource()
    executor.timeline_result = RunTimelinePage(
        run_id=RUN_ID,
        items=(_timeline_event(),),
        next_cursor=None,
    )
    executor.step_result = _step_resource()
    executor.action_result = _external_action_resource()
    app = _app(executor)

    valid = (
        await _get(app, f"/api/v1/runs/{RUN_ID}"),
        await _get(app, f"/api/v1/runs/{RUN_ID}/timeline"),
        await _get(app, f"/api/v1/runs/{RUN_ID}/steps/{STEP_ID}"),
        await _get(app, f"/api/v1/external-actions/{ACTION_ID}"),
    )
    assert {response.status_code for response in valid} == {200}
    for response in valid:
        _assert_no_store(response)

    executor.timeline_result = RunTimelinePage(
        run_id="run.cross-scope",
        items=(_timeline_event(),),
        next_cursor=None,
    )
    executor.step_result = replace(_step_resource(), run_id="run.cross-scope")
    executor.action_result = replace(
        _external_action_resource(),
        action_url="/api/v1/external-actions/action.cross-scope",
    )
    crossed = (
        await _get(app, f"/api/v1/runs/{RUN_ID}/timeline"),
        await _get(app, f"/api/v1/runs/{RUN_ID}/steps/{STEP_ID}"),
        await _get(app, f"/api/v1/external-actions/{ACTION_ID}"),
    )
    assert {response.status_code for response in crossed} == {503}
    for response in crossed:
        _assert_no_store(response)


@pytest.mark.asyncio
async def test_api_07_action_payload_is_redacted_and_result_metadata_rejected() -> None:
    result_canary = "api-07-external-action-result-secret-canary"
    proposal_canary = "api-07-external-action-proposal-secret-canary"
    executor = FakeRunExecutor()
    executor.action_result = replace(
        _external_action_resource(),
        redacted_payload={"mode": "mock", "api_key": proposal_canary},
        state="succeeded",
        receipt_id="receipt.api-07.01",
        result_status="delivered",
        completed_at=NOW,
    )

    response = await _get(_app(executor), f"/api/v1/external-actions/{ACTION_ID}")

    assert response.status_code == 200
    assert response.json()["result_safe_metadata"] is None
    assert response.json()["redacted_payload"] == {
        "mode": "mock",
        "api_key": "[REDACTED]",
    }
    assert proposal_canary not in response.text
    _assert_no_store(response)

    executor.action_result = replace(
        executor.action_result,
        result_safe_metadata={"api_key": result_canary},  # type: ignore[arg-type]
    )
    rejected = await _get(_app(executor), f"/api/v1/external-actions/{ACTION_ID}")
    assert rejected.status_code == 503
    assert result_canary not in rejected.text
    _assert_no_store(rejected)


@pytest.mark.asyncio
async def test_api_07_status_summary_has_private_strong_revalidation() -> None:
    app = _app(FakeRunExecutor())
    first = await _get(app, "/api/v1/agent-instances/status-summary")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-cache, max-age=0"
    assert first.headers["etag"] == '"instance-status-sha256-v1:' + "a" * 64 + '"'
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.json()["runtime_watermark"] == "instance-status-sha256-v1:" + "a" * 64

    unchanged = await _get(
        app,
        "/api/v1/agent-instances/status-summary",
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["cache-control"] == "private, no-cache, max-age=0"


@pytest.mark.asyncio
async def test_api_07_status_summary_preserves_all_43_catalog_ordered_items() -> None:
    executor = FakeRunExecutor()
    ordered_ids = tuple(f"inst.api-07.status.agent.{index:02d}" for index in range(1, 44))
    executor.read_instance_status_summary = cast(
        Any,
        _status_summary_method(ordered_ids),
    )
    response = await _get(_app(executor), "/api/v1/agent-instances/status-summary")
    assert response.status_code == 200
    assert [item["instance_id"] for item in response.json()["items"]] == list(ordered_ids)
    assert len(response.json()["items"]) == 43


def _status_summary_method(
    instance_ids: tuple[str, ...],
) -> Any:
    async def read_instance_status_summary(
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceStatusSummary:
        del principal
        return InstanceStatusSummary(
            scope="single-local-installation",
            items=tuple(
                InstanceRuntimeStatus(
                    instance_id=instance_id,
                    status="never_run",
                    latest_run_id=None,
                    latest_run_state=None,
                    latest_run_created_at=None,
                    latest_run_updated_at=None,
                    instance_url=f"/api/v1/agent-instances/{instance_id}",
                    latest_run_url=None,
                )
                for instance_id in instance_ids
            ),
            etag='"instance-status-sha256-v1:' + "d" * 64 + '"',
        )

    return read_instance_status_summary


@pytest.mark.asyncio
async def test_api_07_private_headers_cover_trusted_host_and_trailing_slash_errors() -> None:
    app = _app(FakeRunExecutor())
    untrusted = await _get(app, "/api/v1/runs", headers={"Host": "attacker.invalid"})
    redirected = await _get(app, "/api/v1/agent-instances/status-summary/")
    assert untrusted.status_code == 400
    assert redirected.status_code in {307, 308}
    _assert_no_store(untrusted)
    _assert_no_store(redirected)


@pytest.mark.asyncio
async def test_api_07_dynamic_instance_detail_is_coherent_and_revalidated() -> None:
    static = await _get(_app(), f"/api/v1/agent-instances/{INSTANCE_ID}")
    executor = FakeRunExecutor()
    dynamic_app = _app(executor)
    dynamic = await _get(dynamic_app, f"/api/v1/agent-instances/{INSTANCE_ID}")
    assert static.status_code == dynamic.status_code == 200
    assert "runtimeStatus" not in static.json()
    assert dynamic.json()["runtimeStatus"]["latestRunId"] == RUN_ID
    assert dynamic.json()["recentRuns"][0]["id"] == RUN_ID
    assert dynamic.headers["etag"] != static.headers["etag"]
    assert dynamic.headers["cache-control"] == "private, no-cache"
    assert dynamic.headers["x-content-type-options"] == "nosniff"
    assert "authorization" in dynamic.headers["vary"].casefold()
    assert executor.status_reads == 2

    unchanged = await _get(
        dynamic_app,
        f"/api/v1/agent-instances/{INSTANCE_ID}",
        headers={"If-None-Match": dynamic.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.headers["cache-control"] == "private, no-cache"
    assert unchanged.headers["x-content-type-options"] == "nosniff"

    executor.drift_status = True
    executor.status_reads = 0
    drifted = await _get(_app(executor), f"/api/v1/agent-instances/{INSTANCE_ID}")
    assert drifted.status_code == 503
    _assert_no_store(drifted)


@pytest.mark.asyncio
async def test_api_07_configured_malformed_runtime_enrichment_fails_after_auth() -> None:
    path = f"/api/v1/agent-instances/{INSTANCE_ID}"

    malformed = await _get(_app(object()), path)
    assert malformed.status_code == 503
    _assert_no_store(malformed)

    unauthorized = await _get(
        _app(object(), principal=service_principal()),
        path,
    )
    assert unauthorized.status_code == 403
    _assert_no_store(unauthorized)

    static = await _get(_app(), path)
    assert static.status_code == 200
    assert "runtimeStatus" not in static.json()
    assert static.headers["cache-control"] == "private, no-cache"


@pytest.mark.asyncio
async def test_api_07_artifact_detail_exposes_only_redacted_json_and_keyed_digest() -> None:
    executor = FakeArtifactExecutor()
    response = await _get(
        _app(FakeRunExecutor(), artifact_executor=executor),
        f"/api/v1/artifacts/{ARTIFACT_ID}",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["payload_digest"] == "artifact-hmac-sha256-v1:" + "c" * 64
    assert body["redacted_payload"]["private_value"] == "[REDACTED]"
    assert "payload_hash" not in body
    assert "raw-private-canary" not in response.text
    assert response.headers["content-type"].startswith("application/json")
    _assert_no_store(response)

    executor.resource = replace(
        _artifact_resource(),
        classification="secret",
        redacted_payload={"non_obvious_private_key": "secret-artifact-canary"},
    )
    secret = await _get(
        _app(FakeRunExecutor(), artifact_executor=executor),
        f"/api/v1/artifacts/{ARTIFACT_ID}",
    )
    assert secret.status_code == 503
    assert "secret-artifact-canary" not in secret.text
    _assert_no_store(secret)


@pytest.mark.asyncio
async def test_api_07_audit_feed_exposes_fixed_watermark_and_rechecks_filters() -> None:
    executor = FakeAuditExecutor()
    executor.page = AuditPage(
        endpoint_version=AUDIT_FEED_ENDPOINT_VERSION,
        high_watermark=42,
        items=(_audit_resource(),),
        next_cursor="audit-feed-v1.opaque-next",
    )
    response = await _get(
        _app(FakeRunExecutor(), audit_executor=executor),
        f"/api/v1/audit-events?run_id={RUN_ID}&event_type=run.received&limit=1",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["high_watermark"] == 42
    assert body["items"][0]["sequence"] == 42
    assert body["next_cursor"] == "audit-feed-v1.opaque-next"
    assert executor.queries[0].run_id == RUN_ID
    assert executor.queries[0].event_type == "run.received"
    _assert_no_store(response)

    executor.page = AuditPage(
        endpoint_version=AUDIT_FEED_ENDPOINT_VERSION,
        high_watermark=42,
        items=(_audit_resource(run_id="run.api-07.cross-scope"),),
        next_cursor=None,
    )
    crossed = await _get(
        _app(FakeRunExecutor(), audit_executor=executor),
        f"/api/v1/audit-events?run_id={RUN_ID}",
    )
    assert crossed.status_code == 503
    _assert_no_store(crossed)

    executor.error = AuditResourceServiceError(
        "audit_cursor_invalid",
        "private-cursor-canary",
    )
    bad_cursor = await _get(
        _app(FakeRunExecutor(), audit_executor=executor),
        "/api/v1/audit-events?cursor=wrong-endpoint-cursor",
    )
    assert bad_cursor.status_code == 422
    assert "private-cursor-canary" not in bad_cursor.text
    _assert_no_store(bad_cursor)


def test_api_07_openapi_has_all_required_read_operations_and_no_mutation() -> None:
    document = _app().openapi()
    expected = {
        "/api/v1/runs": "listRuns",
        "/api/v1/runs/{run_id}": "getRun",
        "/api/v1/runs/{run_id}/timeline": "getRunTimeline",
        "/api/v1/runs/{run_id}/artifacts": "listRunArtifacts",
        "/api/v1/artifacts/{artifact_id}": "getArtifact",
        "/api/v1/audit-events": "listAuditEvents",
        "/api/v1/runs/{run_id}/steps/{step_id}": "getRunStep",
        "/api/v1/external-actions/{action_id}": "getExternalAction",
        "/api/v1/agent-instances/status-summary": "getAgentInstanceStatusSummary",
    }
    for path, operation_id in expected.items():
        operation = document["paths"][path]["get"]
        assert operation["operationId"] == operation_id
        assert "503" in operation["responses"]

    runtime_status_schema = document["components"]["schemas"][
        "marketing_agents__api__schemas__catalog__InstanceRuntimeStatusView"
    ]
    latest_state = runtime_status_schema["properties"]["latestRunState"]
    state_schema = next(
        branch for branch in latest_state["anyOf"] if branch.get("type") == "string"
    )
    assert set(state_schema["enum"]) == {
        "received",
        "validated",
        "planned",
        "awaiting_approval",
        "executing",
        "completed",
        "failed",
        "rejected",
        "cancelled",
    }
    artifact_detail_schema = document["components"]["schemas"]["ArtifactResourceView"]
    assert set(artifact_detail_schema["properties"]["classification"]["enum"]) == {
        "public",
        "internal",
        "personal",
        "sensitive",
    }
    artifact_source_schema = document["components"]["schemas"]["ArtifactSourceView"]
    assert set(artifact_source_schema["properties"]["classification"]["enum"]) == {
        "public",
        "internal",
        "personal",
        "sensitive",
    }
    action_schema = document["components"]["schemas"]["ExternalActionView"]
    assert action_schema["properties"]["result_safe_metadata"]["type"] == "null"
    assert "post" not in document["paths"]["/api/v1/runs/{run_id}"]
