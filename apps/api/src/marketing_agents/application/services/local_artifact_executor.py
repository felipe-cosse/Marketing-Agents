"""Atomic schema-bound NO_CALL transforms; never reserve a provider attempt."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.orchestration.executable_workflows import (
    CatalogRoleExecutionKind,
    ExecutableWorkflowRegistry,
)
from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.provenance import ArtifactEnvelope, ProvenanceSource, ProviderVersion
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    canonical_payload_size_bytes,
    payload_fields_within_byte_limit,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    transition_step,
)
from marketing_agents.domain.validation import require_id

from .audit_events import AuditEventFactory


class LocalArtifactRenderer(Protocol):
    def render_local(
        self, template_id: str, admitted_input: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class LocalArtifactExecutionError(RuntimeError):
    """Payload-safe failure for a local transform's immutable execution fences."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LocalArtifactExecutor:
    """Execute the single local role step, output and audit in one transaction.

    Local rendering is synchronous and bounded by the admitted schema/byte
    limits. No intermediate EXECUTING state escapes a failed transaction, so a
    crash cannot strand an invented open call or duplicate a committed output.
    """

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        workflows: ExecutableWorkflowRegistry,
        renderer: LocalArtifactRenderer,
    ) -> None:
        self._dependencies = dependencies
        self._workflows = workflows
        self._renderer = renderer

    async def execute(self, step_id: str, *, audit_context: AuditContext) -> ArtifactEnvelope:
        require_id(step_id, "local artifact step ID")
        audit_context.verify_integrity()
        async with self._dependencies.unit_of_work() as uow:
            step = await uow.run_steps.get(step_id)
            if step is None:
                raise LocalArtifactExecutionError("local_step_missing")
            run = await uow.runs.get(step.run_id)
            plan = await uow.run_steps.get_plan(step.run_id)
            work = None if run is None else await uow.works.get(run.work_item_id)
            control = await uow.execution_control.get(step.run_id)
            if run is None or plan is None or work is None or control is None:
                raise LocalArtifactExecutionError("local_execution_snapshot_missing")
            definition = self._workflows.get(work.workflow_id)
            self._workflows.require_match(
                work.workflow_id,
                instance_id=work.instance_id,
                template_id=step.template_id,
                trigger_kind=definition.eligible_trigger_kinds[0],
                mode=work.mode,
                catalog_content_hash=plan.catalog_content_hash,
            )
            steps = await uow.run_steps.validate_plan_for_execution(run.id)
            if (
                definition.execution_kind is not CatalogRoleExecutionKind.LOCAL_TRANSFORM
                or definition.capability_id != "cap.artifact.transform-deterministic"
                or step.capability_id != definition.capability_id
                or step.connector_family != "artifact"
                or step.effect is not Effect.READ
                or step.runtime_policy.attempt_kind is not AttemptKind.NO_CALL
                or steps != (step,)
                or step.dependency_keys
                or step.selected_instance_id != work.instance_id
                or step.configuration_revision != work.configuration_revision
                or step.plan_hash != plan.plan_hash
                or control.policy_hash != plan.plan_hash
                or plan.workflow_definition_hash != definition.definition_hash
                or plan.workflow_id != definition.id
                or plan.workflow_version != definition.version
                or run.catalog_hash != definition.catalog_content_hash
                or step.request_schema_id != definition.input_schema_id
                or step.result_schema_id != definition.output_schema_id
                or step.result_schema_hash != definition.output_schema_hash
                or work.input_schema_id != definition.input_schema_id
                or work.input_schema_hash != definition.input_schema_hash
                or plan.approval_required
                or control.model_calls != 0
                or control.tool_calls != 0
            ):
                raise LocalArtifactExecutionError("local_execution_contract_invalid")
            artifact_id = "artifact.local." + hashlib.sha256(step.id.encode("utf-8")).hexdigest()
            existing = await uow.artifacts.get(artifact_id)
            if step.state is StepState.SUCCEEDED:
                if existing is None or existing.provenance.step_id != step.id:
                    raise LocalArtifactExecutionError("local_output_replay_invalid")
                return existing
            if existing is not None or step.state is not StepState.READY:
                raise LocalArtifactExecutionError("local_step_not_ready")
            now = self._dependencies.utc_now()
            if (
                run.state is not RunState.EXECUTING
                or not await uow.execution_control.fence_active(
                    run_id=run.id, expected_control_version=control.version, occurred_at=now
                )
                or not await uow.runs.fence(
                    run_id=run.id, expected_version=run.version, expected_state=RunState.EXECUTING
                )
            ):
                raise LocalArtifactExecutionError("local_execution_fence_lost")
            budget = step.runtime_policy.budget
            if canonical_payload_size_bytes(
                work.admitted_payload
            ) > budget.max_input_bytes or not payload_fields_within_byte_limit(
                work.admitted_payload, budget.max_input_field_bytes
            ):
                raise LocalArtifactExecutionError("local_input_budget_exceeded")
            compile_json_schema(
                definition.input_schema, expected_schema_id=definition.input_schema_id
            ).validate(work.admitted_payload, pointer_root="/input", max_depth=16)
            began = time.monotonic()
            payload = json.loads(
                canonical_json_bytes(
                    self._renderer.render_local(step.template_id, work.admitted_payload)
                )
            )
            completed_at = self._dependencies.utc_now()
            if (
                control.deadline_at is None
                or completed_at >= control.deadline_at
                or time.monotonic() - began >= step.runtime_policy.timeout.step_seconds
            ):
                raise LocalArtifactExecutionError("local_execution_deadline_exceeded")
            if canonical_payload_size_bytes(payload) > budget.max_output_bytes:
                raise LocalArtifactExecutionError("local_output_budget_exceeded")
            compile_json_schema(
                definition.output_schema, expected_schema_id=definition.output_schema_id
            ).validate(payload, pointer_root="/output", max_depth=16)
            artifact = ArtifactEnvelope.create(
                payload=payload,
                artifact_id=artifact_id,
                work_item_id=work.id,
                run_id=run.id,
                step_id=step.id,
                workflow_id=definition.id,
                workflow_version=str(definition.version),
                template_id=step.template_id,
                instance_id=step.selected_instance_id,
                admitted_input_digest=work.input_digest,
                catalog_hash=plan.catalog_content_hash,
                instance_config_revision=step.configuration_revision,
                sources=(
                    ProvenanceSource(
                        kind="work_input",
                        source_id=work.id,
                        integrity_digest=work.input_digest,
                        classification=work.input_classification,
                    ),
                ),
                parent_artifact_ids=(),
                providers=(
                    ProviderVersion(
                        provider_kind="planner",
                        mode="local",
                        name="catalog-role-transform",
                        version="v1",
                    ),
                ),
                output_schema_id=definition.output_schema_id,
                output_schema_version="v1",
                output_schema_hash=definition.output_schema_hash,
                created_at=completed_at,
                classification=work.input_classification,
            )
            started = transition_step(
                step, StepLifecycleCommand.START, NoStepTransitionContext(), now
            )
            finished = transition_step(
                started.step, StepLifecycleCommand.SUCCEED, NoStepTransitionContext(), completed_at
            )
            for result in (started, finished):
                if not await uow.run_steps.apply_transition(
                    expected_run_version=run.version,
                    expected_run_state=RunState.EXECUTING,
                    expected_version=result.transition.expected_version,
                    expected_state=result.transition.previous_state or StepState.READY,
                    result=result,
                ):
                    raise LocalArtifactExecutionError("local_step_fence_lost")
            await uow.artifacts.add_or_get(artifact)
            factory = AuditEventFactory(audit_context)
            await uow.audits.append_many(
                (
                    factory.step_transition(started.step, started.transition),
                    factory.step_transition(finished.step, finished.transition),
                    factory.artifact_transformed(artifact, finished.step),
                )
            )
            await uow.commit()
            return artifact
