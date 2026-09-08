"""Executable composition root over an existing paired local installation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI

from marketing_agents.api.app import create_app
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.ports.webhook_sources import WebhookSourceDefinition
from marketing_agents.application.ports.webhooks import WebhookVerifierConfig
from marketing_agents.application.services.approval_decisions import ApprovalDecisionService
from marketing_agents.application.services.approval_resources import ApprovalResourceService
from marketing_agents.application.services.artifact_resources import ArtifactResourceService
from marketing_agents.application.services.audit_resources import AuditResourceService
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationService,
)
from marketing_agents.application.services.manual_work_intake import ManualDryRunService
from marketing_agents.application.services.run_resources import RunResourceService
from marketing_agents.application.services.webhook_intake import WebhookAdmissionService
from marketing_agents.config import Settings
from marketing_agents.demos import DEMO_SCENARIOS, DemoRunService, build_demo_read_adapter
from marketing_agents.demos.email_signup_service import EmailSignupRunService
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.db import (
    DatabaseRuntime,
    InstanceConfigurationSQLAlchemyUnitOfWorkFactory,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyConnectorReceiptRepository,
    SQLAlchemyExecutionControlRepository,
    SQLAlchemyExternalActionRepository,
    SQLAlchemyInstanceConfigurationRepository,
    SQLAlchemyManualAdmissionUnitOfWorkFactory,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyScheduleRepository,
    SQLAlchemyWebhookReceiptRepository,
    SQLAlchemyWorkRepository,
    create_database_runtime,
)
from marketing_agents.infrastructure.instance_configuration_constraints import (
    CompiledCatalogInstanceConfigurationConstraintProvider,
    LocalMockRegisteredBindingProvider,
)
from marketing_agents.infrastructure.manual_work import CompiledCatalogManualAdmissionResolver
from marketing_agents.infrastructure.readiness import LocalReadinessProbe
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.infrastructure.webhook_ingress import CompiledCatalogWebhookAdmissionResolver
from marketing_agents.infrastructure.webhook_signatures import (
    EnvironmentWebhookSecretResolver,
    HmacSha256WebhookSignatureVerifier,
)
from marketing_agents.infrastructure.webhook_sources import (
    StaticWebhookSourceRegistry,
    StrictJsonWebhookEnvelopeMapper,
)
from marketing_agents.security.digest_key import DigestKey, load_or_create_digest_key


class RuntimeNotReady(RuntimeError):
    """Stable payload-safe process startup failure."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RandomIds:
    def new(self, namespace: str) -> str:
        return f"{namespace}.{uuid4().hex}"


@dataclass(slots=True)
class LocalRuntime:
    settings: Settings
    database: DatabaseRuntime
    catalog: CompiledCatalog
    dependencies: OrchestrationDependencies
    digest_key: DigestKey
    manual: ManualDryRunService
    demos: DemoRunService
    email: EmailSignupRunService
    configurations: InstanceConfigurationService
    webhook: WebhookAdmissionService

    def create_app(self) -> FastAPI:
        dependencies = self.dependencies
        return create_app(
            self.settings,
            manual_dry_run_service=self.manual,
            instance_configuration_service=self.configurations,
            approval_decision_service=ApprovalDecisionService(dependencies),
            approval_resource_service=ApprovalResourceService(dependencies),
            run_resource_service=RunResourceService(
                dependencies.unit_of_work_factory,
                catalog_instance_ids=tuple(item.id for item in self.catalog.instances),
                utc_now=dependencies.utc_now,
            ),
            artifact_resource_service=ArtifactResourceService(
                dependencies.unit_of_work_factory, digest_key=self.digest_key
            ),
            audit_resource_service=AuditResourceService(
                dependencies.unit_of_work_factory, utc_now=dependencies.utc_now
            ),
            webhook_admission_service=self.webhook,
            readiness_probe=LocalReadinessProbe(self.settings),
        )

    async def close(self) -> None:
        await self.database.dispose()


