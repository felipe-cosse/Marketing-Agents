"""Authenticated read-only projections for Runs and runtime child resources."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from marketing_agents.application.policies.runtime_resource_authorization import (
    RUNTIME_RESOURCE_INSTALLATION_SCOPE,
    RuntimeResourceAuthorizationError,
    authorize_runtime_resource_reader,
)
from marketing_agents.application.ports.repositories import (
    ExternalActionRepositoryConflict,
    InspectableRun,
    InspectableRunPlan,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.application.services.artifact_resources import (
    ArtifactSummary,
    project_artifact_summary,
)
from marketing_agents.application.services.connector_output_projection import (
    bounded_connector_output_projection,
)
from marketing_agents.domain.audit import AuditEvent
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import ConnectorActionReceipt, ExternalAction, RunStep
from marketing_agents.domain.enums import ApprovalStatus, Effect, RunState
from marketing_agents.domain.execution_control import RunExecutionControl
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.run_lifecycle import RunLifecycleCommand
from marketing_agents.domain.runtime_policy import (
    effective_call_timeout_seconds,
    run_policy_projection,
    step_policy_projection,
)
from marketing_agents.domain.step_lifecycle import StepStateTransition
from marketing_agents.domain.validation import require_id, require_utc
from marketing_agents.security.redaction import redact

DEFAULT_RUN_PAGE_SIZE = 25
MAX_RUN_PAGE_SIZE = 100
DEFAULT_TIMELINE_PAGE_SIZE = 50
MAX_TIMELINE_PAGE_SIZE = 100
MAX_RECENT_INSTANCE_RUNS = 10
MAX_RUN_CURSOR_LENGTH = 1_024
_RUN_CURSOR_PREFIX = "run-page-v1."
_TIMELINE_CURSOR_PREFIX = "run-timeline-v1."
_RUN_FILTER_DOMAIN = b"marketing-agents:run-page-filter:v1\x00"
_STATUS_ETAG_DOMAIN = b"marketing-agents:instance-runtime-status:v1\x00"


class RunResourceServiceError(ValueError):
    """Stable non-sensitive failure raised by runtime resource queries."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RunListQuery:
    state: RunState | None = None
    instance_id: str | None = None
    workflow_id: str | None = None
    created_at_from: datetime | None = None
    created_at_to: datetime | None = None
    cursor: str | None = field(default=None, repr=False)
    limit: int = DEFAULT_RUN_PAGE_SIZE

    def __post_init__(self) -> None:
        if self.state is not None and type(self.state) is not RunState:
            raise ValueError("run state filter must use the exact enum")
        for identifier, name in (
            (self.instance_id, "run instance filter"),
            (self.workflow_id, "run workflow filter"),
        ):
            if identifier is not None:
                require_id(identifier, name)
        for timestamp, name in (
            (self.created_at_from, "run lower time bound"),
            (self.created_at_to, "run upper time bound"),
        ):
            if timestamp is not None:
                require_utc(timestamp, name)
        if (
            self.created_at_from is not None
            and self.created_at_to is not None
            and self.created_at_from > self.created_at_to
        ):
            raise ValueError("run lower time bound cannot follow upper bound")
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_RUN_PAGE_SIZE:
            raise ValueError("run page limit is outside the supported range")
        _require_cursor(self.cursor, "run page cursor")


@dataclass(frozen=True, slots=True)
class RunTimelineQuery:
    cursor: str | None = field(default=None, repr=False)
    limit: int = DEFAULT_TIMELINE_PAGE_SIZE

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_TIMELINE_PAGE_SIZE:
            raise ValueError("run timeline limit is outside the supported range")
        _require_cursor(self.cursor, "run timeline cursor")


@dataclass(frozen=True, slots=True)
class RunTransitionResource:
    sequence: int
    command: str
    previous_state: str | None
    new_state: str
    reason_code: str
    occurred_at: datetime
    expected_version: int
    resulting_version: int
    completed_effect_count: int
    outcome_unknown_effect_count: int


@dataclass(frozen=True, slots=True)
class RunPlanSelectedInstanceResource:
    instance_id: str
    template_id: str
    configuration_revision: int
    display_order: int
    source_ordinal: int | None
    selection_order: int
    target: bool
    instance_url: str
    template_url: str


@dataclass(frozen=True, slots=True)
class RunRoutingAssignmentResource:
    slot_key: str
    instance_id: str
    template_id: str
    required_capability_ids: tuple[str, ...]
    assignment_order: int
    instance_url: str
    template_url: str


@dataclass(frozen=True, slots=True)
class RunStepTransitionResource:
    sequence: int
    command: str
    previous_state: str | None
    new_state: str
    reason_code: str
    occurred_at: datetime
    expected_version: int
    resulting_version: int


@dataclass(frozen=True, slots=True)
class RunStepResource:
    step_id: str
    run_id: str
    key: str
    kind: str
    selected_instance_id: str
    template_id: str
    dependency_keys: tuple[str, ...]
    capability_id: str
    effect: str
    state: str
    ordinal: int
    source_order: int
    configuration_revision: int
    connector_family: str
    routing_slot_key: str | None
    binding_id: str | None
    binding_configuration_revision: int | None
    request_schema_id: str | None
    result_schema_id: str | None
    result_schema_hash: str | None
    data_classification: str
    idempotency_support: str
    timeout_seconds: int | None
    runtime_policy: Mapping[str, Any]
    approval_policy_id: str
    approval_required_roles: tuple[str, ...]
    approval_required_scopes: tuple[str, ...]
    approval_expires_after_seconds: int | None
    approval_allow_self_approval: bool | None
    terminal_result: bool
    created_at: datetime
    updated_at: datetime
    version: int
    terminal_reason_code: str | None
    transitions: tuple[RunStepTransitionResource, ...]
    step_url: str
    run_url: str
    instance_url: str
    template_url: str


@dataclass(frozen=True, slots=True)
class RunPlanResource:
    plan_hash: str
    workflow_id: str
    workflow_version: int
    workflow_definition_hash: str
    catalog_content_hash: str
    graph_hash: str
    routing_hash: str
    approval_required: bool
    step_count: int
    runtime_policy: Mapping[str, int]
    created_at: datetime
    selected_instances: tuple[RunPlanSelectedInstanceResource, ...]
    routing_assignments: tuple[RunRoutingAssignmentResource, ...]
    steps: tuple[RunStepResource, ...]


