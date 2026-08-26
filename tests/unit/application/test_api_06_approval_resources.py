"""API-06: authenticated, immutable approval resource application behavior."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.approval_authorization import (
    ApprovalAuthorizationError,
    authorize_approval_decision,
)
from marketing_agents.application.ports.repositories import ApprovalRepositoryConflict
from marketing_agents.application.services.approval_decisions import ApprovalDecisionCommand
from marketing_agents.application.services.approval_records import RenewedApprovalRequest
from marketing_agents.application.services.approval_resources import (
    MAX_APPROVAL_CURSOR_LENGTH,
    ApprovalListQuery,
    ApprovalRequestCommand,
    ApprovalRequestDisposition,
    ApprovalResourceService,
    ApprovalResourceServiceError,
)
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalDecision,
    ApprovalPolicySnapshot,
    ProposedExternalAction,
    StoredActionApprovalRequest,
    approval_redaction_schema,
    request_approval,
    safe_approval_destination,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import ApprovalDecisionKind, ApprovalStatus
from marketing_agents.domain.identity import AuthenticatedPrincipal

from tests.support.identity import human_principal


def _approval_request() -> ActionApprovalRequest:
    semantic = SemanticExternalAction(
        template_id="template.email",
        instance_id="instance.email.1",
        action_type="email.send",
        capability_id="cap.email.send",
        connector_family="email",
        binding_id="binding.email.primary",
        destination="recipient-sha256-v1:" + "a" * 64,
        payload_schema_id="schema.email.send.v1",
        minimized_payload={
            "recipient": "person@example.invalid",
            "subject": "Hello",
        },
    )
    action = CanonicalExternalAction(
        action_id="action.1",
        authorization_set_id="authorization-set.1",
        run_id="run.1",
        plan_hash="b" * 64,
        proposal_revision=1,
        step_id="step.send",
        step_key="send",
        template_id=semantic.template_id,
        instance_id=semantic.instance_id,
        action_type=semantic.action_type,
        capability_id=semantic.capability_id,
        connector_family=semantic.connector_family,
        binding_id=semantic.binding_id,
        destination=semantic.destination,
        payload_schema_id=semantic.payload_schema_id,
        minimized_payload=semantic.minimized_payload,
        semantic_action_hash=semantic_action_hash(semantic),
    )
    proposal = ProposedExternalAction.create(
        action,
        redacted_destination=safe_approval_destination(action.binding_id),
        payload_schema=approval_redaction_schema(("/recipient",)),
    )
    return request_approval(
        request_id="approval-request.1",
        proposed_action=proposal,
        policy=ApprovalPolicySnapshot(
            policy_id="policy.external-write",
            required_roles=frozenset({"role.approver"}),
            required_scopes=frozenset({"scope.external-write"}),
            expires_after_seconds=900,
            allow_self_approval=False,
        ),
        requested_by="principal.requester",
        requested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _reader() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.api-06.viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset({"approvals:read"}),
    )


def _requester() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.api-06.operator",
        roles=frozenset({"operator"}),
        scopes=frozenset({"approvals:read", "approvals:request"}),
    )


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.value


class _ExplodingClock:
    def now(self) -> datetime:
        raise AssertionError("clock must not be consulted")


class _Ids:
    def new(self, namespace: str) -> str:
        raise AssertionError(f"ID generator must not be consulted: {namespace}")


@dataclass(frozen=True, slots=True)
class _Action:
    action_hash: str


class _ApprovalRepository:
    def __init__(
        self,
        *,
        listed: tuple[StoredActionApprovalRequest, ...] = (),
        chain: tuple[StoredActionApprovalRequest, ...] = (),
        by_id: StoredActionApprovalRequest | None = None,
    ) -> None:
        self.listed = listed
        self.chain = chain
        self.by_id = by_id
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[str] = []
        self.inspectable_get_calls: list[str] = []
        self.chain_calls: list[str] = []

    async def list_requests(
        self,
        *,
        status: ApprovalStatus | None,
        run_id: str | None,
        action_id: str | None,
        before_requested_at: datetime | None,
        before_request_id: str | None,
        limit: int,
    ) -> tuple[StoredActionApprovalRequest, ...]:
        self.list_calls.append(
            {
                "status": status,
                "run_id": run_id,
                "action_id": action_id,
                "before_requested_at": before_requested_at,
                "before_request_id": before_request_id,
                "limit": limit,
            }
        )
        return self.listed

    async def get(self, request_id: str) -> StoredActionApprovalRequest | None:
        self.get_calls.append(request_id)
        return self.by_id

    async def get_inspectable(
        self,
        request_id: str,
    ) -> StoredActionApprovalRequest | None:
        self.inspectable_get_calls.append(request_id)
        return self.by_id

    async def list_for_action(
        self,
        action_id: str,
    ) -> tuple[StoredActionApprovalRequest, ...]:
        self.chain_calls.append(action_id)
        return self.chain


class _ActionRepository:
    def __init__(self, action: _Action | None) -> None:
        self.action = action
        self.get_calls: list[str] = []

    async def get(self, action_id: str) -> _Action | None:
        self.get_calls.append(action_id)
        return self.action


class _UnitOfWork:
    def __init__(
        self,
        approvals: _ApprovalRepository,
        actions: _ActionRepository,
    ) -> None:
        self.approvals = approvals
        self.external_actions = actions
        self.entries = 0

    async def __aenter__(self) -> _UnitOfWork:
        self.entries += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        raise AssertionError("approval resource queries must not commit")


class _UnitOfWorkFactory:
    def __init__(self, unit_of_work: _UnitOfWork) -> None:
        self.unit_of_work = unit_of_work
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        return self.unit_of_work


class _ExplodingUnitOfWorkFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        raise AssertionError("authorization must fail before repository lookup")


class _ApprovalRecords:
    def __init__(self, renewal: RenewedApprovalRequest) -> None:
        self.renewal = renewal
        self.calls: list[dict[str, object]] = []

    async def renew_expired(
        self,
        *,
        request_id: str,
        expected_version: int,
        expected_action_hash: str,
        audit_context: AuditContext,
        requested_by: str | None = None,
    ) -> RenewedApprovalRequest:
        self.calls.append(
            {
                "request_id": request_id,
                "expected_version": expected_version,
                "expected_action_hash": expected_action_hash,
                "audit_context": audit_context,
                "requested_by": requested_by,
            }
        )
        return self.renewal


class _ExplodingApprovalRecords:
    async def renew_expired(self, **_: object) -> RenewedApprovalRequest:
        raise AssertionError("approval renewal must not be attempted")


class _ConflictingApprovalRecords:
    def __init__(
        self,
        approvals: _ApprovalRepository,
        winner_chain: tuple[StoredActionApprovalRequest, ...],
    ) -> None:
        self.approvals = approvals
        self.winner_chain = winner_chain
        self.calls = 0

    async def renew_expired(self, **_: object) -> RenewedApprovalRequest:
        self.calls += 1
        self.approvals.chain = self.winner_chain
        raise ApprovalRepositoryConflict(
            "approval_renewal_conflict",
            "a concurrent request won renewal",
        )


def _service(
    clock: _Clock | _ExplodingClock,
    units: _UnitOfWorkFactory | _ExplodingUnitOfWorkFactory,
) -> ApprovalResourceService:
    dependencies = OrchestrationDependencies(
        clock,
        _Ids(),
        units,  # type: ignore[arg-type]
    )
    return ApprovalResourceService(dependencies)


@pytest.mark.asyncio
async def test_api_06_read_authorization_happens_before_lookup_or_clock() -> None:
    units = _ExplodingUnitOfWorkFactory()
    service = _service(_ExplodingClock(), units)

    with pytest.raises(ApprovalResourceServiceError) as captured:
        await service.read(
            "approval-request.1",
            principal=human_principal(
                roles=frozenset({"viewer"}),
                scopes=frozenset(),
            ),
        )

    assert captured.value.code == "approval_read_scope_missing"
    assert units.calls == 0


@pytest.mark.asyncio
async def test_api_06_read_uses_historical_inspection_not_mutation_lookup() -> None:
    request = _approval_request()
    stored = StoredActionApprovalRequest.created(request)
    approvals = _ApprovalRepository(by_id=stored)
    units = _UnitOfWorkFactory(_UnitOfWork(approvals, _ActionRepository(None)))
    service = _service(_Clock(request.requested_at), units)

    resource = await service.read(request.id, principal=_reader())

    assert resource.approval_id == request.id
    assert approvals.inspectable_get_calls == [request.id]
    assert approvals.get_calls == []


@pytest.mark.asyncio
async def test_api_06_request_authorization_happens_before_action_lookup() -> None:
    request = _approval_request()
    units = _ExplodingUnitOfWorkFactory()
    service = _service(_ExplodingClock(), units)
    command = ApprovalRequestCommand(
        action_id=request.action_id,
        expected_generation=0,
        expected_action_hash=request.action_hash,
        correlation_id="correlation.api-06.denied",
    )

    with pytest.raises(ApprovalResourceServiceError) as captured:
        await service.request(
            command,
            principal=human_principal(
                roles=frozenset({"operator"}),
                scopes=frozenset({"approvals:read"}),
            ),
        )

    assert captured.value.code == "approval_request_scope_missing"
    assert units.calls == 0


@pytest.mark.asyncio
async def test_api_06_safe_projection_and_filter_bound_cursor_validation() -> None:
    request = _approval_request()
    older_at = request.requested_at - timedelta(minutes=1)
    older_request = replace(
        request,
        id="approval-request.older",
        requested_at=older_at,
        expires_at=older_at + timedelta(seconds=request.policy.expires_after_seconds),
    )
    listed = (
        StoredActionApprovalRequest.created(request),
        StoredActionApprovalRequest.created(older_request),
    )
    approvals = _ApprovalRepository(listed=listed)
    units = _UnitOfWorkFactory(_UnitOfWork(approvals, _ActionRepository(None)))
    service = _service(_Clock(request.requested_at + timedelta(minutes=1)), units)

    query = ApprovalListQuery(limit=1)
    page = await service.list(query, principal=_reader())

    assert len(page.items) == 1
    resource = page.items[0]
    assert resource.approval_id == request.id
    assert resource.destination_summary == request.redacted_destination
    assert resource.redacted_payload == {
        "recipient": "[REDACTED]",
        "subject": "Hello",
    }
    assert resource.payload_hash == request.action_hash
    assert "person@example.invalid" not in repr(resource)
    assert page.next_cursor is not None
    assert len(page.next_cursor) <= MAX_APPROVAL_CURSOR_LENGTH
    assert approvals.list_calls[0]["limit"] == 2

    with pytest.raises(ApprovalResourceServiceError) as captured:
        await service.list(
            ApprovalListQuery(
                status=ApprovalStatus.PENDING,
                cursor=page.next_cursor,
                limit=1,
            ),
            principal=_reader(),
        )

    assert captured.value.code == "approval_cursor_invalid"
    assert units.calls == 1


@pytest.mark.asyncio
async def test_api_06_generation_zero_reuses_exact_current_request_without_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _approval_request()
    current = StoredActionApprovalRequest.created(request)
    approvals = _ApprovalRepository(chain=(current,))
    units = _UnitOfWorkFactory(
        _UnitOfWork(approvals, _ActionRepository(_Action(request.action_hash)))
    )
    service = _service(_Clock(request.requested_at + timedelta(minutes=1)), units)
    monkeypatch.setattr(service, "_records", _ExplodingApprovalRecords())

    result = await service.request(
        ApprovalRequestCommand(
            action_id=request.action_id,
            expected_generation=0,
            expected_action_hash=request.action_hash,
            correlation_id="correlation.api-06.existing",
        ),
        principal=_requester(),
    )

    assert result.disposition is ApprovalRequestDisposition.EXISTING
    assert result.approval.approval_id == request.id
    assert result.approval.generation == 1
    assert result.approval.is_actionable is True
    assert approvals.chain_calls == [request.action_id]


@pytest.mark.asyncio
async def test_api_06_expired_request_renews_once_and_exact_replay_returns_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _approval_request()
    expired = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.EXPIRED,
        version=2,
        updated_at=request.expires_at,
        expired_at=request.expires_at,
    )
    renewed_at = request.expires_at + timedelta(seconds=1)
    replacement_request = replace(
        request,
        id="approval-request.2",
        generation=2,
        requested_by="principal.api-06.operator",
        requested_at=renewed_at,
        expires_at=renewed_at + timedelta(seconds=request.policy.expires_after_seconds),
    )
    replacement = StoredActionApprovalRequest.created(replacement_request)
    linked_expired = replace(
        expired,
        version=3,
        updated_at=renewed_at,
        replacement_request_id=replacement_request.id,
        renewed_at=renewed_at,
    )
    renewal = RenewedApprovalRequest(expired=linked_expired, replacement=replacement)
    records = _ApprovalRecords(renewal)
    approvals = _ApprovalRepository(chain=(expired,))
    units = _UnitOfWorkFactory(
        _UnitOfWork(approvals, _ActionRepository(_Action(request.action_hash)))
    )
    service = _service(_Clock(renewed_at), units)
    monkeypatch.setattr(service, "_records", records)
    command = ApprovalRequestCommand(
        action_id=request.action_id,
        expected_generation=1,
        expected_action_hash=request.action_hash,
        correlation_id="correlation.api-06.renew",
    )

    created = await service.request(command, principal=_requester())
    approvals.chain = (linked_expired, replacement)
    replayed = await service.request(command, principal=_requester())

    assert created.disposition is ApprovalRequestDisposition.RENEWED
    assert replayed.disposition is ApprovalRequestDisposition.EXISTING
    assert created.approval == replayed.approval
    assert created.approval.approval_id == replacement_request.id
    assert created.approval.generation == request.generation + 1
    assert created.approval.action_id == request.action_id
    assert created.approval.payload_hash == request.action_hash
    assert created.approval.redacted_payload == request.redacted_projection["payload"]
    assert created.approval.policy_id == request.policy.policy_id
    assert created.approval.requested_by == "principal.api-06.operator"
    assert [call["expected_version"] for call in records.calls] == [2, 2]
    assert all(call["request_id"] == request.id for call in records.calls)
    assert all(call["requested_by"] == "principal.api-06.operator" for call in records.calls)
    assert all(call["expected_action_hash"] == request.action_hash for call in records.calls)
    assert all(
        isinstance(call["audit_context"], AuditContext)
        and call["audit_context"].binds_authenticated_user(
            actor_id="principal.api-06.operator",
            authentication_method="bearer",
            correlation_id=command.correlation_id,
        )
        for call in records.calls
    )
    requesting_approver = human_principal(
        actor_id="principal.api-06.operator",
        roles=frozenset({"operator", "approver", "role.approver"}),
        scopes=frozenset(
            {
                "approvals:read",
                "approvals:request",
                "approvals:decide",
                "scope.external-write",
            }
        ),
    )
    with pytest.raises(ApprovalAuthorizationError) as captured:
        authorize_approval_decision(requesting_approver, replacement.request)
    assert captured.value.code == "self_approval_forbidden"


@pytest.mark.asyncio
async def test_api_06_renewal_replay_created_false_returns_existing_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _approval_request()
    expired = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.EXPIRED,
        version=2,
        updated_at=request.expires_at,
        expired_at=request.expires_at,
    )
    renewed_at = request.expires_at + timedelta(seconds=1)
    replacement_request = replace(
        request,
        id="approval-request.replay-winner",
        generation=2,
        requested_by="principal.api-06.operator",
        requested_at=renewed_at,
        expires_at=renewed_at + timedelta(seconds=request.policy.expires_after_seconds),
    )
    replacement = StoredActionApprovalRequest.created(replacement_request)
    linked_expired = replace(
        expired,
        version=3,
        updated_at=renewed_at,
        replacement_request_id=replacement_request.id,
        renewed_at=renewed_at,
    )
    records = _ApprovalRecords(
        RenewedApprovalRequest(
            expired=linked_expired,
            replacement=replacement,
            created=False,
        )
    )
    approvals = _ApprovalRepository(chain=(expired,))
    service = _service(
        _Clock(renewed_at),
        _UnitOfWorkFactory(_UnitOfWork(approvals, _ActionRepository(_Action(request.action_hash)))),
    )
    monkeypatch.setattr(service, "_records", records)

    result = await service.request(
        ApprovalRequestCommand(
            action_id=request.action_id,
            expected_generation=1,
            expected_action_hash=request.action_hash,
            correlation_id="correlation.api-06.replay-winner",
        ),
        principal=_requester(),
    )

    assert result.disposition is ApprovalRequestDisposition.EXISTING
    assert result.approval.approval_id == replacement_request.id
    assert result.approval.requested_by == "principal.api-06.operator"


@pytest.mark.asyncio
async def test_api_06_renewal_conflict_rereads_and_returns_exact_concurrent_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _approval_request()
    expired = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.EXPIRED,
        version=2,
        updated_at=request.expires_at,
        expired_at=request.expires_at,
    )
    renewed_at = request.expires_at + timedelta(seconds=1)
    replacement_request = replace(
        request,
        id="approval-request.concurrent-winner",
        generation=2,
        requested_by="principal.api-06.operator",
        requested_at=renewed_at,
        expires_at=renewed_at + timedelta(seconds=request.policy.expires_after_seconds),
    )
    replacement = StoredActionApprovalRequest.created(replacement_request)
    linked_expired = replace(
        expired,
        version=3,
        updated_at=renewed_at,
        replacement_request_id=replacement_request.id,
        renewed_at=renewed_at,
    )
    approvals = _ApprovalRepository(chain=(expired,))
    records = _ConflictingApprovalRecords(
        approvals,
        (linked_expired, replacement),
    )
    service = _service(
        _Clock(renewed_at),
        _UnitOfWorkFactory(_UnitOfWork(approvals, _ActionRepository(_Action(request.action_hash)))),
    )
    monkeypatch.setattr(service, "_records", records)

    result = await service.request(
        ApprovalRequestCommand(
            action_id=request.action_id,
            expected_generation=1,
            expected_action_hash=request.action_hash,
            correlation_id="correlation.api-06.concurrent-winner",
        ),
        principal=_requester(),
    )

    assert result.disposition is ApprovalRequestDisposition.EXISTING
    assert result.approval.approval_id == replacement_request.id
    assert records.calls == 1
    assert approvals.chain_calls == [request.action_id, request.action_id]


@pytest.mark.parametrize("control", ("\x00", "\x1f", "\x7f", "\x9f", "\ud800"))
def test_api_06_decision_reason_rejects_controls_in_command_and_domain(control: str) -> None:
    request = _approval_request()
    reason = f"unsafe{control}reason"

    with pytest.raises(ValueError, match="unsupported characters"):
        ApprovalDecisionCommand(
            request_id=request.id,
            expected_generation=request.generation,
            expected_action_hash=request.action_hash,
            decision=ApprovalDecisionKind.REJECT,
            correlation_id="correlation.api-06.invalid-reason",
            reason=reason,
        )

    with pytest.raises(ValueError, match="unsupported characters"):
        ApprovalDecision(
            id="approval-decision.invalid-reason",
            request_id=request.id,
            action_id=request.action_id,
            action_hash=request.action_hash,
            authorization_set_id=request.authorization_set_id,
            run_id=request.run_id,
            plan_hash=request.plan_hash,
            proposal_revision=request.proposal_revision,
            step_id=request.step_id,
            step_key=request.step_key,
            actor_id="principal.api-06.approver",
            authentication_method="bearer",
            correlation_id="correlation.api-06.invalid-reason",
            decision=ApprovalDecisionKind.REJECT,
            authority_roles=frozenset({"approver"}),
            authority_scopes=frozenset({"approvals:decide"}),
            reason_code="approval_rejected",
            decided_at=request.requested_at,
            reason=reason,
        )


@pytest.mark.asyncio
async def test_api_06_missing_initial_chain_fails_closed_before_clock_or_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _approval_request()
    approvals = _ApprovalRepository(chain=())
    units = _UnitOfWorkFactory(
        _UnitOfWork(approvals, _ActionRepository(_Action(request.action_hash)))
    )
    service = _service(_ExplodingClock(), units)
    monkeypatch.setattr(service, "_records", _ExplodingApprovalRecords())

    with pytest.raises(ApprovalResourceServiceError) as captured:
        await service.request(
            ApprovalRequestCommand(
                action_id=request.action_id,
                expected_generation=0,
                expected_action_hash=request.action_hash,
                correlation_id="correlation.api-06.missing-chain",
            ),
            principal=_requester(),
        )

    assert captured.value.code == "approval_request_missing"
    assert approvals.chain_calls == [request.action_id]
