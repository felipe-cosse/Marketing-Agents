"""Authenticated approval queries and exact existing-action request renewal."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.policies.approval_resource_authorization import (
    ApprovalResourceAuthorizationError,
    authorize_approval_reader,
    authorize_approval_requester,
)
from marketing_agents.application.ports.repositories import (
    ApprovalRepositoryConflict,
    ExternalActionRepositoryConflict,
)
from marketing_agents.application.services.approval_records import (
    ApprovalRecordService,
    ApprovalRecordServiceError,
)
from marketing_agents.domain.approval import StoredActionApprovalRequest
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import ApprovalStatus
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.validation import require_digest, require_id, require_utc

DEFAULT_APPROVAL_PAGE_SIZE = 25
MAX_APPROVAL_PAGE_SIZE = 100
MAX_APPROVAL_CURSOR_LENGTH = 1_024
_CURSOR_PREFIX = "approval-page-v1."


class ApprovalResourceServiceError(ValueError):
    """Stable non-sensitive failure raised by the approval API application seam."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalRequestDisposition(StrEnum):
    EXISTING = "existing"
    RENEWED = "renewed"


@dataclass(frozen=True, slots=True)
class ApprovalListQuery:
    status: ApprovalStatus | None = None
    run_id: str | None = None
    action_id: str | None = None
    cursor: str | None = field(default=None, repr=False)
    limit: int = DEFAULT_APPROVAL_PAGE_SIZE

    def __post_init__(self) -> None:
        if self.status is not None and type(self.status) is not ApprovalStatus:
            raise ValueError("approval status filter must use the exact enum")
        for value, name in (
            (self.run_id, "approval run filter"),
            (self.action_id, "approval action filter"),
        ):
            if value is not None:
                require_id(value, name)
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_APPROVAL_PAGE_SIZE:
            raise ValueError("approval page limit is outside the supported range")
        if self.cursor is not None and (
            type(self.cursor) is not str
            or not self.cursor
            or len(self.cursor) > MAX_APPROVAL_CURSOR_LENGTH
        ):
            raise ValueError("approval page cursor is invalid")


@dataclass(frozen=True, slots=True)
class ApprovalRequestCommand:
    action_id: str
    expected_generation: int
    expected_action_hash: str = field(repr=False)
    correlation_id: str = field(repr=False)

    def __post_init__(self) -> None:
        require_id(self.action_id, "approval action ID")
        if type(self.expected_generation) is not int or self.expected_generation < 0:
            raise ValueError("approval expected generation must be a nonnegative integer")
        require_digest(self.expected_action_hash, "approval expected action hash")
        require_id(self.correlation_id, "approval request correlation ID")


@dataclass(frozen=True, slots=True)
class ApprovalResource:
    approval_id: str
    status: ApprovalStatus
    resource_version: int
    generation: int
    one_time_use_state: str
    action_id: str
    action_type: str
    capability_id: str
    connector_family: str
    binding_id: str
    destination_summary: str
    redacted_payload: Mapping[str, Any] = field(repr=False)
    payload_hash: str = field(repr=False)
    run_id: str
    step_id: str
    template_id: str
    instance_id: str
    policy_id: str
    required_roles: tuple[str, ...]
    required_scopes: tuple[str, ...]
    allow_self_approval: bool
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    updated_at: datetime
    is_expired: bool
    is_actionable: bool
    decision_id: str | None
    decision_kind: str | None
    decision_actor_id: str | None
    decision_reason_code: str | None
    decision_reason: str | None = field(repr=False)
    decided_at: datetime | None
    expired_at: datetime | None
    replacement_approval_id: str | None
    renewed_at: datetime | None
    superseded_at: datetime | None
    superseded_reason_code: str | None
    consumed_at: datetime | None
    approval_url: str
    action_url: str
    run_url: str
    step_url: str
    template_url: str
    instance_url: str


@dataclass(frozen=True, slots=True)
class ApprovalPage:
    items: tuple[ApprovalResource, ...]
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ApprovalRequestResult:
    approval: ApprovalResource
    disposition: ApprovalRequestDisposition


@dataclass(frozen=True, slots=True)
class _CursorBoundary:
    requested_at: datetime
    request_id: str


def _filter_fingerprint(query: ApprovalListQuery) -> str:
    return hashlib.sha256(
        b"marketing-agents:approval-page-filter:v1\x00"
        + canonical_json_bytes(
            {
                "status": None if query.status is None else query.status.value,
                "run_id": query.run_id,
                "action_id": query.action_id,
            }
        )
    ).hexdigest()