@dataclass(frozen=True, slots=True)
class ExternalActionResource:
    action_id: str
    run_id: str
    proposal_revision: int
    step_id: str
    step_key: str
    template_id: str
    instance_id: str
    action_type: str
    capability_id: str
    connector_family: str
    binding_id: str
    destination_summary: str
    redacted_payload: Mapping[str, Any] = field(repr=False)
    payload_schema_id: str
    state: str
    created_at: datetime
    updated_at: datetime
    version: int
    delivery_attempt_count: int
    delivery_attempt_limit: int
    approval_policy_id: str
    approval_required_roles: tuple[str, ...]
    approval_required_scopes: tuple[str, ...]
    approval_expires_after_seconds: int
    approval_allow_self_approval: bool
    terminal_reason_code: str | None
    superseded_by_action_id: str | None
    superseded_at: datetime | None
    receipt_id: str | None
    result_status: str | None
    result_safe_metadata: None
    completed_at: datetime | None
    action_url: str
    run_url: str
    step_url: str
    instance_url: str
    template_url: str


@dataclass(frozen=True, slots=True)
class RunResource:
    run_id: str
    work_item_id: str
    instance_id: str
    workflow_id: str
    trigger_id: str
    source: str
    mode: str
    state: str
    catalog_hash: str
    configuration_revision: int
    approval_required: bool | None
    terminal_reason_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    transitions: tuple[RunTransitionResource, ...]
    plan: RunPlanResource | None
    execution_control: RunExecutionControlResource | None
    pending_approvals: tuple[PendingApprovalSummary, ...]
    artifact_summaries: tuple[ArtifactSummary, ...]
    artifacts_truncated: bool
    external_actions: tuple[ExternalActionResource, ...]
    run_url: str
    timeline_url: str
    artifacts_url: str
    instance_url: str


@dataclass(frozen=True, slots=True)
class RunPage:
    items: tuple[RunResource, ...]
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class RunTimelineEvent:
    event_id: str
    sequence: int
    schema_version: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    outcome: str
    actor_id: str
    actor_source: str
    auth_method: str
    correlation_id: str
    occurred_at: datetime
    step_id: str | None
    action_id: str | None
    approval_request_id: str | None
    artifact_id: str | None
    attempted_command: str | None
    previous_state: str | None
    new_state: str | None
    reason_code: str | None
    metadata: Mapping[str, Any] = field(repr=False)
    metadata_classification: str
    metadata_expires_at: datetime
    metadata_expired: bool
    run_url: str
    step_url: str | None
    action_url: str | None
    approval_url: str | None
    artifact_url: str | None


@dataclass(frozen=True, slots=True)
class RunTimelinePage:
    run_id: str
    items: tuple[RunTimelineEvent, ...]
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class InstanceRuntimeStatus:
    instance_id: str
    status: str
    latest_run_id: str | None
    latest_run_state: str | None
    latest_run_created_at: datetime | None
    latest_run_updated_at: datetime | None
    instance_url: str
    latest_run_url: str | None


@dataclass(frozen=True, slots=True)
class InstanceStatusSummary:
    scope: str
    items: tuple[InstanceRuntimeStatus, ...]
    etag: str


@dataclass(frozen=True, slots=True)
class RunExecutionControlResource:
    run_timeout_seconds: int
    max_model_calls: int
    max_tool_calls: int
    model_calls: int
    tool_calls: int
    remaining_model_calls: int
    remaining_tool_calls: int
    started_at: datetime | None
    deadline_at: datetime | None
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class PendingApprovalSummary:
    approval_id: str
    action_id: str
    step_id: str
    status: str
    destination_summary: str
    requested_at: datetime
    expires_at: datetime
    is_expired: bool
    approval_url: str
    action_url: str
    step_url: str


@dataclass(frozen=True, slots=True)
class _RunCursorBoundary:
    created_at: datetime
    run_id: str


def _require_cursor(value: str | None, name: str) -> None:
    if value is not None and (
        type(value) is not str or not value or len(value) > MAX_RUN_CURSOR_LENGTH
    ):
        raise ValueError(f"{name} is invalid")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds")


def _run_filter_fingerprint(query: RunListQuery) -> str:
    return hashlib.sha256(
        _RUN_FILTER_DOMAIN
        + canonical_json_bytes(
            {
                "created_at_from": _iso(query.created_at_from),
                "created_at_to": _iso(query.created_at_to),
                "instance_id": query.instance_id,
                "state": None if query.state is None else query.state.value,
                "workflow_id": query.workflow_id,
            }
        )
    ).hexdigest()


def _encode_run_cursor(resource: RunResource, query: RunListQuery) -> str:
    payload = canonical_json_bytes(
        {
            "created_at": resource.created_at.isoformat(timespec="microseconds"),
            "endpoint": "runs-v1",
            "filter": _run_filter_fingerprint(query),
            "id": resource.run_id,
            "version": 1,
        }
    )
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_RUN_CURSOR_PREFIX}{token}"


def _decode_run_cursor(query: RunListQuery) -> _RunCursorBoundary | None:
    if query.cursor is None:
        return None
    decoded, raw = _decode_payload(query.cursor, _RUN_CURSOR_PREFIX, "run_cursor_invalid")
    if (
        set(decoded) != {"created_at", "endpoint", "filter", "id", "version"}
        or decoded.get("version") != 1
        or decoded.get("endpoint") != "runs-v1"
        or type(decoded.get("filter")) is not str
        or type(decoded.get("id")) is not str
        or type(decoded.get("created_at")) is not str
    ):
        raise _run_cursor_error()
    try:
        if not hmac.compare_digest(decoded["filter"], _run_filter_fingerprint(query)):
            raise ValueError("run cursor filters changed")
        created_at = datetime.fromisoformat(decoded["created_at"])
        require_utc(created_at, "run cursor time")
        require_id(decoded["id"], "run cursor ID")
    except (TypeError, ValueError):
        raise _run_cursor_error() from None
    canonical = _RUN_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if not hmac.compare_digest(canonical, query.cursor):
        raise _run_cursor_error()
    return _RunCursorBoundary(created_at=created_at, run_id=decoded["id"])


