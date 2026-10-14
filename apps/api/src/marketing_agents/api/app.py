"""FastAPI application factory without import-time infrastructure side effects."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from marketing_agents import __version__
from marketing_agents.api.catalog_queries import (
    CatalogQueryExecutor,
    LocalCatalogQueryService,
)
from marketing_agents.api.csrf import ProcessLocalCsrfToken
from marketing_agents.api.dependencies import (
    ApprovalDecisionExecutor,
    ApprovalResourceExecutor,
    ArtifactResourceExecutor,
    AuditResourceExecutor,
    DemoScenarioRegistryExecutor,
    InstanceConfigurationExecutor,
    ManualDryRunExecutor,
    RunCancellationExecutor,
    RunResourceExecutor,
    WebhookAdmissionExecutor,
)
from marketing_agents.api.errors import (
    safe_detail_expired,
    safe_http_exception,
    safe_request_validation_error,
    safe_unhandled_exception,
)
from marketing_agents.api.middleware import Api09TransportSecurityMiddleware
from marketing_agents.api.openapi import install_problem_openapi
from marketing_agents.api.routes.approvals import (
    ApprovalPrivateResponseMiddleware,
)
from marketing_agents.api.routes.approvals import action_router as approval_actions_router
from marketing_agents.api.routes.approvals import router as approvals_router
from marketing_agents.api.routes.artifacts import router as artifacts_router
from marketing_agents.api.routes.artifacts import run_router as run_artifacts_router
from marketing_agents.api.routes.audit_events import router as audit_events_router
from marketing_agents.api.routes.campaign_briefs import (
    CampaignBriefExecutor,
    CampaignBriefRequestBoundsMiddleware,
)
from marketing_agents.api.routes.campaign_briefs import router as campaign_briefs_router
from marketing_agents.api.routes.catalog import router as catalog_router
from marketing_agents.api.routes.demo_scenarios import (
    DemoScenarioRequestBoundsMiddleware,
)
from marketing_agents.api.routes.demo_scenarios import router as demo_scenarios_router
from marketing_agents.api.routes.health import router as health_router
from marketing_agents.api.routes.instance_configuration import (
    InstanceConfigurationRequestBoundsMiddleware,
)
from marketing_agents.api.routes.instance_configuration import (
    router as instance_configuration_router,
)
from marketing_agents.api.routes.manual_work import ManualWorkRequestBoundsMiddleware
from marketing_agents.api.routes.manual_work import router as manual_work_router
from marketing_agents.api.routes.retained_details import RetainedResourceExecutor
from marketing_agents.api.routes.retained_details import router as retained_details_router
from marketing_agents.api.routes.run_cancellation import RunCancellationRequestBoundsMiddleware
from marketing_agents.api.routes.run_cancellation import router as run_cancellation_router
from marketing_agents.api.routes.runs import (
    Api07PrivateResponseMiddleware,
    instance_status_router,
)
from marketing_agents.api.routes.runs import external_action_router as runtime_actions_router
from marketing_agents.api.routes.runs import router as runs_router
from marketing_agents.api.routes.schedules import (
    ScheduleControlExecutor,
    ScheduleControlRequestBoundsMiddleware,
)
from marketing_agents.api.routes.schedules import router as schedules_router
from marketing_agents.api.routes.session import router as session_router
from marketing_agents.api.routes.webhooks import WebhookRequestBoundsMiddleware
from marketing_agents.api.routes.webhooks import router as webhooks_router
from marketing_agents.api.schemas.problems import ProblemDetails
from marketing_agents.api.telemetry import LocalTelemetryMiddleware
from marketing_agents.application.ports.identity import IdentityProvider
from marketing_agents.application.ports.readiness import ReadinessProbe
from marketing_agents.config import Settings, get_settings
from marketing_agents.demos import build_demo_scenario_registry
from marketing_agents.domain.retained_details import DetailExpired
from marketing_agents.infrastructure.adapters.identity import LocalIdentityProvider
from marketing_agents.infrastructure.readiness import LocalReadinessProbe
from marketing_agents.observability import LocalTelemetry


def create_app(
    settings: Settings | None = None,
    identity_provider: IdentityProvider | None = None,
    approval_decision_service: ApprovalDecisionExecutor | None = None,
    approval_resource_service: ApprovalResourceExecutor | None = None,
    readiness_probe: ReadinessProbe | None = None,
    catalog_query_service: CatalogQueryExecutor | None = None,
    instance_configuration_service: InstanceConfigurationExecutor | None = None,
    manual_dry_run_service: ManualDryRunExecutor | None = None,
    webhook_admission_service: WebhookAdmissionExecutor | None = None,
    run_resource_service: RunResourceExecutor | None = None,
    artifact_resource_service: ArtifactResourceExecutor | None = None,
    audit_resource_service: AuditResourceExecutor | None = None,
    demo_scenario_registry: DemoScenarioRegistryExecutor | None = None,
    run_cancellation_service: RunCancellationExecutor | None = None,
    schedule_control_service: ScheduleControlExecutor | None = None,
    campaign_brief_service: CampaignBriefExecutor | None = None,
    telemetry: LocalTelemetry | None = None,
    retained_resource_service: RetainedResourceExecutor | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    application = FastAPI(
        title="Marketing Agents API",
        version=__version__,
        docs_url="/docs" if active_settings.app_env != "production" else None,
        redoc_url=None,
        responses={
            "default": {
                "model": ProblemDetails,
                "description": "A safe process-wide API problem occurrence.",
            }
        },
    )
    application.state.settings = active_settings
    application.state.telemetry = telemetry if telemetry is not None else LocalTelemetry()
    csrf_token = ProcessLocalCsrfToken()
    application.state.csrf_token = csrf_token
    application.state.identity_provider = (
        identity_provider
        if identity_provider is not None
        else LocalIdentityProvider(active_settings)
    )
    application.state.approval_decision_service = approval_decision_service
    application.state.approval_resource_service = approval_resource_service
    application.state.instance_configuration_service = instance_configuration_service
    application.state.manual_dry_run_service = manual_dry_run_service
    application.state.demo_scenario_registry = (
        demo_scenario_registry
        if demo_scenario_registry is not None
        else build_demo_scenario_registry()
    )
    application.state.webhook_admission_service = webhook_admission_service
    application.state.run_resource_service = run_resource_service
    application.state.run_cancellation_service = run_cancellation_service
    application.state.artifact_resource_service = artifact_resource_service
    application.state.audit_resource_service = audit_resource_service
    application.state.schedule_control_service = schedule_control_service
    application.state.campaign_brief_service = campaign_brief_service
    application.state.retained_resource_service = retained_resource_service
    application.state.catalog_query_service = (
        catalog_query_service
        if catalog_query_service is not None
        else LocalCatalogQueryService(
            active_settings.catalog_root,
            configuration_reader=instance_configuration_service,
        )
    )
    application.state.readiness_probe = (
        readiness_probe if readiness_probe is not None else LocalReadinessProbe(active_settings)
    )
    application.add_exception_handler(
        RequestValidationError,
        safe_request_validation_error,
    )
    application.add_exception_handler(StarletteHTTPException, safe_http_exception)
    application.add_exception_handler(Exception, safe_unhandled_exception)
    application.add_exception_handler(DetailExpired, safe_detail_expired)
    application.add_middleware(ManualWorkRequestBoundsMiddleware)
    application.add_middleware(RunCancellationRequestBoundsMiddleware)
    application.add_middleware(DemoScenarioRequestBoundsMiddleware)
    application.add_middleware(InstanceConfigurationRequestBoundsMiddleware)
    application.add_middleware(ScheduleControlRequestBoundsMiddleware)
    application.add_middleware(CampaignBriefRequestBoundsMiddleware)
    application.add_middleware(WebhookRequestBoundsMiddleware)
    application.add_middleware(ApprovalPrivateResponseMiddleware)
    application.add_middleware(Api07PrivateResponseMiddleware)
    application.add_middleware(
        Api09TransportSecurityMiddleware,
        settings=active_settings,
        csrf_token=csrf_token,
    )
    application.add_middleware(LocalTelemetryMiddleware, telemetry=application.state.telemetry)
    application.include_router(health_router)
    application.include_router(session_router)
    application.include_router(instance_configuration_router)
    application.include_router(schedules_router)
    application.include_router(campaign_briefs_router)
    application.include_router(retained_details_router)
    application.include_router(manual_work_router)
    application.include_router(demo_scenarios_router)
    application.include_router(webhooks_router)
    application.include_router(instance_status_router)
    application.include_router(catalog_router)
    application.include_router(runs_router)
    application.include_router(run_cancellation_router)
    application.include_router(run_artifacts_router)
    application.include_router(artifacts_router)
    application.include_router(audit_events_router)
    application.include_router(runtime_actions_router)
    application.include_router(approvals_router)
    application.include_router(approval_actions_router)
    install_problem_openapi(application)
    return application