def _encode_cursor(resource: ApprovalResource, query: ApprovalListQuery) -> str:
    payload = canonical_json_bytes(
        {
            "filter": _filter_fingerprint(query),
            "id": resource.approval_id,
            "requested_at": resource.requested_at.isoformat(timespec="microseconds"),
            "version": 1,
        }
    )
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_CURSOR_PREFIX}{token}"


def _decode_cursor(query: ApprovalListQuery) -> _CursorBoundary | None:
    if query.cursor is None:
        return None
    if not query.cursor.startswith(_CURSOR_PREFIX):
        raise ApprovalResourceServiceError(
            "approval_cursor_invalid",
            "approval page cursor is invalid",
        )
    encoded = query.cursor[len(_CURSOR_PREFIX) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        decoded = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ApprovalResourceServiceError(
            "approval_cursor_invalid",
            "approval page cursor is invalid",
        ) from None
    if (
        type(decoded) is not dict
        or set(decoded) != {"filter", "id", "requested_at", "version"}
        or decoded.get("version") != 1
        or type(decoded.get("filter")) is not str
        or type(decoded.get("id")) is not str
        or type(decoded.get("requested_at")) is not str
    ):
        raise ApprovalResourceServiceError(
            "approval_cursor_invalid",
            "approval page cursor is invalid",
        )
    try:
        require_digest(decoded["filter"], "approval cursor filter")
        if not hmac.compare_digest(decoded["filter"], _filter_fingerprint(query)):
            raise ValueError("approval cursor filters changed")
        requested_at = datetime.fromisoformat(decoded["requested_at"])
        require_utc(requested_at, "approval cursor time")
        require_id(decoded["id"], "approval cursor request ID")
    except (TypeError, ValueError):
        raise ApprovalResourceServiceError(
            "approval_cursor_invalid",
            "approval page cursor is invalid",
        ) from None
    canonical = _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if not hmac.compare_digest(canonical, query.cursor):
        raise ApprovalResourceServiceError(
            "approval_cursor_invalid",
            "approval page cursor is invalid",
        )
    return _CursorBoundary(requested_at=requested_at, request_id=decoded["id"])


def _plain_mapping(value: object) -> Mapping[str, Any]:
    decoded = json.loads(canonical_json_bytes(value))
    if type(decoded) is not dict:
        raise ApprovalResourceServiceError(
            "approval_record_corrupt",
            "approval safe projection is invalid",
        )
    return cast(dict[str, Any], decoded)


def _project(stored: StoredActionApprovalRequest, *, now: datetime) -> ApprovalResource:
    require_utc(now, "approval projection time")
    request = stored.request
    decision = stored.decision
    use = stored.use
    payload = _plain_mapping(request.redacted_projection.get("payload"))
    logically_expired = stored.status is ApprovalStatus.EXPIRED or (
        stored.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
        and now >= request.expires_at
    )
    return ApprovalResource(
        approval_id=request.id,
        status=stored.status,
        resource_version=stored.version,
        generation=request.generation,
        one_time_use_state="consumed" if use is not None else "unused",
        action_id=request.action_id,
        action_type=request.action_type,
        capability_id=request.capability_id,
        connector_family=request.connector_family,
        binding_id=request.binding_id,
        destination_summary=request.redacted_destination,
        redacted_payload=payload,
        payload_hash=request.action_hash,
        run_id=request.run_id,
        step_id=request.step_id,
        template_id=request.template_id,
        instance_id=request.instance_id,
        policy_id=request.policy.policy_id,
        required_roles=tuple(sorted(request.policy.required_roles)),
        required_scopes=tuple(sorted(request.policy.required_scopes)),
        allow_self_approval=request.policy.allow_self_approval,
        requested_by=request.requested_by,
        requested_at=request.requested_at,
        expires_at=request.expires_at,
        updated_at=stored.updated_at,
        is_expired=logically_expired,
        is_actionable=stored.status is ApprovalStatus.PENDING and not logically_expired,
        decision_id=None if decision is None else decision.id,
        decision_kind=None if decision is None else decision.decision.value,
        decision_actor_id=None if decision is None else decision.actor_id,
        decision_reason_code=None if decision is None else decision.reason_code,
        decision_reason=None if decision is None else decision.reason,
        decided_at=None if decision is None else decision.decided_at,
        expired_at=stored.expired_at,
        replacement_approval_id=stored.replacement_request_id,
        renewed_at=stored.renewed_at,
        superseded_at=stored.superseded_at,
        superseded_reason_code=stored.superseded_reason_code,
        consumed_at=None if use is None else use.used_at,
        approval_url=f"/api/v1/approvals/{request.id}",
        action_url=f"/api/v1/external-actions/{request.action_id}",
        run_url=f"/api/v1/runs/{request.run_id}",
        step_url=f"/api/v1/runs/{request.run_id}/steps/{request.step_id}",
        template_url=f"/api/v1/agent-templates/{request.template_id}",
        instance_url=f"/api/v1/agent-instances/{request.instance_id}",
    )


class ApprovalResourceService:
    """Authorize and project approvals without exposing execution authority."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        if type(dependencies) is not OrchestrationDependencies:
            raise ValueError("approval resources require exact orchestration dependencies")
        self._dependencies = dependencies
        self._records = ApprovalRecordService(dependencies)

    async def list(
        self,
        query: ApprovalListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalPage:
        self._authorize_read(principal)
        if type(query) is not ApprovalListQuery:
            raise ApprovalResourceServiceError(
                "approval_query_invalid",
                "approval list query is invalid",
            )
        boundary = _decode_cursor(query)
        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                stored = await unit_of_work.approvals.list_requests(
                    status=query.status,
                    run_id=query.run_id,
                    action_id=query.action_id,
                    before_requested_at=None if boundary is None else boundary.requested_at,
                    before_request_id=None if boundary is None else boundary.request_id,
                    limit=query.limit + 1,
                )
        except (ApprovalRepositoryConflict, ExternalActionRepositoryConflict):
            raise ApprovalResourceServiceError(
                "approval_record_corrupt",
                "approval resources could not be validated",
            ) from None
        now = self._dependencies.utc_now()
        page_items = tuple(_project(item, now=now) for item in stored[: query.limit])
        next_cursor = (
            _encode_cursor(page_items[-1], query)
            if len(stored) > query.limit and page_items
            else None
        )
        return ApprovalPage(items=page_items, next_cursor=next_cursor)

    async def read(
        self,
        approval_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalResource:
        self._authorize_read(principal)
        try:
            require_id(approval_id, "approval request ID")
        except (TypeError, ValueError):
            raise ApprovalResourceServiceError(
                "approval_query_invalid",
                "approval request identity is invalid",
            ) from None
        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                stored = await unit_of_work.approvals.get_inspectable(approval_id)
        except (ApprovalRepositoryConflict, ExternalActionRepositoryConflict):
            raise ApprovalResourceServiceError(
                "approval_record_corrupt",
                "approval resource could not be validated",
            ) from None
        if stored is None:
            raise ApprovalResourceServiceError(
                "approval_request_missing",
                "approval request does not exist",
            )
        return _project(stored, now=self._dependencies.utc_now())

    async def request(
        self,
        command: ApprovalRequestCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalRequestResult:
        self._authorize_request(principal)
        if type(command) is not ApprovalRequestCommand:
            raise ApprovalResourceServiceError(
                "approval_request_invalid",
                "approval request command is invalid",
            )
        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                action = await unit_of_work.external_actions.get(command.action_id)
                if action is None:
                    raise ApprovalResourceServiceError(
                        "approval_action_missing",
                        "approval action does not exist",
                    )
                if not hmac.compare_digest(action.action_hash, command.expected_action_hash):
                    raise ApprovalResourceServiceError(
                        "approval_hash_mismatch",
                        "approval request action hash changed",
                    )
                chain = await unit_of_work.approvals.list_for_action(command.action_id)
        except ApprovalResourceServiceError:
            raise
        except (ApprovalRepositoryConflict, ExternalActionRepositoryConflict):
            raise ApprovalResourceServiceError(
                "approval_record_corrupt",
                "approval request authority could not be validated",
            ) from None
        if not chain:
            raise ApprovalResourceServiceError(
                "approval_request_missing",
                "approval action has no complete immutable request set",
            )
        if any(
            item.request.action_id != command.action_id
            or not hmac.compare_digest(item.request.action_hash, command.expected_action_hash)
            for item in chain
        ):
            raise ApprovalResourceServiceError(
                "approval_record_corrupt",
                "approval request chain differs from its exact action",
            )

        now = self._dependencies.utc_now()
        current = chain[-1]
        if command.expected_generation == 0:
            if current.status not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
                raise ApprovalResourceServiceError(
                    "approval_request_conflict",
                    "approval request is no longer reusable",
                )
            if now >= current.request.expires_at:
                raise ApprovalResourceServiceError(
                    "approval_expired",
                    "approval request requires an exact replacement generation",
                )
            return ApprovalRequestResult(
                approval=_project(current, now=now),
                disposition=ApprovalRequestDisposition.EXISTING,
            )

        source = next(
            (item for item in chain if item.request.generation == command.expected_generation),
            None,
        )
        if source is None:
            raise ApprovalResourceServiceError(
                "approval_generation_conflict",
                "approval generation changed",
            )
        if source.replacement_request_id is None and source != current:
            raise ApprovalResourceServiceError(
                "approval_generation_conflict",
                "approval request is not the current generation",
            )
        if source.replacement_request_id is None and source.status not in {
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
            ApprovalStatus.EXPIRED,
        }:
            raise ApprovalResourceServiceError(
                "approval_request_conflict",
                "approval request cannot be replaced",
            )
        if (
            source.replacement_request_id is None
            and source.status is not ApprovalStatus.EXPIRED
            and now < source.request.expires_at
        ):
            raise ApprovalResourceServiceError(
                "approval_not_expired",
                "approval request is not yet renewable",
            )
        expected_version = (
            max(1, source.version - 1)
            if source.replacement_request_id is not None
            else source.version
        )
        audit_context = AuditContext.authenticated_user(
            principal.actor_id,
            authentication_method=principal.authentication_method.value,
            correlation_id=command.correlation_id,
        )
        try:
            renewed = await self._records.renew_expired(
                request_id=source.request.id,
                expected_version=expected_version,
                expected_action_hash=command.expected_action_hash,
                audit_context=audit_context,
                requested_by=principal.actor_id,
            )
        except ApprovalRepositoryConflict as error:
            if error.code == "approval_renewal_conflict":
                winner = await self._reconcile_renewal_winner(command)
                if winner is not None:
                    return ApprovalRequestResult(
                        approval=_project(winner, now=self._dependencies.utc_now()),
                        disposition=ApprovalRequestDisposition.EXISTING,
                    )
            raise ApprovalResourceServiceError(
                "approval_request_conflict",
                "approval request could not be renewed",
            ) from None
        except ApprovalRecordServiceError as error:
            code = getattr(error, "code", "approval_request_conflict")
            if code in {"approval_renewal_corrupt", "approval_record_corrupt"}:
                raise ApprovalResourceServiceError(
                    "approval_record_corrupt",
                    "approval renewal authority could not be validated",
                ) from None
            safe_code = (
                code
                if code
                in {
                    "approval_request_missing",
                    "approval_action_missing",
                    "approval_renewal_conflict",
                    "approval_expiration_conflict",
                    "expected_hash_mismatch",
                    "full_set_epoch_required",
                }
                else "approval_request_conflict"
            )
            raise ApprovalResourceServiceError(
                safe_code,
                "approval request could not be renewed",
            ) from None
        replacement = renewed.replacement
        disposition = (
            ApprovalRequestDisposition.EXISTING
            if source.replacement_request_id is not None or not renewed.created
            else ApprovalRequestDisposition.RENEWED
        )
        return ApprovalRequestResult(
            approval=_project(replacement, now=self._dependencies.utc_now()),
            disposition=disposition,
        )

    async def _reconcile_renewal_winner(
        self,
        command: ApprovalRequestCommand,
    ) -> StoredActionApprovalRequest | None:
        """Return a concurrent exact winner only after revalidating its complete chain."""

        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                chain = await unit_of_work.approvals.list_for_action(command.action_id)
        except (ApprovalRepositoryConflict, ExternalActionRepositoryConflict):
            return None
        source = next(
            (item for item in chain if item.request.generation == command.expected_generation),
            None,
        )
        if source is None or source.replacement_request_id is None:
            return None
        winner = next(
            (
                item
                for item in chain
                if item.request.id == source.replacement_request_id
                and item.request.generation == source.request.generation + 1
            ),
            None,
        )
        if (
            winner is None
            or winner.request.action_id != command.action_id
            or not hmac.compare_digest(
                winner.request.action_hash,
                command.expected_action_hash,
            )
        ):
            return None
        return winner

    @staticmethod
    def _authorize_read(principal: AuthenticatedPrincipal) -> None:
        try:
            authorize_approval_reader(principal)
        except ApprovalResourceAuthorizationError as exc:
            raise ApprovalResourceServiceError(exc.code, str(exc)) from None

    @staticmethod
    def _authorize_request(principal: AuthenticatedPrincipal) -> None:
        try:
            authorize_approval_requester(principal)
        except ApprovalResourceAuthorizationError as exc:
            raise ApprovalResourceServiceError(exc.code, str(exc)) from None


__all__ = [
    "DEFAULT_APPROVAL_PAGE_SIZE",
    "MAX_APPROVAL_CURSOR_LENGTH",
    "MAX_APPROVAL_PAGE_SIZE",
    "ApprovalListQuery",
    "ApprovalPage",
    "ApprovalRequestCommand",
    "ApprovalRequestDisposition",
    "ApprovalRequestResult",
    "ApprovalResource",
    "ApprovalResourceService",
    "ApprovalResourceServiceError",
]