def _encode_timeline_cursor(run_id: str, sequence: int) -> str:
    payload = canonical_json_bytes(
        {
            "endpoint": f"run-timeline:{run_id}",
            "run_id": run_id,
            "sequence": sequence,
            "version": 1,
        }
    )
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_TIMELINE_CURSOR_PREFIX}{token}"


def _decode_timeline_cursor(run_id: str, query: RunTimelineQuery) -> int:
    if query.cursor is None:
        return 0
    decoded, raw = _decode_payload(
        query.cursor,
        _TIMELINE_CURSOR_PREFIX,
        "run_timeline_cursor_invalid",
    )
    if (
        set(decoded) != {"endpoint", "run_id", "sequence", "version"}
        or decoded.get("version") != 1
        or decoded.get("endpoint") != f"run-timeline:{run_id}"
        or decoded.get("run_id") != run_id
        or type(decoded.get("sequence")) is not int
        or isinstance(decoded.get("sequence"), bool)
        or decoded["sequence"] < 1
    ):
        raise _timeline_cursor_error()
    canonical = _TIMELINE_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if not hmac.compare_digest(canonical, query.cursor):
        raise _timeline_cursor_error()
    return cast(int, decoded["sequence"])


def _decode_payload(
    cursor: str,
    prefix: str,
    error_code: str,
) -> tuple[dict[str, Any], bytes]:
    if not cursor.startswith(prefix):
        raise RunResourceServiceError(error_code, "runtime page cursor is invalid")
    encoded = cursor[len(prefix) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        decoded = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise RunResourceServiceError(error_code, "runtime page cursor is invalid") from None
    if type(decoded) is not dict:
        raise RunResourceServiceError(error_code, "runtime page cursor is invalid")
    return cast(dict[str, Any], decoded), raw


def _run_cursor_error() -> RunResourceServiceError:
    return RunResourceServiceError("run_cursor_invalid", "run page cursor is invalid")


def _timeline_cursor_error() -> RunResourceServiceError:
    return RunResourceServiceError(
        "run_timeline_cursor_invalid",
        "run timeline cursor is invalid",
    )


def _validate_inspectable_run(item: InspectableRun) -> None:
    if type(item) is not InspectableRun:
        raise ValueError("run inspection contract is invalid")
    run = item.run
    if item.work_item.id != run.work_item_id:
        raise ValueError("run no longer binds its admitted work")
    expected_sequences = tuple(range(1, len(item.transitions) + 1))
    if tuple(value.sequence for value in item.transitions) != expected_sequences:
        raise ValueError("run transition history is not contiguous")
    latest = item.transitions[-1]
    if latest.new_state is not run.state or latest.resulting_version != run.version:
        raise ValueError("run snapshot does not match its transition history")


def _project_transition(item: Any) -> RunTransitionResource:
    return RunTransitionResource(
        sequence=item.sequence,
        command=item.command.value,
        previous_state=None if item.previous_state is None else item.previous_state.value,
        new_state=item.new_state.value,
        reason_code=item.reason_code,
        occurred_at=item.occurred_at,
        expected_version=item.expected_version,
        resulting_version=item.resulting_version,
        completed_effect_count=item.completed_effect_count,
        outcome_unknown_effect_count=item.outcome_unknown_effect_count,
    )


def _project_step(
    step: RunStep,
    *,
    transitions: tuple[StepStateTransition, ...] = (),
) -> RunStepResource:
    if type(step) is not RunStep:
        raise ValueError("run step contract is invalid")
    if transitions:
        expected_sequences = tuple(range(1, len(transitions) + 1))
        if (
            any(
                type(value) is not StepStateTransition
                or value.step_id != step.id
                or value.run_id != step.run_id
                for value in transitions
            )
            or tuple(value.sequence for value in transitions) != expected_sequences
            or transitions[-1].new_state is not step.state
            or transitions[-1].resulting_version != step.version
        ):
            raise ValueError("run step snapshot does not match its transition history")
    return RunStepResource(
        step_id=step.id,
        run_id=step.run_id,
        key=step.key,
        kind=step.kind,
        selected_instance_id=step.selected_instance_id,
        template_id=step.template_id,
        dependency_keys=step.dependency_keys,
        capability_id=step.capability_id,
        effect=step.effect.value,
        state=step.state.value,
        ordinal=step.ordinal,
        source_order=step.source_order,
        configuration_revision=step.configuration_revision,
        connector_family=step.connector_family,
        routing_slot_key=step.routing_slot_key,
        binding_id=step.binding_id,
        binding_configuration_revision=step.binding_configuration_revision,
        request_schema_id=step.request_schema_id,
        result_schema_id=step.result_schema_id,
        result_schema_hash=step.result_schema_hash,
        data_classification=step.data_classification.value,
        idempotency_support=step.idempotency_support,
        timeout_seconds=step.timeout_seconds,
        runtime_policy=step_policy_projection(step.runtime_policy),
        approval_policy_id=step.approval_policy_id,
        approval_required_roles=step.approval_required_roles,
        approval_required_scopes=step.approval_required_scopes,
        approval_expires_after_seconds=step.approval_expires_after_seconds,
        approval_allow_self_approval=step.approval_allow_self_approval,
        terminal_result=step.terminal_result,
        created_at=step.created_at,
        updated_at=step.updated_at,
        version=step.version,
        terminal_reason_code=step.terminal_reason_code,
        transitions=tuple(
            RunStepTransitionResource(
                sequence=value.sequence,
                command=value.command.value,
                previous_state=(
                    None if value.previous_state is None else value.previous_state.value
                ),
                new_state=value.new_state.value,
                reason_code=value.reason_code,
                occurred_at=value.occurred_at,
                expected_version=value.expected_version,
                resulting_version=value.resulting_version,
            )
            for value in transitions
        ),
        step_url=f"/api/v1/runs/{step.run_id}/steps/{step.id}",
        run_url=f"/api/v1/runs/{step.run_id}",
        instance_url=f"/api/v1/agent-instances/{step.selected_instance_id}",
        template_url=f"/api/v1/agent-templates/{step.template_id}",
    )


def _project_plan(run_id: str, item: InspectableRunPlan) -> RunPlanResource:
    if type(item) is not InspectableRunPlan or item.plan.run_id != run_id:
        raise ValueError("run plan inspection contract is invalid")
    plan = item.plan
    if len(item.steps) != plan.step_count:
        raise ValueError("run plan step count no longer matches")
    if (
        any(value.plan_hash != plan.plan_hash for value in item.selected_instances)
        or any(value.plan_hash != plan.plan_hash for value in item.assignments)
        or any(value.plan_hash != plan.plan_hash for value in item.steps)
    ):
        raise ValueError("run plan child no longer binds its plan")
    return RunPlanResource(
        plan_hash=plan.plan_hash,
        workflow_id=plan.workflow_id,
        workflow_version=plan.workflow_version,
        workflow_definition_hash=plan.workflow_definition_hash,
        catalog_content_hash=plan.catalog_content_hash,
        graph_hash=plan.graph_hash,
        routing_hash=plan.routing_hash,
        approval_required=plan.approval_required,
        step_count=plan.step_count,
        runtime_policy=run_policy_projection(plan.runtime_policy),
        created_at=plan.created_at,
        selected_instances=tuple(
            RunPlanSelectedInstanceResource(
                instance_id=value.instance_id,
                template_id=value.template_id,
                configuration_revision=value.configuration_revision,
                display_order=value.display_order,
                source_ordinal=value.source_ordinal,
                selection_order=value.selection_order,
                target=value.target,
                instance_url=f"/api/v1/agent-instances/{value.instance_id}",
                template_url=f"/api/v1/agent-templates/{value.template_id}",
            )
            for value in item.selected_instances
        ),
        routing_assignments=tuple(
            RunRoutingAssignmentResource(
                slot_key=value.slot_key,
                instance_id=value.instance_id,
                template_id=value.template_id,
                required_capability_ids=value.required_capability_ids,
                assignment_order=value.assignment_order,
                instance_url=f"/api/v1/agent-instances/{value.instance_id}",
                template_url=f"/api/v1/agent-templates/{value.template_id}",
            )
            for value in item.assignments
        ),
        steps=tuple(_project_step(value) for value in item.steps),
    )


def _plain_mapping(value: object) -> Mapping[str, Any]:
    decoded = json.loads(canonical_json_bytes(value))
    if type(decoded) is not dict:
        raise ValueError("safe runtime projection is not an object")
    return cast(dict[str, Any], decoded)


def _plain_redacted_mapping(value: object) -> Mapping[str, Any]:
    """Apply the central conservative redactor at the public read boundary."""

    return _plain_mapping(redact(value))


def _validate_action_result_receipt(
    action: ExternalAction,
    receipt: ConnectorActionReceipt | None,
    step: RunStep,
) -> None:
    """Bind a succeeded action result to its authoritative durable receipt."""

    result = action.result
    if result is None:
        return
    if type(receipt) is not ConnectorActionReceipt:
        raise ValueError("external action result lacks its exact durable receipt")
    expected_metadata = bounded_connector_output_projection(
        receipt.safe_metadata,
        step.runtime_policy.budget.max_output_bytes,
    )
    if (
        receipt.external_action_id != action.id
        or receipt.connector_binding_id != action.connector_binding_id
        or receipt.idempotency_key != action.idempotency_key
        or receipt.action_hash != action.action_hash
        or receipt.capability_id != action.envelope.capability_id
        or receipt.receipt_id != result.receipt_id
        or receipt.status != result.status
        or result.completed_at != action.updated_at
        or receipt.created_at > result.completed_at
        or canonical_json_bytes(expected_metadata) != canonical_json_bytes(result.safe_metadata)
    ):
        raise ValueError("external action result lacks its exact durable receipt")


def _validate_action_step_binding(
    action: ExternalAction,
    step: RunStep,
    *,
    run_id: str,
    plan_hash: str,
) -> None:
    """Revalidate registration-time action authority against the sealed step."""

    if type(action) is not ExternalAction or type(step) is not RunStep:
        raise ValueError("external action contract is invalid")
    action.proposal.__post_init__()
    envelope = action.envelope
    contract = action.delivery_contract
    policy = action.approval_policy
    if (
        envelope.run_id != run_id
        or envelope.plan_hash != plan_hash
        or step.run_id != envelope.run_id
        or step.plan_hash != envelope.plan_hash
        or step.id != envelope.step_id
        or step.key != envelope.step_key
        or step.effect is not Effect.WRITE
        or step.template_id != envelope.template_id
        or step.selected_instance_id != envelope.instance_id
        or step.capability_id != envelope.capability_id
        or step.connector_family != envelope.connector_family
        or step.binding_id != envelope.binding_id
        or step.request_schema_id != envelope.payload_schema_id
        or step.binding_configuration_revision != contract.binding_configuration_revision
        or step.idempotency_support != contract.idempotency_support
        or effective_call_timeout_seconds(step.runtime_policy, step.timeout_seconds)
        != contract.timeout_seconds
        or step.approval_policy_id != policy.policy_id
        or frozenset(step.approval_required_roles) != policy.required_roles
        or frozenset(step.approval_required_scopes) != policy.required_scopes
        or step.approval_expires_after_seconds != policy.expires_after_seconds
        or step.approval_allow_self_approval != policy.allow_self_approval
    ):
        raise ValueError("external action no longer binds its sealed WRITE step")


def _project_action(action: ExternalAction) -> ExternalActionResource:
    if type(action) is not ExternalAction:
        raise ValueError("external action contract is invalid")
    action.proposal.__post_init__()
    envelope = action.proposal.envelope
    projection = action.proposal.redacted_projection
    payload = _plain_redacted_mapping(projection["payload"])
    result = action.result
    return ExternalActionResource(
        action_id=envelope.action_id,
        run_id=envelope.run_id,
        proposal_revision=envelope.proposal_revision,
        step_id=envelope.step_id,
        step_key=envelope.step_key,
        template_id=envelope.template_id,
        instance_id=envelope.instance_id,
        action_type=envelope.action_type,
        capability_id=envelope.capability_id,
        connector_family=envelope.connector_family,
        binding_id=envelope.binding_id,
        destination_summary=cast(str, projection["destination"]),
        redacted_payload=payload,
        payload_schema_id=envelope.payload_schema_id,
        state=action.state.value,
        created_at=action.created_at,
        updated_at=action.updated_at,
        version=action.version,
        delivery_attempt_count=action.delivery_attempt_count,
        delivery_attempt_limit=action.delivery_attempt_limit,
        approval_policy_id=action.approval_policy.policy_id,
        approval_required_roles=tuple(sorted(action.approval_policy.required_roles)),
        approval_required_scopes=tuple(sorted(action.approval_policy.required_scopes)),
        approval_expires_after_seconds=action.approval_policy.expires_after_seconds,
        approval_allow_self_approval=action.approval_policy.allow_self_approval,
        terminal_reason_code=action.terminal_reason_code,
        superseded_by_action_id=action.superseded_by_action_id,
        superseded_at=action.superseded_at,
        receipt_id=None if result is None else result.receipt_id,
        result_status=None if result is None else result.status,
        result_safe_metadata=None,
        completed_at=None if result is None else result.completed_at,
        action_url=f"/api/v1/external-actions/{envelope.action_id}",
        run_url=f"/api/v1/runs/{envelope.run_id}",
        step_url=f"/api/v1/runs/{envelope.run_id}/steps/{envelope.step_id}",
        instance_url=f"/api/v1/agent-instances/{envelope.instance_id}",
        template_url=f"/api/v1/agent-templates/{envelope.template_id}",
    )


def _project_run(
    item: InspectableRun,
    *,
    plan: RunPlanResource | None = None,
    execution_control: RunExecutionControlResource | None = None,
    pending_approvals: tuple[PendingApprovalSummary, ...] = (),
    artifact_summaries: tuple[ArtifactSummary, ...] = (),
    artifacts_truncated: bool = False,
    actions: tuple[ExternalActionResource, ...] = (),
) -> RunResource:
    _validate_inspectable_run(item)
    run = item.run
    work = item.work_item
    return RunResource(
        run_id=run.id,
        work_item_id=run.work_item_id,
        instance_id=work.instance_id,
        workflow_id=work.workflow_id,
        trigger_id=work.trigger_id,
        source=work.source,
        mode=work.mode.value,
        state=run.state.value,
        catalog_hash=run.catalog_hash,
        configuration_revision=run.configuration_revision,
        approval_required=run.approval_required,
        terminal_reason_code=run.terminal_reason_code,
        created_at=run.created_at,
        updated_at=run.updated_at,
        version=run.version,
        transitions=tuple(_project_transition(value) for value in item.transitions),
        plan=plan,
        execution_control=execution_control,
        pending_approvals=pending_approvals,
        artifact_summaries=artifact_summaries,
        artifacts_truncated=artifacts_truncated,
        external_actions=actions,
        run_url=f"/api/v1/runs/{run.id}",
        timeline_url=f"/api/v1/runs/{run.id}/timeline",
        artifacts_url=f"/api/v1/runs/{run.id}/artifacts",
        instance_url=f"/api/v1/agent-instances/{work.instance_id}",
    )


def _project_execution_control(
    control: RunExecutionControl,
    *,
    run_id: str,
) -> RunExecutionControlResource:
    if type(control) is not RunExecutionControl or control.run_id != run_id:
        raise ValueError("execution control no longer binds the Run")
    return RunExecutionControlResource(
        run_timeout_seconds=control.run_timeout_seconds,
        max_model_calls=control.max_model_calls,
        max_tool_calls=control.max_tool_calls,
        model_calls=control.model_calls,
        tool_calls=control.tool_calls,
        remaining_model_calls=control.max_model_calls - control.model_calls,
        remaining_tool_calls=control.max_tool_calls - control.tool_calls,
        started_at=control.started_at,
        deadline_at=control.deadline_at,
        cancel_requested_at=control.cancel_requested_at,
        created_at=control.created_at,
        updated_at=control.updated_at,
        version=control.version,
    )


def _project_pending_approval(value: Any, *, run_id: str, now: datetime) -> PendingApprovalSummary:
    require_utc(now, "pending approval projection time")
    request = value.request
    if value.status is not ApprovalStatus.PENDING or request.run_id != run_id:
        raise ValueError("pending approval no longer binds the Run")
    return PendingApprovalSummary(
        approval_id=request.id,
        action_id=request.action_id,
        step_id=request.step_id,
        status=value.status.value,
        destination_summary=request.redacted_destination,
        requested_at=request.requested_at,
        expires_at=request.expires_at,
        is_expired=now >= request.expires_at,
        approval_url=f"/api/v1/approvals/{request.id}",
        action_url=f"/api/v1/external-actions/{request.action_id}",
        step_url=f"/api/v1/runs/{run_id}/steps/{request.step_id}",
    )


def _project_timeline_event(
    event: AuditEvent,
    *,
    run_id: str,
    now: datetime,
) -> RunTimelineEvent:
    if type(event) is not AuditEvent or event.run_id != run_id:
        raise ValueError("run audit event no longer binds the Run")
    event.draft.verify_integrity()
    event.safe_metadata.verify_integrity()
    require_utc(now, "run timeline projection time")
    if type(event.run_sequence) is not int or event.run_sequence < 1:
        raise ValueError("run audit sequence is invalid")
    metadata_expired = now >= event.safe_metadata.expires_at
    metadata = {} if metadata_expired else _plain_mapping(event.safe_metadata.values)
    return RunTimelineEvent(
        event_id=event.id,
        sequence=event.run_sequence,
        schema_version=event.schema_version,
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        outcome=event.outcome.value,
        actor_id=event.actor_id,
        actor_source=event.actor_source.value,
        auth_method=event.auth_method,
        correlation_id=event.correlation_id,
        occurred_at=event.occurred_at,
        step_id=event.step_id,
        action_id=event.action_id,
        approval_request_id=event.approval_request_id,
        artifact_id=event.artifact_id,
        attempted_command=event.attempted_command,
        previous_state=event.previous_state,
        new_state=event.new_state,
        reason_code=event.reason_code,
        metadata=metadata,
        metadata_classification=event.safe_metadata.classification.value,
        metadata_expires_at=event.safe_metadata.expires_at,
        metadata_expired=metadata_expired,
        run_url=f"/api/v1/runs/{run_id}",
        step_url=(
            None if event.step_id is None else f"/api/v1/runs/{run_id}/steps/{event.step_id}"
        ),
        action_url=(
            None if event.action_id is None else f"/api/v1/external-actions/{event.action_id}"
        ),
        approval_url=(
            None
            if event.approval_request_id is None
            else f"/api/v1/approvals/{event.approval_request_id}"
        ),
        artifact_url=(
            None if event.artifact_id is None else f"/api/v1/artifacts/{event.artifact_id}"
        ),
    )


class RunResourceService:
    """Authorize and project immutable runtime state without invoking adapters."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        *,
        catalog_instance_ids: tuple[str, ...],
        utc_now: Callable[[], datetime],
    ) -> None:
        if not callable(unit_of_work) or not callable(utc_now):
            raise ValueError("run resources require callable dependencies")
        self._validate_instance_ids(catalog_instance_ids)
        self._unit_of_work = unit_of_work
        self._catalog_instance_ids = catalog_instance_ids
        self._utc_now = utc_now

    async def list(
        self,
        query: RunListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunPage:
        self._authorize(principal)
        if type(query) is not RunListQuery:
            raise RunResourceServiceError("run_query_invalid", "run list query is invalid")
        boundary = _decode_run_cursor(query)
        try:
            async with self._unit_of_work() as unit_of_work:
                stored = await unit_of_work.runs.list_inspectable(
                    state=query.state,
                    instance_id=query.instance_id,
                    workflow_id=query.workflow_id,
                    created_at_from=query.created_at_from,
                    created_at_to=query.created_at_to,
                    before_created_at=None if boundary is None else boundary.created_at,
                    before_run_id=None if boundary is None else boundary.run_id,
                    limit=query.limit + 1,
                )
        except (TypeError, ValueError, RuntimeError):
            raise self._corrupt() from None
        try:
            items = tuple(_project_run(value) for value in stored[: query.limit])
            boundaries = tuple((value.created_at, value.run_id) for value in items)
            if (
                boundaries != tuple(sorted(boundaries, reverse=True))
                or len(boundaries) != len(set(boundaries))
                or (
                    boundary is not None
                    and any(value >= (boundary.created_at, boundary.run_id) for value in boundaries)
                )
                or any(
                    (query.state is not None and item.run.state is not query.state)
                    or (
                        query.instance_id is not None
                        and item.work_item.instance_id != query.instance_id
                    )
                    or (
                        query.workflow_id is not None
                        and item.work_item.workflow_id != query.workflow_id
                    )
                    or (
                        query.created_at_from is not None
                        and item.run.created_at < query.created_at_from
                    )
                    or (
                        query.created_at_to is not None
                        and item.run.created_at > query.created_at_to
                    )
                    for item in stored[: query.limit]
                )
            ):
                raise ValueError("run page violates its deterministic query boundary")
        except (TypeError, ValueError):
            raise self._corrupt() from None
        next_cursor = (
            _encode_run_cursor(items[-1], query) if len(stored) > query.limit and items else None
        )
        return RunPage(items=items, next_cursor=next_cursor)

    async def read(
        self,
        run_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunResource:
        self._authorize(principal)
        self._validate_id(run_id, "run")
        try:
            async with self._unit_of_work() as unit_of_work:
                stored = await unit_of_work.runs.get_inspectable(run_id)
                plan_item = None
                control = None
                pending_approvals: tuple[Any, ...] = ()
                artifact_items: tuple[Any, ...] = ()
                actions: tuple[ExternalAction, ...] = ()
                action_receipts: dict[str, ConnectorActionReceipt | None] = {}
                if stored is not None:
                    plan_item = await unit_of_work.run_steps.get_inspectable_plan(run_id)
                    control = await unit_of_work.execution_control.get(run_id)
                    pending_approvals = await unit_of_work.approvals.list_requests(
                        status=ApprovalStatus.PENDING,
                        run_id=run_id,
                        action_id=None,
                        before_requested_at=None,
                        before_request_id=None,
                        limit=101,
                    )
                    artifact_items = await unit_of_work.artifacts.list_for_run_page(
                        run_id,
                        after_created_at=None,
                        after_artifact_id=None,
                        limit=11,
                    )
                    if plan_item is not None:
                        actions = await unit_of_work.external_actions.list_run_plan(
                            run_id,
                            plan_item.plan.plan_hash,
                        )
                        for action in actions:
                            if action.result is not None:
                                action_receipts[
                                    action.id
                                ] = await unit_of_work.connector_receipts.get(
                                    action.connector_binding_id,
                                    action.idempotency_key,
                                )
                stored_again = await unit_of_work.runs.get_inspectable(run_id)
        except (
            AttributeError,
            ExternalActionRepositoryConflict,
            TypeError,
            ValueError,
            RuntimeError,
        ):
            raise self._corrupt() from None
        if stored is None:
            if stored_again is not None:
                raise self._corrupt()
            raise RunResourceServiceError("run_not_found", "run was not found")
        if stored_again != stored:
            raise self._corrupt()
        try:
            now = self._utc_now()
            require_utc(now, "run projection time")
            recorded_plan = any(
                transition.command is RunLifecycleCommand.RECORD_PLAN
                for transition in stored.transitions
            )
            if recorded_plan != (plan_item is not None):
                raise ValueError("run plan presence disagrees with lifecycle history")
            if (plan_item is None) != (control is None):
                raise ValueError("execution control presence disagrees with the Run plan")
            if len(pending_approvals) > 100:
                raise ValueError("pending approvals exceed the bounded Run projection")
            plan = None if plan_item is None else _project_plan(run_id, plan_item)
            if plan_item is not None and (
                plan_item.plan.workflow_id != stored.work_item.workflow_id
                or plan_item.plan.catalog_content_hash != stored.run.catalog_hash
                or plan_item.plan.approval_required != stored.run.approval_required
            ):
                raise ValueError("run plan snapshot no longer binds the Run and WorkItem")
            projected_control = (
                None if control is None else _project_execution_control(control, run_id=run_id)
            )
            projected_approvals = tuple(
                _project_pending_approval(value, run_id=run_id, now=now)
                for value in pending_approvals
            )
            projected_artifacts = tuple(
                project_artifact_summary(value) for value in artifact_items[:10]
            )
            if any(value.run_id != run_id for value in projected_artifacts):
                raise ValueError("artifact summary no longer binds the Run")
            for action in actions:
                envelope = action.envelope
                matches = (
                    ()
                    if plan_item is None
                    else tuple(value for value in plan_item.steps if value.id == envelope.step_id)
                )
                if len(matches) != 1:
                    raise ValueError("external action no longer binds the Run plan")
                step = matches[0]
                assert plan_item is not None
                _validate_action_step_binding(
                    action,
                    step,
                    run_id=run_id,
                    plan_hash=plan_item.plan.plan_hash,
                )
                _validate_action_result_receipt(
                    action,
                    action_receipts.get(action.id),
                    step,
                )
            projected_actions = tuple(_project_action(value) for value in actions)
            return _project_run(
                stored,
                plan=plan,
                execution_control=projected_control,
                pending_approvals=projected_approvals,
                artifact_summaries=projected_artifacts,
                artifacts_truncated=len(artifact_items) > 10,
                actions=projected_actions,
            )
        except (TypeError, ValueError):
            raise self._corrupt() from None

    async def read_timeline(
        self,
        run_id: str,
        query: RunTimelineQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunTimelinePage:
        self._authorize(principal)
        self._validate_id(run_id, "run")
        if type(query) is not RunTimelineQuery:
            raise RunResourceServiceError(
                "run_timeline_query_invalid",
                "run timeline query is invalid",
            )
        after_sequence = _decode_timeline_cursor(run_id, query)
        try:
            async with self._unit_of_work() as unit_of_work:
                run = await unit_of_work.runs.get_inspectable(run_id)
                events = (
                    ()
                    if run is None
                    else await unit_of_work.audits.list_run(
                        run_id,
                        after_sequence=after_sequence,
                        limit=query.limit + 1,
                    )
                )
        except (TypeError, ValueError, RuntimeError):
            raise self._corrupt() from None
        if run is None:
            raise RunResourceServiceError("run_not_found", "run was not found")
        try:
            now = self._utc_now()
            require_utc(now, "run timeline projection time")
            _validate_inspectable_run(run)
            page_events = events[: query.limit]
            items = tuple(
                _project_timeline_event(value, run_id=run_id, now=now) for value in page_events
            )
            sequences = tuple(value.sequence for value in items)
            if sequences != tuple(sorted(sequences)) or len(sequences) != len(set(sequences)):
                raise ValueError("run timeline order is invalid")
            if any(value <= after_sequence for value in sequences):
                raise ValueError("run timeline crossed its cursor boundary")
        except (TypeError, ValueError):
            raise self._corrupt() from None
        next_cursor = (
            _encode_timeline_cursor(run_id, items[-1].sequence)
            if len(events) > query.limit and items
            else None
        )
        return RunTimelinePage(run_id=run_id, items=items, next_cursor=next_cursor)

    async def read_step(
        self,
        run_id: str,
        step_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> RunStepResource:
        self._authorize(principal)
        self._validate_id(run_id, "run")
        self._validate_id(step_id, "run step")
        try:
            async with self._unit_of_work() as unit_of_work:
                run = await unit_of_work.runs.get_inspectable(run_id)
                plan_item = await unit_of_work.run_steps.get_inspectable_plan(run_id)
                transitions: tuple[StepStateTransition, ...] = ()
                if plan_item is not None and any(value.id == step_id for value in plan_item.steps):
                    transitions = await unit_of_work.run_steps.list_transitions(step_id)
        except (TypeError, ValueError, RuntimeError):
            raise self._corrupt() from None
        if run is None:
            raise RunResourceServiceError("run_not_found", "run was not found")
        matches = (
            ()
            if plan_item is None
            else tuple(value for value in plan_item.steps if value.id == step_id)
        )
        if len(matches) != 1:
            raise RunResourceServiceError("run_step_not_found", "run step was not found")
        if not transitions:
            raise self._corrupt()
        try:
            _validate_inspectable_run(run)
            assert plan_item is not None
            _project_plan(run_id, plan_item)
            if not any(
                transition.command is RunLifecycleCommand.RECORD_PLAN
                for transition in run.transitions
            ):
                raise ValueError("run step exists without a recorded plan")
            return _project_step(matches[0], transitions=transitions)
        except (TypeError, ValueError):
            raise self._corrupt() from None

    async def read_external_action(
        self,
        action_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ExternalActionResource:
        self._authorize(principal)
        self._validate_id(action_id, "external action")
        try:
            async with self._unit_of_work() as unit_of_work:
                action = await unit_of_work.external_actions.get(action_id)
                run = None
                plan_item = None
                step_transitions: tuple[StepStateTransition, ...] = ()
                receipt: ConnectorActionReceipt | None = None
                if action is not None:
                    envelope = action.proposal.envelope
                    if action.result is not None:
                        receipt = await unit_of_work.connector_receipts.get(
                            action.connector_binding_id,
                            action.idempotency_key,
                        )
                    run = await unit_of_work.runs.get_inspectable(envelope.run_id)
                    plan_item = await unit_of_work.run_steps.get_inspectable_plan(envelope.run_id)
                    if plan_item is not None and any(
                        value.id == envelope.step_id for value in plan_item.steps
                    ):
                        step_transitions = await unit_of_work.run_steps.list_transitions(
                            envelope.step_id
                        )
        except (
            AttributeError,
            ExternalActionRepositoryConflict,
            TypeError,
            ValueError,
            RuntimeError,
        ):
            raise self._corrupt() from None
        if action is None:
            raise RunResourceServiceError(
                "external_action_not_found",
                "external action was not found",
            )
        envelope = action.proposal.envelope
        if run is None or plan_item is None:
            raise self._corrupt()
        matches = tuple(value for value in plan_item.steps if value.id == envelope.step_id)
        if len(matches) != 1 or not step_transitions:
            raise self._corrupt()
        step = matches[0]
        if envelope.action_id != action_id:
            raise self._corrupt()
        try:
            _validate_inspectable_run(run)
            _project_plan(envelope.run_id, plan_item)
            _project_step(step, transitions=step_transitions)
            _validate_action_step_binding(
                action,
                step,
                run_id=envelope.run_id,
                plan_hash=plan_item.plan.plan_hash,
            )
            _validate_action_result_receipt(action, receipt, step)
            if (
                envelope.plan_hash != plan_item.plan.plan_hash
                or plan_item.plan.workflow_id != run.work_item.workflow_id
                or plan_item.plan.catalog_content_hash != run.run.catalog_hash
            ):
                raise ValueError("external action plan no longer binds its Run")
            return _project_action(action)
        except (TypeError, ValueError):
            raise self._corrupt() from None

    async def read_instance_status_summary(
        self,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceStatusSummary:
        return await self.read_instance_statuses(
            self._catalog_instance_ids,
            principal=principal,
        )

    async def read_instance_statuses(
        self,
        instance_ids: tuple[str, ...],
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceStatusSummary:
        self._authorize(principal)
        self._validate_instance_ids(instance_ids)
        if any(value not in self._catalog_instance_ids for value in instance_ids):
            raise RunResourceServiceError(
                "agent_instance_not_found",
                "agent instance was not found",
            )
        try:
            async with self._unit_of_work() as unit_of_work:
                latest_by_instance = []
                for instance_id in instance_ids:
                    latest = await unit_of_work.runs.list_inspectable(
                        state=None,
                        instance_id=instance_id,
                        workflow_id=None,
                        created_at_from=None,
                        created_at_to=None,
                        before_created_at=None,
                        before_run_id=None,
                        limit=1,
                    )
                    latest_by_instance.append((instance_id, latest))
        except (TypeError, ValueError, RuntimeError):
            raise self._corrupt() from None
        try:
            items = tuple(
                self._project_instance_status(instance_id, latest)
                for instance_id, latest in latest_by_instance
            )
        except (TypeError, ValueError):
            raise self._corrupt() from None
        digest = hashlib.sha256(
            _STATUS_ETAG_DOMAIN
            + canonical_json_bytes(
                [
                    {
                        "instance_id": item.instance_id,
                        "latest_run_id": item.latest_run_id,
                        "latest_run_state": item.latest_run_state,
                        "latest_run_created_at": _iso(item.latest_run_created_at),
                        "latest_run_updated_at": _iso(item.latest_run_updated_at),
                        "status": item.status,
                    }
                    for item in items
                ]
            )
        ).hexdigest()
        return InstanceStatusSummary(
            scope=RUNTIME_RESOURCE_INSTALLATION_SCOPE,
            items=items,
            etag=f'"instance-status-sha256-v1:{digest}"',
        )

    async def list_recent_instance_runs(
        self,
        instance_id: str,
        *,
        limit: int = 5,
        principal: AuthenticatedPrincipal,
    ) -> tuple[RunResource, ...]:
        self._authorize(principal)
        self._validate_id(instance_id, "agent instance")
        if instance_id not in self._catalog_instance_ids:
            raise RunResourceServiceError(
                "agent_instance_not_found",
                "agent instance was not found",
            )
        if type(limit) is not int or not 1 <= limit <= MAX_RECENT_INSTANCE_RUNS:
            raise RunResourceServiceError(
                "recent_run_limit_invalid",
                "recent Run limit is outside the supported range",
            )
        try:
            async with self._unit_of_work() as unit_of_work:
                stored = await unit_of_work.runs.list_inspectable(
                    state=None,
                    instance_id=instance_id,
                    workflow_id=None,
                    created_at_from=None,
                    created_at_to=None,
                    before_created_at=None,
                    before_run_id=None,
                    limit=limit,
                )
        except (TypeError, ValueError, RuntimeError):
            raise self._corrupt() from None
        try:
            projected = tuple(_project_run(value) for value in stored)
        except (TypeError, ValueError):
            raise self._corrupt() from None
        boundaries = tuple((value.created_at, value.run_id) for value in projected)
        if boundaries != tuple(sorted(boundaries, reverse=True)):
            raise self._corrupt()
        return projected

    @staticmethod
    def _project_instance_status(
        instance_id: str,
        latest: tuple[InspectableRun, ...],
    ) -> InstanceRuntimeStatus:
        if len(latest) > 1:
            raise ValueError("latest Run query returned an invalid cardinality")
        if not latest:
            return InstanceRuntimeStatus(
                instance_id=instance_id,
                status="never_run",
                latest_run_id=None,
                latest_run_state=None,
                latest_run_created_at=None,
                latest_run_updated_at=None,
                instance_url=f"/api/v1/agent-instances/{instance_id}",
                latest_run_url=None,
            )
        item = latest[0]
        _validate_inspectable_run(item)
        if item.work_item.instance_id != instance_id:
            raise ValueError("latest Run does not bind the requested instance")
        return InstanceRuntimeStatus(
            instance_id=instance_id,
            status=item.run.state.value,
            latest_run_id=item.run.id,
            latest_run_state=item.run.state.value,
            latest_run_created_at=item.run.created_at,
            latest_run_updated_at=item.run.updated_at,
            instance_url=f"/api/v1/agent-instances/{instance_id}",
            latest_run_url=f"/api/v1/runs/{item.run.id}",
        )

    @staticmethod
    def _validate_instance_ids(instance_ids: tuple[str, ...]) -> None:
        if (
            type(instance_ids) is not tuple
            or not instance_ids
            or len(instance_ids) > 100
            or len(instance_ids) != len(set(instance_ids))
        ):
            raise ValueError("catalog instance IDs must be a bounded unique tuple")
        for value in instance_ids:
            require_id(value, "catalog instance ID")

    @staticmethod
    def _validate_id(value: str, resource: str) -> None:
        try:
            require_id(value, f"{resource} ID")
        except (TypeError, ValueError):
            raise RunResourceServiceError(
                f"{resource.replace(' ', '_')}_id_invalid",
                f"{resource} ID is invalid",
            ) from None

    @staticmethod
    def _authorize(principal: AuthenticatedPrincipal) -> None:
        try:
            authorize_runtime_resource_reader(principal)
        except RuntimeResourceAuthorizationError as exc:
            raise RunResourceServiceError(exc.code, str(exc)) from None

    @staticmethod
    def _corrupt() -> RunResourceServiceError:
        return RunResourceServiceError(
            "runtime_record_corrupt",
            "runtime resources could not be validated",
        )


__all__ = [
    "DEFAULT_RUN_PAGE_SIZE",
    "DEFAULT_TIMELINE_PAGE_SIZE",
    "MAX_RECENT_INSTANCE_RUNS",
    "MAX_RUN_PAGE_SIZE",
    "MAX_TIMELINE_PAGE_SIZE",
    "ExternalActionResource",
    "InstanceRuntimeStatus",
    "InstanceStatusSummary",
    "PendingApprovalSummary",
    "RunExecutionControlResource",
    "RunListQuery",
    "RunPage",
    "RunPlanResource",
    "RunPlanSelectedInstanceResource",
    "RunResource",
    "RunResourceService",
    "RunResourceServiceError",
    "RunRoutingAssignmentResource",
    "RunStepResource",
    "RunTimelineEvent",
    "RunTimelinePage",
    "RunTimelineQuery",
    "RunTransitionResource",
]
