"""Bounded, restartable advancement of the implemented local mock workflows."""

from __future__ import annotations

import asyncio
import hmac

from marketing_agents.application.services.approval_boundaries import ApprovalBoundaryService
from marketing_agents.application.services.run_lifecycle import (
    RunAdvanceDisposition,
    RunLifecycleService,
)
from marketing_agents.application.services.terminal_execution_cleanup import (
    TerminalExecutionCleanupService,
)
from marketing_agents.demos import DEMO_SCENARIOS
from marketing_agents.demos.email_signup_onboarding import EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.run_lifecycle import (
    FailureContext,
    RunFailurePhase,
    RunLifecycleCommand,
)
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import require_id
from marketing_agents.infrastructure.runtime.run_claims import ACTIVE_STATES, RunClaim, RunClaims
from marketing_agents.security.admission_digest import derive_admission_digests

from .composition import LocalRuntime


class RunWorker:
    def __init__(
        self, runtime: LocalRuntime, worker_id: str, *, timeout_seconds: float = 45
    ) -> None:
        require_id(worker_id, "run worker ID")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("worker advancement timeout must be bounded")
        self.runtime = runtime
        self.worker_id = worker_id
        self.timeout_seconds = timeout_seconds
        self.claims = RunClaims(runtime)
        self.stopping = False

    def stop_claiming(self) -> None:
        self.stopping = True

    async def drain_once(self) -> bool:
        if self.stopping:
            return False
        claim = await self.claims.claim_once(self.worker_id)
        if claim is None:
            return False
        task = asyncio.create_task(self._advance(claim.run_id))
        lease_lost = asyncio.Event()
        renewer = asyncio.create_task(self._renew(claim, task, lease_lost))
        try:
            await asyncio.wait_for(task, timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            # A stopped process leaves durable in-flight attempts/actions for the
            # existing executor recovery paths. It never invents a call outcome.
            if not lease_lost.is_set():
                raise
        except Exception:
            await self._fail(claim)
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self.claims.release(claim)
        return True

    async def _renew(
        self, claim: RunClaim, task: asyncio.Task[None], lease_lost: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                owned = await self.claims.renew(claim)
            except Exception:
                owned = False
            if not owned:
                lease_lost.set()
                task.cancel()
                return

    async def _advance(self, run_id: str) -> None:
        dependencies = self.runtime.dependencies
        async with dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run_id)
            work = None if run is None else await unit_of_work.works.get(run.work_item_id)
        if run is None or work is None or run.state.value not in ACTIVE_STATES:
            return
        # A signed admitted envelope, frozen schema, and exact registry contract
        # are rechecked before received -> validated and before any resumed call.
        scenario = DEMO_SCENARIOS.get(work.workflow_id)
        schema = scenario.input_schema
        expected = derive_admission_digests(
            AdmissionEnvelope(
                source=work.source,
                event_id=work.event_id,
                instance_id=work.instance_id,
                trigger_id=work.trigger_id,
                workflow_id=work.workflow_id,
                mode=work.mode,
                brief_id=work.brief_id,
                brief_revision=work.brief_revision,
                configuration_revision=work.configuration_revision,
                admitted_payload=work.admitted_payload,
            ),
            self.runtime.digest_key,
        )
        if (
            not hmac.compare_digest(expected.input_digest, work.input_digest)
            or not hmac.compare_digest(expected.admission_digest, work.admission_digest)
            or expected.digest_key_version != work.digest_key_version
            or work.instance_id != scenario.instance_id
            or work.input_schema_id != scenario.input_schema_id
            or work.input_schema_hash != canonical_schema_hash(schema)
            or run.catalog_hash != self.runtime.catalog.content_hash
        ):
            raise ValueError("runtime_admission_integrity_invalid")
        DEMO_SCENARIOS.resolve_input(scenario.id, work.admitted_payload)
        correlation = dependencies.new_id("worker-advance")
        if work.workflow_id == EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID:
            if run.state is RunState.AWAITING_APPROVAL:
                await ApprovalBoundaryService(dependencies).evaluate(
                    run.id,
                    audit_context=AuditContext.worker(self.worker_id, correlation_id=correlation),
                )
            await self.runtime.email.resume(
                run.id, correlation_id=correlation, worker_id=self.worker_id
            )
        else:
            await self.runtime.demos.resume_persisted(
                run.id, correlation_id=correlation, worker_id=self.worker_id
            )

    async def _fail(self, claim: RunClaim) -> None:
        run_id = claim.run_id
        dependencies = self.runtime.dependencies
        context = AuditContext.worker(self.worker_id, correlation_id=dependencies.new_id("failure"))
        async with self.claims.failure_unit_of_work(claim) as unit_of_work:
            if unit_of_work is None:
                return
            run = await unit_of_work.runs.get(run_id)
            if run is None or run.state.value not in ACTIVE_STATES:
                return
            steps = await unit_of_work.run_steps.list_for_run(run_id)
            if run.state is RunState.EXECUTING:
                failed_step = next(
                    (
                        step
                        for step in steps
                        if step.state
                        in {
                            StepState.READY,
                            StepState.EXECUTING,
                        }
                    ),
                    None,
                )
                if failed_step is not None:
                    await TerminalExecutionCleanupService().fail_execution_in_uow(
                        unit_of_work,
                        run_id=run_id,
                        failed_step_id=failed_step.id,
                        plan_hash=failed_step.plan_hash,
                        failure_code="unclassified_failure",
                        occurred_at=dependencies.utc_now(),
                        audit_context=context,
                    )
                    await unit_of_work.commit()
                    return
            phase = {
                RunState.RECEIVED: RunFailurePhase.VALIDATION,
                RunState.VALIDATED: RunFailurePhase.PLANNING,
                RunState.PLANNED: RunFailurePhase.PLANNING,
                RunState.AWAITING_APPROVAL: RunFailurePhase.APPROVAL_PROCESSING,
                RunState.EXECUTING: RunFailurePhase.EXECUTION,
            }[run.state]
            attempt = await RunLifecycleService(dependencies).attempt_advance_in_uow(
                unit_of_work,
                run_id,
                run.version,
                RunLifecycleCommand.FAIL,
                FailureContext(phase, "unclassified_failure"),
                audit_context=context,
            )
            if attempt.disposition is RunAdvanceDisposition.CAS_LOST:
                return
            await unit_of_work.commit()
            if attempt.error is not None:
                raise attempt.error