async def build_runtime(settings: Settings, *, clock: Clock | None = None) -> LocalRuntime:
    """Verify schema/catalog/key before opening writable application services."""
    if (
        settings.llm_provider != "mock"
        or settings.connector_mode != "mock"
        or settings.allow_external_network
        or settings.auth_mode != "local"
        or settings.app_env == "production"
    ):
        raise RuntimeNotReady("runtime_requires_mock_offline_local_modes")
    report = await LocalReadinessProbe(settings).check()
    if not report.ready:
        codes = sorted({item.code.value for item in report.checks if item.code.value != "ready"})
        raise RuntimeNotReady("runtime_not_ready:" + ",".join(codes))
    key = load_or_create_digest_key(
        settings.marketing_agents_digest_key_path, persistent_state_exists=True
    )
    catalog = compile_catalog(settings.catalog_root)
    database = create_database_runtime(settings.database_url)
    try:
        factories = SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            configurations=SQLAlchemyInstanceConfigurationRepository,
            approvals=lambda session: SQLAlchemyApprovalRepository(session, key),
            run_steps=SQLAlchemyRunStepRepository,
            external_actions=SQLAlchemyExternalActionRepository,
            connector_receipts=SQLAlchemyConnectorReceiptRepository,
            execution_control=lambda session: SQLAlchemyExecutionControlRepository(session, key),
            artifacts=SQLAlchemyArtifactRepository,
            schedules=SQLAlchemyScheduleRepository,
            webhook_receipts=SQLAlchemyWebhookReceiptRepository,
        )
        dependencies = OrchestrationDependencies(
            clock or SystemClock(),
            RandomIds(),
            SQLAlchemyManualAdmissionUnitOfWorkFactory(database.session_factory, factories),
        )
        manual = ManualDryRunService(
            dependencies,
            key,
            CompiledCatalogManualAdmissionResolver(
                catalog, mock_connectors_active=True, demo_scenarios=DEMO_SCENARIOS
            ),
            current_catalog_hash=catalog.content_hash,
        )
        adapter = build_demo_read_adapter(catalog)
        configurations = InstanceConfigurationService(
            unit_of_work_factory=InstanceConfigurationSQLAlchemyUnitOfWorkFactory(
                database.session_factory
            ),
            constraints=CompiledCatalogInstanceConfigurationConstraintProvider(catalog),
            registered_bindings=LocalMockRegisteredBindingProvider(),
            recurrence=CroniterRecurrenceCalculator(),
            clock=dependencies.clock,
            audit_pseudonym_key=key,
        )
        definitions: list[WebhookSourceDefinition] = []
        if settings.webhook_hmac_secret is not None:
            bindings: set[tuple[str, str]] = set()
            async with dependencies.unit_of_work() as unit_of_work:
                for instance in catalog.instances:
                    configuration = await unit_of_work.configurations.get(instance.id)
                    if configuration is not None and configuration.enabled:
                        bindings.update(
                            (binding.event_source, f"trigger.webhook.{binding.event_source}.v1")
                            for binding in configuration.trigger_bindings
                            if binding.kind is TriggerKind.WEBHOOK
                            and binding.enabled
                            and binding.event_source is not None
                        )
            # One configured secret grants one source authority. Fan-out may target
            # multiple instances, but must retain that exact source/trigger pair.
            if len(bindings) > 1:
                raise RuntimeNotReady("runtime_webhook_secret_requires_one_source")
            for source, trigger_id in sorted(bindings):
                mapper = StrictJsonWebhookEnvelopeMapper()
                definitions.append(
                    WebhookSourceDefinition(
                        source=source,
                        trigger_id=trigger_id,
                        mapper_version=mapper.version,
                        signature_verifier=HmacSha256WebhookSignatureVerifier(
                            EnvironmentWebhookSecretResolver(
                                {
                                    "LOCAL_WEBHOOK_SECRET": (
                                        settings.webhook_hmac_secret.get_secret_value()
                                    )
                                }
                            )
                        ),
                        verifier_config=WebhookVerifierConfig(
                            secret_reference="env:LOCAL_WEBHOOK_SECRET"
                        ),
                        mapper=mapper,
                    )
                )
        webhook = WebhookAdmissionService(
            dependencies,
            key,
            StaticWebhookSourceRegistry(tuple(definitions)),
            CompiledCatalogWebhookAdmissionResolver(catalog, mock_connectors_active=True),
            current_catalog_hash=catalog.content_hash,
        )
        return LocalRuntime(
            settings,
            database,
            catalog,
            dependencies,
            key,
            manual,
            DemoRunService(dependencies, manual, catalog, adapter),
            EmailSignupRunService(dependencies, manual, catalog, adapter),
            configurations,
            webhook,
        )
    except BaseException:
        await database.dispose()
        raise
