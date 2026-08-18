"""Convert one atomically reserved exact approval into a sealed connector proof."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from marketing_agents.domain.action_hash import CanonicalExternalAction, canonical_action_hash


class WriteAuthorizationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalReservation(BaseModel):
    """Snapshot produced only after the complete approval set is atomically consumed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reservation_id: str = Field(min_length=1, max_length=200)
    authorization_set_id: str = Field(min_length=1, max_length=200)
    state: Literal["dispatch_reserved"]
    action_id: str = Field(min_length=1, max_length=200)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    approval_request_id: str = Field(min_length=1, max_length=200)
    approval_decision_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=16, max_length=240)
    reserved_at: AwareDatetime

    @field_validator("reserved_at")
    @classmethod
    def require_utc_reserved_at(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("reservation time must be UTC")
        return value

    @model_validator(mode="after")
    def require_distinct_approval_records(self) -> Self:
        if self.approval_request_id == self.approval_decision_id:
            raise ValueError("approval request and decision IDs must be distinct")
        return self


_AUTHORIZATION_SEAL = object()


@dataclass(frozen=True, slots=True)
class AuthorizedExternalWrite:
    """Internal sealed proof accepted by mutating connector ports."""

    action: CanonicalExternalAction
    action_hash: str
    reservation_id: str
    approval_request_id: str
    approval_decision_id: str
    idempotency_key: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _AUTHORIZATION_SEAL:
            raise WriteAuthorizationError(
                "invalid_authorization_seal", "authorized writes must come from the write guard"
            )


class WriteAuthorizationGuard:
    def authorize(
        self,
        action: CanonicalExternalAction,
        reservation: ApprovalReservation,
        idempotency_key: str,
    ) -> AuthorizedExternalWrite:
        computed_hash = canonical_action_hash(action)
        checks = (
            (reservation.action_id == action.action_id, "approval_action_mismatch"),
            (reservation.action_hash == computed_hash, "approval_hash_mismatch"),
            (
                reservation.authorization_set_id == action.authorization_set_id,
                "authorization_set_mismatch",
            ),
            (reservation.capability_id == action.capability_id, "approval_capability_mismatch"),
            (reservation.binding_id == action.binding_id, "approval_binding_mismatch"),
            (reservation.idempotency_key == idempotency_key, "idempotency_key_mismatch"),
        )
        for valid, code in checks:
            if not valid:
                raise WriteAuthorizationError(code, "reserved approval does not match exact action")
        return AuthorizedExternalWrite(
            action=action,
            action_hash=computed_hash,
            reservation_id=reservation.reservation_id,
            approval_request_id=reservation.approval_request_id,
            approval_decision_id=reservation.approval_decision_id,
            idempotency_key=idempotency_key,
            _seal=_AUTHORIZATION_SEAL,
        )
