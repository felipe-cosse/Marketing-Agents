"""Standalone immutable briefs with durable retries and redacted, expiring views.

No executable workflow consumes these resources. Restricted validated content is
retained separately from its operator projection, just as admitted input is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.policies.manual_work_authorization import (
    ManualWorkAuthorizationError,
    authorize_manual_work_operator,
)
from marketing_agents.application.policies.runtime_resource_authorization import (
    RuntimeResourceAuthorizationError,
    authorize_runtime_resource_reader,
)
from marketing_agents.application.ports.campaign_briefs import (
    CampaignBriefSnapshot,
    brief_content,
    snapshot_document,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import CampaignBrief
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.retention import RetentionCategory, RetentionPolicy
from marketing_agents.domain.validation import frozen_json_mapping, require_id, require_utc
from marketing_agents.security.digest_key import DigestKey, digest_key_fingerprint
from marketing_agents.security.redaction import SecretValue, redact, redaction_classification

MAX_BRIEF_BYTES = 65_536
MAX_BRIEF_DEPTH = 16
BRIEF_ID_PATTERN = r"^brief\.[0-9a-f]{64}$"
IDEMPOTENCY_KEY_PATTERN = r"^[\x21-\x7e]{8,240}$"
_SCHEMA = {
    "properties": {
        # A short operational label remains visible. Free-text objectives and
        # external source references are restricted, not operator diagnostics.
        "objective": {"x-data-classification": "sensitive"},
        "source_refs": {"x-data-classification": "sensitive"},
    }
}


class CampaignBriefError(ValueError):
    """Codes and messages contain no supplied content or retry material."""

    def __init__(self, code: str) -> None:
        super().__init__("campaign brief request could not be completed")
        self.code = code


def _bounded_content(value: object) -> Mapping[str, Any]:
    """Validate before canonicalization to bound direct service callers too."""
    try:
        stack = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if depth > MAX_BRIEF_DEPTH or nodes > 4096:
                raise ValueError("brief structure exceeds bounds")
            if isinstance(item, Mapping):
                if len(item) > 256:
                    raise ValueError("brief object exceeds bounds")
                for key, child in item.items():
                    if type(key) is not str or not 1 <= len(key) <= 200:
                        raise ValueError("brief key is invalid")
                    stack.append((child, depth + 1))
            elif isinstance(item, list | tuple):
                if type(item) not in {list, tuple}:
                    raise ValueError("brief array is not strict JSON")
                if len(item) > 256:
                    raise ValueError("brief array exceeds bounds")
                stack.extend((child, depth + 1) for child in item)
            elif type(item) is str:
                if len(item) > 2000:
                    raise ValueError("brief string exceeds bounds")
            elif item is not None and type(item) not in {int, float, bool}:
                raise ValueError("brief is not strict JSON")
        encoded = canonical_json_bytes(value)
        if len(encoded) > MAX_BRIEF_BYTES:
            raise ValueError("brief exceeds byte bound")
        plain = json.loads(encoded)
        if type(plain) is not dict or set(plain) != {
            "title",
            "objective",
            "constraints",
            "source_refs",
        }:
            raise ValueError("brief fields are invalid")
        if type(plain["constraints"]) is not dict or type(plain["source_refs"]) is not list:
            raise ValueError("brief collections are invalid")
        if len(plain["source_refs"]) > 64 or any(
            type(ref) is not str or not 1 <= len(ref) <= 240 for ref in plain["source_refs"]
        ):
            raise ValueError("brief source references are invalid")
        CampaignBrief(
            "brief.validation",
            plain["title"],
            plain["objective"],
            plain["constraints"],
            tuple(plain["source_refs"]),
        )
        return frozen_json_mapping(plain, "campaign brief content")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise CampaignBriefError("campaign_brief_invalid") from None


@dataclass(frozen=True, slots=True)
class CreateCampaignBriefCommand:
    title: str = field(repr=False)
    objective: str = field(repr=False)
    constraints: Mapping[str, Any] = field(repr=False)
    source_refs: tuple[str, ...] = field(repr=False)
    idempotency_key: SecretValue = field(repr=False)
    correlation_id: str

    def __post_init__(self) -> None:
        if (
            type(self.idempotency_key) is not SecretValue
            or re.fullmatch(IDEMPOTENCY_KEY_PATTERN, self.idempotency_key.reveal()) is None
        ):
            raise CampaignBriefError("campaign_brief_retry_key_invalid")
        try:
            require_id(self.correlation_id, "campaign brief correlation ID")
        except (TypeError, ValueError):
            raise CampaignBriefError("campaign_brief_invalid") from None
        content = _bounded_content(
            {
                "title": self.title,
                "objective": self.objective,
                "constraints": self.constraints,
                "source_refs": self.source_refs,
            }
        )
        for name, value in content.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class CampaignBriefView:
    id: str
    revision: int
    created_at: datetime
    expires_at: datetime
    classification: str
    expired: bool
    details: Mapping[str, Any] | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class CampaignBriefResult:
    disposition: str
    resource: CampaignBriefView


class CampaignBriefService:
    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
        retention_policy: RetentionPolicy | None = None,
    ) -> None:
        self._dependencies = dependencies
        self._key = digest_key
        self._retention = retention_policy or RetentionPolicy()
        self._key_version = digest_key_fingerprint(digest_key)

    def _digest(self, purpose: str, value: object) -> str:
        return hmac.new(
            self._key.bytes_for_digest(),
            f"marketing-agents:campaign-brief:{purpose}:v1\0".encode("ascii")
            + canonical_json_bytes(value),
            hashlib.sha256,
        ).hexdigest()

    def _verify(self, snapshot: CampaignBriefSnapshot, brief_id: str) -> None:
        try:
            if type(snapshot) is not CampaignBriefSnapshot:
                raise ValueError("snapshot type")
            content = _bounded_content(brief_content(snapshot.brief))
            require_utc(snapshot.created_at, "brief creation time")
            require_utc(snapshot.expires_at, "brief expiry time")
            classification = redaction_classification(content, schema=_SCHEMA)
            if (
                snapshot.brief.id != brief_id
                or type(snapshot.revision) is not int
                or snapshot.revision != 1
                or snapshot.key_version != self._key_version
                or snapshot.expires_at <= snapshot.created_at
                or classification is DataClassification.SECRET
                or snapshot.classification is not classification
                or canonical_json_bytes(snapshot.projection)
                != canonical_json_bytes(redact(content, schema=_SCHEMA))
                or not hmac.compare_digest(snapshot.body_digest, self._digest("body", content))
                or not hmac.compare_digest(
                    snapshot.integrity_digest, self._digest("snapshot", snapshot_document(snapshot))
                )
            ):
                raise ValueError("snapshot integrity")
        except (TypeError, ValueError, AttributeError):
            raise CampaignBriefError("campaign_brief_unavailable") from None

    def _view(self, snapshot: CampaignBriefSnapshot) -> CampaignBriefView:
        expired = self._dependencies.utc_now() >= snapshot.expires_at
        return CampaignBriefView(
            snapshot.brief.id,
            snapshot.revision,
            snapshot.created_at,
            snapshot.expires_at,
            snapshot.classification.value,
            expired,
            None if expired else snapshot.projection,
        )

    async def create(
        self, *, principal: AuthenticatedPrincipal, command: CreateCampaignBriefCommand
    ) -> CampaignBriefResult:
        try:
            authorize_manual_work_operator(principal)
        except ManualWorkAuthorizationError:
            raise CampaignBriefError("campaign_brief_forbidden") from None
        if type(command) is not CreateCampaignBriefCommand:
            raise CampaignBriefError("campaign_brief_invalid")
        command = replace(command)
        content = _bounded_content(
            {
                "title": command.title,
                "objective": command.objective,
                "constraints": command.constraints,
                "source_refs": command.source_refs,
            }
        )
        classification = redaction_classification(content, schema=_SCHEMA)
        if classification is DataClassification.SECRET:
            raise CampaignBriefError("campaign_brief_secret_not_retainable")
        brief_id = "brief." + self._digest("identity", command.idempotency_key.reveal())
        created_at = self._dependencies.utc_now()
        snapshot = CampaignBriefSnapshot(
            brief=CampaignBrief(
                brief_id, command.title, command.objective, command.constraints, command.source_refs
            ),
            revision=1,
            created_at=created_at,
            expires_at=self._retention.expires_at(
                RetentionCategory.ADMITTED_PAYLOAD, created_at, classification
            ),
            classification=classification,
            projection=frozen_json_mapping(redact(content, schema=_SCHEMA), "brief projection"),
            body_digest=self._digest("body", content),
            key_version=self._key_version,
            integrity_digest="",
        )
        snapshot = replace(
            snapshot, integrity_digest=self._digest("snapshot", snapshot_document(snapshot))
        )
        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                stored = await unit_of_work.campaign_briefs.add_or_get(snapshot)
                self._verify(stored.snapshot, brief_id)
                if not hmac.compare_digest(stored.snapshot.body_digest, snapshot.body_digest):
                    raise CampaignBriefError("campaign_brief_idempotency_conflict")
                if stored.inserted:
                    context = AuditContext.authenticated_user(
                        principal.actor_id,
                        authentication_method=principal.authentication_method.value,
                        correlation_id=command.correlation_id,
                    )
                    event = AuditEventFactory(
                        context, retention_policy=self._retention
                    ).campaign_brief_created(
                        brief_id=brief_id,
                        occurred_at=created_at,
                    )
                    await unit_of_work.audits.append_global_many((event,))
                await unit_of_work.commit()
                return CampaignBriefResult(
                    "created" if stored.inserted else "replayed", self._view(stored.snapshot)
                )
        except CampaignBriefError:
            raise
        except Exception:
            raise CampaignBriefError("campaign_brief_unavailable") from None

    async def read(self, *, principal: AuthenticatedPrincipal, brief_id: str) -> CampaignBriefView:
        try:
            authorize_runtime_resource_reader(principal)
        except RuntimeResourceAuthorizationError:
            raise CampaignBriefError("campaign_brief_forbidden") from None
        if type(brief_id) is not str or re.fullmatch(BRIEF_ID_PATTERN, brief_id) is None:
            raise CampaignBriefError("campaign_brief_not_found")
        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                snapshot = await unit_of_work.campaign_briefs.get(brief_id)
                if snapshot is None:
                    raise CampaignBriefError("campaign_brief_not_found")
                self._verify(snapshot, brief_id)
                return self._view(snapshot)
        except CampaignBriefError:
            raise
        except Exception:
            raise CampaignBriefError("campaign_brief_unavailable") from None
