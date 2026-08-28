"""Authenticated, fan-out-safe, idempotent webhook admission."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.repositories import WebhookReceiptRepositoryConflict
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.ports.webhook_admission import (
    WebhookAdmissionBinding,
    WebhookAdmissionResolutionError,
    WebhookAdmissionResolver,
)
from marketing_agents.application.ports.webhook_sources import (
    MappedWebhookEnvelope,
    WebhookEnvelopeMappingError,
    WebhookSourceDefinition,
    WebhookSourceRegistry,
)
from marketing_agents.application.ports.webhooks import (
    VerifiedWebhookIdentity,
    WebhookSecretResolutionError,
    WebhookSignatureVerificationError,
    require_webhook_source_id,
    require_webhook_trigger_id,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.idempotent_work_receipt import (
    IdempotentWorkRunReceiptService,
    WorkRunReceiptDisposition,
    WorkRunReceiptError,
    WorkRunReceiptResult,
)
from marketing_agents.application.services.incoming_work_validation import (
    IncomingWorkValidationError,
    ValidatedIncomingWork,
)
from marketing_agents.application.services.webhook_rate_limit import (
    ProcessLocalWebhookAdmissionRateLimiter,
    WebhookAdmissionRateLimiterUnavailable,
)
from marketing_agents.application.services.work_admission import WorkIdempotencyError
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    PrincipalKind,
)
from marketing_agents.domain.validation import require_id
from marketing_agents.domain.webhook import WebhookReceipt, WebhookReceiptDelivery
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.webhook_digest import (
    WebhookBodyDigest,
    derive_webhook_body_digest,
)

MAX_WEBHOOK_BODY_BYTES = 1_048_576
MAX_WEBHOOK_HEADERS = 128
MAX_WEBHOOK_HEADER_BYTES = 65_536


class WebhookAdmissionServiceError(RuntimeError):
    """Stable body-safe rejection at the webhook application boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        pointer: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = _safe_input_pointer(pointer)
        self.retry_after_seconds = _safe_retry_after_seconds(retry_after_seconds)


class WebhookAdmissionDisposition(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookAdmissionCommand:
    """Raw authenticated transport candidate; payload authority is intentionally absent."""

    source: str
    trigger_id: str
    raw_body: bytes = field(repr=False)
    received_headers: tuple[tuple[str, str], ...] = field(repr=False)
    correlation_id: str

    def __post_init__(self) -> None:
        try:
            require_webhook_source_id(self.source, "webhook command source")
            require_webhook_trigger_id(self.trigger_id, "webhook command trigger ID")
            require_id(self.correlation_id, "webhook command correlation ID")
        except (TypeError, ValueError):
            raise WebhookAdmissionServiceError(
                "webhook_command_invalid",
                "webhook admission command is invalid",
            ) from None
        if type(self.raw_body) is not bytes or len(self.raw_body) > MAX_WEBHOOK_BODY_BYTES:
            raise WebhookAdmissionServiceError(
                "webhook_command_invalid",
                "webhook admission command is invalid",
            )
        if (
            type(self.received_headers) is not tuple
            or len(self.received_headers) > MAX_WEBHOOK_HEADERS
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                for item in self.received_headers
            )
            or sum(
                len(name.encode("utf-8")) + len(value.encode("utf-8"))
                for name, value in self.received_headers
            )
            > MAX_WEBHOOK_HEADER_BYTES
        ):
            raise WebhookAdmissionServiceError(
                "webhook_command_invalid",
                "webhook admission command is invalid",
            )


@dataclass(frozen=True, slots=True)
class WebhookAdmissionResult:
    receipt: WebhookReceipt
    disposition: WebhookAdmissionDisposition

    def __post_init__(self) -> None:
        if type(self.receipt) is not WebhookReceipt:
            raise ValueError("webhook admission result requires one exact receipt")
        self.receipt.__post_init__()
        if type(self.disposition) is not WebhookAdmissionDisposition:
            raise ValueError("webhook admission result disposition is invalid")


def _safe_input_pointer(value: object) -> str | None:
    if type(value) is not str or not value.startswith("/input") or len(value) > 1_000:
        return None
    tokens = value.split("/")[1:]
    if not tokens or tokens[0] != "input" or len(tokens) > 65:
        return None
    if any(
        not token
        or len(token) > 100
        or any(not (character.isalnum() or character in "_.-") for character in token)
        for token in tokens[1:]
    ):
        return None
    return value


