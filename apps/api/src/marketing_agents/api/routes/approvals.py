"""Authenticated approve/reject mutations for one immutable approval request."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from marketing_agents.api.dependencies import (
    ApprovalDecisionExecutor,
    get_approval_decision_executor,
    require_approval_principal,
)
from marketing_agents.api.schemas.approvals import (
    ApprovalDecisionInput,
    ApprovalDecisionResponse,
)
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionServiceError,
    AuthorizedApprovalDecision,
)
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal

_APPROVAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$"
_FORBIDDEN_CODES = frozenset(
    {
        "human_approval_required",
        "approval_role_missing",
        "approval_scope_missing",
        "self_approval_forbidden",
    }
)
_NOT_FOUND_CODES = frozenset({"approval_request_missing", "approval_action_missing"})
_INPUT_CODES = frozenset({"approval_command_invalid"})
_SAFE_CONFLICT_CODES = frozenset(
    {
        "approval_generation_conflict",
        "approval_decision_conflict",
        "approval_hash_mismatch",
        "approval_action_conflict",
        "approval_expired",
    }
)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


def _raise_safe_service_problem(error: ApprovalDecisionServiceError) -> None:
    if error.code in _FORBIDDEN_CODES:
        status_code = status.HTTP_403_FORBIDDEN
        code = error.code
        message = "approval decision is forbidden"
    elif error.code in _NOT_FOUND_CODES:
        status_code = status.HTTP_404_NOT_FOUND
        code = "approval_not_found"
        message = "approval request was not found"
    elif error.code in _INPUT_CODES:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        code = "approval_input_invalid"
        message = "approval decision input is invalid"
    else:
        status_code = status.HTTP_409_CONFLICT
        code = error.code if error.code in _SAFE_CONFLICT_CODES else "approval_conflict"
        message = "approval request could not be decided"
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    ) from None


async def _decide(
    *,
    approval_id: str,
    body: ApprovalDecisionInput,
    decision: ApprovalDecisionKind,
    principal: AuthenticatedPrincipal,
    executor: ApprovalDecisionExecutor,
) -> ApprovalDecisionResponse:
    correlation_id = f"correlation.approval-api.{secrets.token_hex(16)}"
    try:
        command = ApprovalDecisionCommand(
            request_id=approval_id,
            expected_generation=body.expected_generation,
            expected_action_hash=body.expected_payload_hash,
            decision=decision,
            correlation_id=correlation_id,
            reason=body.reason_text(),
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "approval_input_invalid",
                "message": "approval decision input is invalid",
            },
        ) from None
    try:
        result = await executor.decide(command, principal=principal)
    except ApprovalDecisionServiceError as error:
        _raise_safe_service_problem(error)
    expected_status = (
        ApprovalStatus.APPROVED
        if decision is ApprovalDecisionKind.APPROVE
        else ApprovalStatus.REJECTED
    )
    expected_action_state = (
        ExternalActionState.APPROVED
        if decision is ApprovalDecisionKind.APPROVE
        else ExternalActionState.REJECTED
    )
    if type(result) is not AuthorizedApprovalDecision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "approval_conflict",
                "message": "approval request could not be decided",
            },
        )
    request = result.request.request
    recorded = result.decision
    action = result.action
    if (
        request.id != approval_id
        or result.request.status is not expected_status
        or result.request.decision != recorded
        or action.state is not expected_action_state
        or action.id != request.action_id
        or action.action_hash != request.action_hash
        or action.run_id != request.run_id
        or recorded.decision is not decision
        or recorded.request_id != request.id
        or recorded.action_id != action.id
        or recorded.action_hash != action.action_hash
        or recorded.run_id != request.run_id
        or recorded.actor_id != principal.actor_id
        or recorded.authentication_method != principal.authentication_method.value
        or recorded.correlation_id != correlation_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "approval_conflict",
                "message": "approval request could not be decided",
            },
        )
    return ApprovalDecisionResponse(
        approval_id=request.id,
        decision_id=recorded.id,
        action_id=action.id,
        run_id=request.run_id,
        status=result.request.status.value,
    )


@router.post(
    "/{approval_id}/approve",
    response_model=ApprovalDecisionResponse,
    operation_id="approveApproval",
)
async def approve_approval(
    approval_id: Annotated[str, Path(pattern=_APPROVAL_ID_PATTERN)],
    body: ApprovalDecisionInput,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_approval_principal),
    ],
    executor: Annotated[
        ApprovalDecisionExecutor,
        Depends(get_approval_decision_executor),
    ],
) -> ApprovalDecisionResponse:
    return await _decide(
        approval_id=approval_id,
        body=body,
        decision=ApprovalDecisionKind.APPROVE,
        principal=principal,
        executor=executor,
    )


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalDecisionResponse,
    operation_id="rejectApproval",
)
async def reject_approval(
    approval_id: Annotated[str, Path(pattern=_APPROVAL_ID_PATTERN)],
    body: ApprovalDecisionInput,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_approval_principal),
    ],
    executor: Annotated[
        ApprovalDecisionExecutor,
        Depends(get_approval_decision_executor),
    ],
) -> ApprovalDecisionResponse:
    return await _decide(
        approval_id=approval_id,
        body=body,
        decision=ApprovalDecisionKind.REJECT,
        principal=principal,
        executor=executor,
    )