def _safe_retry_after_seconds(value: object) -> int | None:
    if type(value) is not int or not 1 <= value <= 3_600:
        return None
    return value


def _service_error(code: str, message: str) -> WebhookAdmissionServiceError:
    return WebhookAdmissionServiceError(code, message)


class WebhookAdmissionService:
    """Verify, map, validate, receipt, and audit one webhook atomically."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
        source_registry: WebhookSourceRegistry,
        resolver: WebhookAdmissionResolver,
        *,
        current_catalog_hash: str,
        admission_rate_limiter: ProcessLocalWebhookAdmissionRateLimiter | None = None,
    ) -> None:
        if type(dependencies) is not OrchestrationDependencies:
            raise ValueError("webhook service requires exact orchestration dependencies")
        if type(digest_key) is not DigestKey:
            raise ValueError("webhook service requires the exact digest key")
        if not callable(getattr(source_registry, "resolve", None)):
            raise ValueError("webhook service requires a source registry")
        if not callable(getattr(resolver, "resolve_all_in_uow", None)):
            raise ValueError("webhook service requires an admission resolver")
        self._dependencies = dependencies
        self._digest_key = digest_key
        self._source_registry = source_registry
        self._resolver = resolver
        if admission_rate_limiter is not None and type(admission_rate_limiter) is not (
            ProcessLocalWebhookAdmissionRateLimiter
        ):
            raise ValueError("webhook service requires an exact admission rate limiter")
        self._admission_rate_limiter = (
            ProcessLocalWebhookAdmissionRateLimiter()
            if admission_rate_limiter is None
            else admission_rate_limiter
        )
        self._receipt_service = IdempotentWorkRunReceiptService(
            dependencies,
            digest_key,
            current_catalog_hash=current_catalog_hash,
        )

    async def submit(self, command: WebhookAdmissionCommand) -> WebhookAdmissionResult:
        self._require_command(command)
        definition = self._source_registry.resolve(command.source, command.trigger_id)
        if type(definition) is not WebhookSourceDefinition:
            raise _service_error(
                "webhook_binding_forbidden",
                "webhook source is not configured",
            )
        now = self._dependencies.utc_now()
        attempt_id = self._new_id("webhook-ingress")
        try:
            identity = definition.signature_verifier.verify(
                source=command.source,
                trigger_id=command.trigger_id,
                raw_body=command.raw_body,
                received_headers=command.received_headers,
                received_at=now,
                verifier_config=definition.verifier_config,
            )
        except WebhookSecretResolutionError:
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            ) from None
        except WebhookSignatureVerificationError:
            try:
                await self._audit_signature_rejected(command, attempt_id=attempt_id, now=now)
            except Exception:
                raise _service_error(
                    "webhook_service_unavailable",
                    "webhook admission is temporarily unavailable",
                ) from None
            raise _service_error(
                "webhook_authentication_failed",
                "webhook authentication failed",
            ) from None
        self._require_identity(identity, command)
        audit_context = AuditContext.verified_webhook(
            identity.principal.actor_id,
            correlation_id=command.correlation_id,
        )
        try:
            rate_decision = self._admission_rate_limiter.consume(
                source=identity.source,
                observed_at=now,
                max_calls=definition.admission_rate_max_calls,
                window_seconds=definition.admission_rate_window_seconds,
            )
        except (TypeError, ValueError, WebhookAdmissionRateLimiterUnavailable):
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            ) from None
        if not rate_decision.allowed:
            retry_after_seconds = rate_decision.retry_after_seconds
            if retry_after_seconds is None:
                raise _service_error(
                    "webhook_service_unavailable",
                    "webhook admission is temporarily unavailable",
                )
            try:
                await self._audit_authenticated_rate_denial(
                    command,
                    audit_context=audit_context,
                    attempt_id=attempt_id,
                    retry_after_seconds=retry_after_seconds,
                    now=now,
                )
            except Exception:
                raise _service_error(
                    "webhook_service_unavailable",
                    "webhook admission is temporarily unavailable",
                ) from None
            raise WebhookAdmissionServiceError(
                "webhook_rate_limited",
                "webhook admission rate limit exceeded",
                retry_after_seconds=retry_after_seconds,
            )
        try:
            mapped = definition.mapper.parse(command.raw_body)
        except WebhookEnvelopeMappingError as error:
            await self._audit_schema_rejected_or_unavailable(
                command,
                audit_context=audit_context,
                attempt_id=attempt_id,
                now=now,
            )
            raise WebhookAdmissionServiceError(
                error.code,
                "webhook envelope is invalid",
                pointer=error.pointer,
            ) from None
        except Exception:
            await self._audit_schema_rejected_or_unavailable(
                command,
                audit_context=audit_context,
                attempt_id=attempt_id,
                now=now,
            )
            raise _service_error(
                "webhook_envelope_invalid",
                "webhook envelope is invalid",
            ) from None

        digest = derive_webhook_body_digest(command.raw_body, self._digest_key)
        try:
            return await self._submit_authenticated(
                command,
                definition=definition,
                identity=identity,
                mapped=mapped,
                body_digest=digest,
                audit_context=audit_context,
                attempt_id=attempt_id,
                now=now,
            )
        except WebhookAdmissionServiceError:
            raise
        except (WebhookReceiptRepositoryConflict, WorkRunReceiptError):
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            ) from None
        except (IncomingWorkValidationError, WorkIdempotencyError) as error:
            raise WebhookAdmissionServiceError(
                error.code,
                str(error),
                pointer=getattr(error, "pointer", None),
            ) from None
        except Exception:
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            ) from None

    async def _submit_authenticated(
        self,
        command: WebhookAdmissionCommand,
        *,
        definition: WebhookSourceDefinition,
        identity: VerifiedWebhookIdentity,
        mapped: MappedWebhookEnvelope,
        body_digest: WebhookBodyDigest,
        audit_context: AuditContext,
        attempt_id: str,
        now: datetime,
    ) -> WebhookAdmissionResult:
        del identity
        occurred_at = now
        deferred_error: WebhookAdmissionServiceError | None = None
        result: WebhookAdmissionResult | None = None
        async with self._dependencies.unit_of_work() as unit_of_work:
            existing = await unit_of_work.webhook_receipts.get_by_source_event(
                command.source,
                mapped.event_id,
            )
            if existing is not None:
                result, deferred_error = await self._audit_existing_receipt(
                    unit_of_work,
                    existing,
                    command=command,
                    definition=definition,
                    body_digest=body_digest,
                    audit_context=audit_context,
                    attempt_id=attempt_id,
                    occurred_at=occurred_at,
                )
                await unit_of_work.commit()
            else:
                try:
                    bindings = await self._resolver.resolve_all_in_uow(
                        unit_of_work,
                        source=command.source,
                        trigger_id=command.trigger_id,
                    )
                except WebhookAdmissionResolutionError as error:
                    await unit_of_work.audits.append_global(
                        AuditEventFactory(audit_context).webhook_signature_validated(
                            source=command.source,
                            trigger_id=command.trigger_id,
                            webhook_attempt_id=attempt_id,
                            occurred_at=occurred_at,
                        )
                    )
                    await unit_of_work.commit()
                    deferred_error = _service_error(error.code, str(error))
                else:
                    # PostgreSQL transactions may both observe an absent receipt before
                    # one waits on the resolver's deterministic configuration locks.
                    # Re-read after lock acquisition so the waiter classifies the
                    # winner's committed receipt instead of treating its work as orphaned.
                    existing = await unit_of_work.webhook_receipts.get_by_source_event(
                        command.source,
                        mapped.event_id,
                    )
                    if existing is not None:
                        result, deferred_error = await self._audit_existing_receipt(
                            unit_of_work,
                            existing,
                            command=command,
                            definition=definition,
                            body_digest=body_digest,
                            audit_context=audit_context,
                            attempt_id=attempt_id,
                            occurred_at=occurred_at,
                        )
                        await unit_of_work.commit()
                    else:
                        try:
                            validated = self._validate_all(bindings, mapped)
                        except IncomingWorkValidationError as error:
                            factory = AuditEventFactory(audit_context)
                            events = [
                                factory.webhook_signature_validated(
                                    source=command.source,
                                    trigger_id=command.trigger_id,
                                    webhook_attempt_id=attempt_id,
                                    occurred_at=occurred_at,
                                )
                            ]
                            binding = self._binding_for_pointer(bindings, error)
                            events.append(
                                factory.webhook_schema_rejected(
                                    source=command.source,
                                    trigger_id=command.trigger_id,
                                    webhook_attempt_id=attempt_id,
                                    occurred_at=occurred_at,
                                    instance_id=(None if binding is None else binding.instance_id),
                                    configuration_revision=(
                                        None if binding is None else binding.configuration_revision
                                    ),
                                    workflow_id=(None if binding is None else binding.workflow_id),
                                )
                            )
                            await unit_of_work.audits.append_global_many(tuple(events))
                            await unit_of_work.commit()
                            deferred_error = WebhookAdmissionServiceError(
                                error.code,
                                str(error),
                                pointer=error.pointer,
                            )
                        else:
                            for binding in bindings:
                                if (
                                    await unit_of_work.works.get_by_source_key(
                                        command.source,
                                        mapped.event_id,
                                        binding.instance_id,
                                    )
                                    is not None
                                ):
                                    raise _service_error(
                                        "webhook_receipt_missing",
                                        "webhook receipt state is incomplete",
                                    )
                            receipts = tuple(
                                [
                                    await self._receipt_service.receive_in_uow(
                                        unit_of_work,
                                        incoming,
                                        audit_context=audit_context,
                                    )
                                    for incoming in validated
                                ]
                            )
                            candidate = WebhookReceipt(
                                id=self._new_id("webhook-receipt"),
                                source=command.source,
                                event_id=mapped.event_id,
                                trigger_id=command.trigger_id,
                                body_digest=body_digest.value,
                                digest_key_version=body_digest.digest_key_version,
                                mapper_version=definition.mapper_version,
                                received_at=occurred_at,
                                deliveries=tuple(
                                    WebhookReceiptDelivery(
                                        instance_id=item.work_item.instance_id,
                                        work_item_id=item.work_item.id,
                                        run_id=item.run.id,
                                    )
                                    for item in receipts
                                ),
                            )
                            inserted = await unit_of_work.webhook_receipts.add_or_get(candidate)
                            self._require_insert_outcome(
                                candidate,
                                inserted.receipt,
                                receipts,
                                inserted.inserted,
                            )
                            disposition = (
                                WebhookAdmissionDisposition.CREATED
                                if inserted.inserted
                                else WebhookAdmissionDisposition.REPLAYED
                            )
                            factory = AuditEventFactory(audit_context)
                            events = [
                                factory.webhook_signature_validated(
                                    source=command.source,
                                    trigger_id=command.trigger_id,
                                    webhook_attempt_id=attempt_id,
                                    occurred_at=occurred_at,
                                ),
                            ]
                            if disposition is WebhookAdmissionDisposition.CREATED:
                                events.append(
                                    factory.webhook_received(
                                        source=command.source,
                                        trigger_id=command.trigger_id,
                                        webhook_attempt_id=attempt_id,
                                        webhook_receipt_id=inserted.receipt.id,
                                        target_count=len(inserted.receipt.deliveries),
                                        occurred_at=occurred_at,
                                    )
                                )
                            else:
                                events.append(
                                    factory.webhook_duplicate_suppressed(
                                        source=command.source,
                                        trigger_id=command.trigger_id,
                                        webhook_attempt_id=attempt_id,
                                        webhook_receipt_id=inserted.receipt.id,
                                        target_count=len(inserted.receipt.deliveries),
                                        occurred_at=occurred_at,
                                    )
                                )
                            await unit_of_work.audits.append_global_many(tuple(events))
                            await unit_of_work.commit()
                            result = WebhookAdmissionResult(
                                receipt=inserted.receipt,
                                disposition=disposition,
                            )
        if deferred_error is not None:
            raise deferred_error
        if result is None:
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            )
        return result

    async def _audit_existing_receipt(
        self,
        unit_of_work: UnitOfWork,
        existing: WebhookReceipt,
        *,
        command: WebhookAdmissionCommand,
        definition: WebhookSourceDefinition,
        body_digest: WebhookBodyDigest,
        audit_context: AuditContext,
        attempt_id: str,
        occurred_at: datetime,
    ) -> tuple[WebhookAdmissionResult | None, WebhookAdmissionServiceError | None]:
        exact = self._classify_existing(
            existing,
            definition=definition,
            body_digest=body_digest,
        )
        factory = AuditEventFactory(audit_context)
        events = [
            factory.webhook_signature_validated(
                source=command.source,
                trigger_id=command.trigger_id,
                webhook_attempt_id=attempt_id,
                occurred_at=occurred_at,
            ),
        ]
        if exact:
            events.append(
                factory.webhook_duplicate_suppressed(
                    source=command.source,
                    trigger_id=command.trigger_id,
                    webhook_attempt_id=attempt_id,
                    webhook_receipt_id=existing.id,
                    target_count=len(existing.deliveries),
                    occurred_at=occurred_at,
                )
            )
            result = WebhookAdmissionResult(
                receipt=existing,
                disposition=WebhookAdmissionDisposition.REPLAYED,
            )
            deferred_error = None
        else:
            events.append(
                factory.webhook_idempotency_collision(
                    source=command.source,
                    trigger_id=command.trigger_id,
                    webhook_attempt_id=attempt_id,
                    webhook_receipt_id=existing.id,
                    target_count=len(existing.deliveries),
                    occurred_at=occurred_at,
                )
            )
            result = None
            deferred_error = _service_error(
                "webhook_idempotency_conflict",
                "authenticated event identity is already bound to a different body",
            )
        await unit_of_work.audits.append_global_many(tuple(events))
        return result, deferred_error

    @staticmethod
    def _validate_all(
        bindings: tuple[WebhookAdmissionBinding, ...],
        mapped: MappedWebhookEnvelope,
    ) -> tuple[ValidatedIncomingWork, ...]:
        if (
            type(bindings) is not tuple
            or not bindings
            or any(type(item) is not WebhookAdmissionBinding for item in bindings)
        ):
            raise _service_error(
                "webhook_binding_unavailable",
                "webhook admission binding is unavailable",
            )
        validated: list[ValidatedIncomingWork] = []
        for binding in bindings:
            envelope = AdmissionEnvelope(
                source=binding.source,
                event_id=mapped.event_id,
                instance_id=binding.instance_id,
                trigger_id=binding.trigger_id,
                workflow_id=binding.workflow_id,
                mode=binding.mode,
                brief_id=None,
                brief_revision=None,
                configuration_revision=binding.configuration_revision,
                admitted_payload=mapped.input_payload,
            )
            validated.append(binding.validator.validate(envelope))
        return tuple(validated)

    @staticmethod
    def _binding_for_pointer(
        bindings: tuple[WebhookAdmissionBinding, ...],
        _error: IncomingWorkValidationError,
    ) -> WebhookAdmissionBinding | None:
        return bindings[0] if len(bindings) == 1 else None

    @staticmethod
    def _classify_existing(
        existing: WebhookReceipt,
        *,
        definition: WebhookSourceDefinition,
        body_digest: WebhookBodyDigest,
    ) -> bool:
        if existing.digest_key_version != body_digest.digest_key_version:
            raise _service_error(
                "webhook_digest_key_mismatch",
                "webhook admission is temporarily unavailable",
            )
        if existing.mapper_version != definition.mapper_version:
            raise _service_error(
                "webhook_mapper_version_mismatch",
                "webhook admission is temporarily unavailable",
            )
        return existing.trigger_id == definition.trigger_id and hmac.compare_digest(
            existing.body_digest,
            body_digest.value,
        )

    @staticmethod
    def _require_insert_outcome(
        candidate: WebhookReceipt,
        stored: WebhookReceipt,
        receipts: tuple[WorkRunReceiptResult, ...],
        inserted: bool,
    ) -> None:
        if type(stored) is not WebhookReceipt or type(inserted) is not bool:
            raise _service_error(
                "webhook_receipt_invalid",
                "webhook receipt state is invalid",
            )
        expected_disposition = (
            WorkRunReceiptDisposition.CREATED if inserted else WorkRunReceiptDisposition.REPLAYED
        )
        if any(item.disposition is not expected_disposition for item in receipts):
            raise _service_error(
                "webhook_receipt_mixed",
                "webhook receipt state is incomplete",
            )
        if inserted:
            if stored != candidate:
                raise _service_error(
                    "webhook_receipt_invalid",
                    "webhook receipt state is invalid",
                )
            return
        if (
            stored.source != candidate.source
            or stored.event_id != candidate.event_id
            or stored.trigger_id != candidate.trigger_id
            or stored.body_digest != candidate.body_digest
            or stored.digest_key_version != candidate.digest_key_version
            or stored.mapper_version != candidate.mapper_version
            or stored.deliveries != candidate.deliveries
        ):
            raise _service_error(
                "webhook_receipt_conflict",
                "webhook receipt state conflicts with admitted work",
            )

    async def _audit_signature_rejected(
        self,
        command: WebhookAdmissionCommand,
        *,
        attempt_id: str,
        now: datetime,
    ) -> None:
        async with self._dependencies.unit_of_work() as unit_of_work:
            event = AuditEventFactory(
                AuditContext.system(
                    "webhook-authenticator",
                    correlation_id=command.correlation_id,
                )
            ).webhook_signature_rejected(
                source=command.source,
                trigger_id=command.trigger_id,
                webhook_attempt_id=attempt_id,
                occurred_at=now,
            )
            await unit_of_work.audits.append_global(event)
            await unit_of_work.commit()

    async def _audit_authenticated_rate_denial(
        self,
        command: WebhookAdmissionCommand,
        *,
        audit_context: AuditContext,
        attempt_id: str,
        retry_after_seconds: int,
        now: datetime,
    ) -> None:
        """Persist authentication and bounded throttle evidence before returning 429."""

        async with self._dependencies.unit_of_work() as unit_of_work:
            factory = AuditEventFactory(audit_context)
            await unit_of_work.audits.append_global_many(
                (
                    factory.webhook_signature_validated(
                        source=command.source,
                        trigger_id=command.trigger_id,
                        webhook_attempt_id=attempt_id,
                        occurred_at=now,
                    ),
                    factory.webhook_rate_limited(
                        source=command.source,
                        trigger_id=command.trigger_id,
                        webhook_attempt_id=attempt_id,
                        retry_after_seconds=retry_after_seconds,
                        occurred_at=now,
                    ),
                )
            )
            await unit_of_work.commit()

    async def _audit_schema_rejected(
        self,
        command: WebhookAdmissionCommand,
        *,
        audit_context: AuditContext,
        attempt_id: str,
        now: datetime,
    ) -> None:
        async with self._dependencies.unit_of_work() as unit_of_work:
            factory = AuditEventFactory(audit_context)
            await unit_of_work.audits.append_global_many(
                (
                    factory.webhook_signature_validated(
                        source=command.source,
                        trigger_id=command.trigger_id,
                        webhook_attempt_id=attempt_id,
                        occurred_at=now,
                    ),
                    factory.webhook_schema_rejected(
                        source=command.source,
                        trigger_id=command.trigger_id,
                        webhook_attempt_id=attempt_id,
                        occurred_at=now,
                    ),
                )
            )
            await unit_of_work.commit()

    async def _audit_schema_rejected_or_unavailable(
        self,
        command: WebhookAdmissionCommand,
        *,
        audit_context: AuditContext,
        attempt_id: str,
        now: datetime,
    ) -> None:
        try:
            await self._audit_schema_rejected(
                command,
                audit_context=audit_context,
                attempt_id=attempt_id,
                now=now,
            )
        except Exception:
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            ) from None

    @staticmethod
    def _require_command(command: WebhookAdmissionCommand) -> None:
        if type(command) is not WebhookAdmissionCommand:
            raise _service_error(
                "webhook_command_invalid",
                "webhook admission command is invalid",
            )
        command.__post_init__()

    @staticmethod
    def _require_identity(
        identity: VerifiedWebhookIdentity,
        command: WebhookAdmissionCommand,
    ) -> None:
        if type(identity) is not VerifiedWebhookIdentity:
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            )
        principal = identity.principal
        required_scopes = {
            "webhook:submit",
            f"webhook:source:{command.source}",
            f"webhook:trigger:{command.trigger_id}",
        }
        try:
            identity.verify_integrity()
            principal.verify_integrity()
        except (TypeError, ValueError):
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            ) from None
        if (
            identity.source != command.source
            or identity.trigger_id != command.trigger_id
            or type(principal) is not AuthenticatedPrincipal
            or principal.kind is not PrincipalKind.SERVICE
            or principal.authentication_method is not AuthenticationMethod.VERIFIED_WEBHOOK
            or principal.roles != frozenset({"webhook_service"})
            or principal.scopes != frozenset(required_scopes)
        ):
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            )

    def _new_id(self, namespace: str) -> str:
        try:
            value = self._dependencies.new_id(namespace)
            require_id(value, f"generated {namespace} ID")
            return value
        except (TypeError, ValueError):
            raise _service_error(
                "webhook_service_unavailable",
                "webhook admission is temporarily unavailable",
            ) from None


__all__ = [
    "WebhookAdmissionCommand",
    "WebhookAdmissionDisposition",
    "WebhookAdmissionResult",
    "WebhookAdmissionService",
    "WebhookAdmissionServiceError",
]
