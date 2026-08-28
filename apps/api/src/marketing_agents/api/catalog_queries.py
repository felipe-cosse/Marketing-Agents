"""Read-only catalog query composition and safe public projection."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from marketing_agents.api.instance_configuration_etag import instance_configuration_etag
from marketing_agents.api.schemas.catalog import (
    AgentInstanceDetailResponse,
    AgentInstanceListResponse,
    AgentInstanceRuntimeDetailResponse,
    AgentInstanceView,
    AgentTemplateDetailResponse,
    AgentTemplateListResponse,
    AgentTemplateView,
    ApprovalPolicyListResponse,
    ApprovalPolicyView,
    BudgetPolicyView,
    CapabilitySummaryView,
    CatalogApiModel,
    CatalogCounts,
    CatalogHierarchyResponse,
    CatalogManifestView,
    CatalogResponse,
    ConnectorBindingView,
    DepartmentCountView,
    DepartmentView,
    FunctionView,
    HierarchyCounts,
    HierarchyDepartmentView,
    HierarchyFunctionView,
    HierarchyInstanceView,
    InstanceRecentRunView,
    InstanceRuntimeStatusView,
    RateLimitPolicyView,
    RetryPolicyView,
    ScheduleBindingView,
    TimeoutPolicyView,
    ToolCapabilityListResponse,
    ToolCapabilityView,
    TriggerBindingView,
)
from marketing_agents.application.policies.catalog_authorization import (
    authorize_catalog_reader,
)
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationSnapshot,
)
from marketing_agents.application.services.run_resources import (
    InstanceRuntimeStatus,
    InstanceStatusSummary,
    RunResource,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import InstanceConfiguration
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import (
    AgentInstanceRecord,
    AgentTemplateRecord,
    ApprovalPolicyRecord,
    CompiledCatalog,
    ConnectorBinding,
    DepartmentRecord,
    FunctionRecord,
    ScheduleBinding,
    ToolCapabilityRecord,
    TriggerBinding,
)
from marketing_agents.infrastructure.instance_configuration_constraints import (
    InstanceConfigurationConstraintError,
    validate_mock_connector_bindings,
)

_ETAG_DOMAIN = b"marketing-agents:catalog-api-etag:v1\x00"
_REPRESENTATION_SEAL = object()
_LABEL_INITIALISMS = MappingProxyType(
    {
        "api": "API",
        "cms": "CMS",
        "crm": "CRM",
        "llm": "LLM",
        "seo": "SEO",
    }
)


class CatalogQueryUnavailable(RuntimeError):
    """Raised without source diagnostics when no complete safe projection exists."""


def _representation_etag(label: str, content: bytes) -> str:
    digest = hashlib.sha256(_ETAG_DOMAIN + label.encode("utf-8") + b"\x00" + content).hexdigest()
    return f'"{digest}"'


@dataclass(frozen=True, slots=True, init=False)
class CatalogRepresentation:
    label: str
    model_type: type[CatalogApiModel]
    content: bytes
    etag: str

    def __init__(
        self,
        *,
        label: str,
        model_type: type[CatalogApiModel],
        content: bytes,
        etag: str,
        _seal: object,
    ) -> None:
        if _seal is not _REPRESENTATION_SEAL:
            raise ValueError("catalog representations must be issued by the projector")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "model_type", model_type)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "etag", etag)
        if not self.is_valid_for(model_type, label):
            raise ValueError("catalog representation integrity is invalid")

    def is_valid_for(
        self,
        expected_type: type[CatalogApiModel],
        expected_label: str,
    ) -> bool:
        return (
            type(self.label) is str
            and 0 < len(self.label) <= 240
            and self.label == expected_label
            and self.model_type is expected_type
            and type(self.content) is bytes
            and type(self.etag) is str
            and self.etag == _representation_etag(self.label, self.content)
        )


@dataclass(frozen=True, slots=True)
class CatalogDocuments:
    catalog: CatalogRepresentation
    hierarchy: CatalogRepresentation
    templates: CatalogRepresentation
    tool_capabilities: CatalogRepresentation
    approval_policies: CatalogRepresentation
    instances: CatalogRepresentation
    template_details: Mapping[str, CatalogRepresentation]
    instance_details: Mapping[str, CatalogRepresentation]


class CatalogQueryExecutor(Protocol):
    async def read(self, principal: AuthenticatedPrincipal) -> CatalogDocuments: ...


class InstanceConfigurationSnapshotReader(Protocol):
    async def read_all(
        self,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationSnapshot: ...


class LocalCatalogQueryService:
    """Compile the local source off-loop and return an immutable safe projection."""

    def __init__(
        self,
        catalog_root: Path,
        *,
        compiler: Callable[[Path], CompiledCatalog] = compile_catalog,
        configuration_reader: InstanceConfigurationSnapshotReader | None = None,
    ) -> None:
        self._catalog_root = Path(catalog_root)
        self._compiler = compiler
        self._configuration_reader = configuration_reader
        self._documents: CatalogDocuments | None = None
        self._load_task: asyncio.Task[CatalogDocuments] | None = None
        self._compiled: CompiledCatalog | None = None
        self._compile_task: asyncio.Task[CompiledCatalog] | None = None
        self._load_lock = asyncio.Lock()

    async def read(self, principal: AuthenticatedPrincipal) -> CatalogDocuments:
        authorize_catalog_reader(principal)
        if self._configuration_reader is not None:
            return await self._read_effective(principal)
        if self._documents is not None:
            return self._documents
        async with self._load_lock:
            if self._documents is not None:
                return self._documents
            task = self._load_task
            if task is None:
                task = asyncio.create_task(asyncio.to_thread(self._compile_and_project))
                self._load_task = task
        try:
            documents = await asyncio.shield(task)
            if type(documents) is not CatalogDocuments:
                raise TypeError("catalog projection returned the wrong boundary type")
        except Exception:
            async with self._load_lock:
                if self._load_task is task:
                    self._load_task = None
            raise CatalogQueryUnavailable("catalog projection is unavailable") from None
        async with self._load_lock:
            if self._documents is None:
                self._documents = documents
            if self._load_task is task:
                self._load_task = None
            return self._documents

    async def _read_effective(
        self,
        principal: AuthenticatedPrincipal,
    ) -> CatalogDocuments:
        compiled = await self._compiled_catalog()
        try:
            reader = self._configuration_reader
            if reader is None:
                raise TypeError("configuration reader is unavailable")
            snapshot = await reader.read_all(principal=principal)
            if type(snapshot) is not InstanceConfigurationSnapshot:
                raise TypeError("configuration reader returned the wrong boundary type")
            configurations = {
                configuration.instance_id: configuration
                for configuration in snapshot.configurations
            }
            if len(configurations) != len(snapshot.configurations):
                raise TypeError("configuration snapshot contains duplicate instances")
            documents = await asyncio.to_thread(
                project_catalog,
                compiled,
                configurations,
            )
            if type(documents) is not CatalogDocuments:
                raise TypeError("catalog projection returned the wrong boundary type")
            return documents
        except Exception:
            raise CatalogQueryUnavailable("catalog projection is unavailable") from None

    async def _compiled_catalog(self) -> CompiledCatalog:
        if self._compiled is not None:
            return self._compiled
        async with self._load_lock:
            if self._compiled is not None:
                return self._compiled
            task = self._compile_task
            if task is None:
                task = asyncio.create_task(asyncio.to_thread(self._compiler, self._catalog_root))
                self._compile_task = task
        try:
            compiled = await asyncio.shield(task)
            if type(compiled) is not CompiledCatalog:
                raise TypeError("catalog compiler returned the wrong boundary type")
        except Exception:
            async with self._load_lock:
                if self._compile_task is task:
                    self._compile_task = None
            raise CatalogQueryUnavailable("catalog projection is unavailable") from None
        async with self._load_lock:
            if self._compiled is None:
                self._compiled = compiled
            if self._compile_task is task:
                self._compile_task = None
            return self._compiled

    def _compile_and_project(self) -> CatalogDocuments:
        compiled = self._compiler(self._catalog_root)
        if type(compiled) is not CompiledCatalog:
            raise TypeError("catalog compiler returned the wrong boundary type")
        return project_catalog(compiled)


def _index_by_id[RecordT](
    records: Sequence[RecordT],
    *,
    kind: str,
) -> dict[str, RecordT]:
    result: dict[str, RecordT] = {}
    for record in records:
        identifier = getattr(record, "id", None)
        if type(identifier) is not str or not identifier or identifier in result:
            raise CatalogQueryUnavailable(f"invalid {kind} identity")
        result[identifier] = record
    return result


def _capability_display_name(identifier: str) -> str:
    """Derive a neutral v1 label until the source catalog owns one explicitly."""

    parts = identifier.split(".")
    if len(parts) != 3 or parts[0] != "cap":
        raise CatalogQueryUnavailable("invalid capability identity")
    family = " ".join(_LABEL_INITIALISMS.get(word, word.title()) for word in parts[1].split("-"))
    raw_operation_words = parts[2].split("-")
    operation_words = [_LABEL_INITIALISMS.get(word, word) for word in raw_operation_words]
    operation_words[0] = _LABEL_INITIALISMS.get(
        raw_operation_words[0], raw_operation_words[0].title()
    )
    return f"{family}: {' '.join(operation_words)}"


def _capability_view(record: ToolCapabilityRecord) -> ToolCapabilityView:
    return ToolCapabilityView(
        id=record.id,
        display_name=_capability_display_name(record.id),
        description=record.description,
        effect=record.effect,
        connector_family=record.connector_family,
        idempotency_support=record.idempotency_support,
        default_timeout_seconds=record.default_timeout_seconds,
        data_classification=record.data_classification,
    )


def _approval_policy_view(record: ApprovalPolicyRecord) -> ApprovalPolicyView:
    return ApprovalPolicyView(
        id=record.id,
        kind=record.kind,
        required_roles=record.required_roles,
        expiry_seconds=record.expiry_seconds,
        allow_self_approval=record.allow_self_approval,
    )


def _template_view(record: AgentTemplateRecord) -> AgentTemplateView:
    return AgentTemplateView(
        id=record.id,
        display_name=record.display_name,
        department_id=record.department_id,
        function_id=record.function_id,
        display_order=record.display_order,
        purpose=record.purpose,
        input_schema_id=record.input_schema_id,
        output_schema_id=record.output_schema_id,
        allowed_tool_capability_ids=record.allowed_tool_capability_ids,
        supported_trigger_types=record.supported_trigger_types,
        operation_classification=record.operation_classification,
        output_handling=record.output_handling,
        approval_policy_id=record.approval_policy_id,
        retry_policy=RetryPolicyView(
            max_attempts=record.retry_policy.max_attempts,
            backoff=record.retry_policy.backoff,
        ),
        timeout_policy=TimeoutPolicyView(
            step_seconds=record.timeout_policy.step_seconds,
            run_seconds=record.timeout_policy.run_seconds,
        ),
        budget_policy=BudgetPolicyView(
            max_steps=record.budget_policy.max_steps,
            max_model_calls=record.budget_policy.max_model_calls,
            max_tool_calls=record.budget_policy.max_tool_calls,
            max_input_bytes=record.budget_policy.max_input_bytes,
            max_input_field_bytes=record.budget_policy.max_input_field_bytes,
            max_output_bytes=record.budget_policy.max_output_bytes,
            max_model_output_tokens=record.budget_policy.max_model_output_tokens,
        ),
        rate_limit_policy=RateLimitPolicyView(
            max_calls=record.rate_limit_policy.max_calls,
            window_seconds=record.rate_limit_policy.window_seconds,
        ),
        source_confidence=record.source_confidence,
        source_references=record.source_references,
        implementation_notes=record.implementation_notes,
    )


def _trigger_view(record: TriggerBinding) -> TriggerBindingView:
    return TriggerBindingView(
        type=record.type,
        enabled=record.enabled,
        event_source=record.event_source,
        cron=record.cron,
        timezone=record.timezone,
        misfire_policy=record.misfire_policy,
        misfire_grace_seconds=record.misfire_grace_seconds,
    )


def _connector_view(record: ConnectorBinding) -> ConnectorBindingView:
    return ConnectorBindingView(
        connector_family=record.connector_family,
        binding_id=record.binding_id,
        enabled=record.enabled,
    )


def _schedule_view(record: ScheduleBinding | None) -> ScheduleBindingView | None:
    if record is None:
        return None
    return ScheduleBindingView(
        cron=record.cron,
        timezone=record.timezone,
        misfire_policy=record.misfire_policy,
        misfire_grace_seconds=record.misfire_grace_seconds,
    )


def _instance_view(
    record: AgentInstanceRecord,
    configuration: InstanceConfiguration | None = None,
) -> AgentInstanceView:
    if record.variant is None:
        raise CatalogQueryUnavailable("instance source ordinal is unavailable")
    if configuration is not None and configuration.instance_id != record.id:
        raise CatalogQueryUnavailable("effective instance configuration identity is invalid")
    return AgentInstanceView(
        id=record.id,
        template_id=record.template_id,
        display_order=record.display_order,
        enabled=record.enabled if configuration is None else configuration.enabled,
        source_ordinal=record.variant.source_ordinal,
        variant_label=(
            record.variant.variant_label if configuration is None else configuration.variant_label
        ),
        trigger_bindings=(
            tuple(_trigger_view(item) for item in record.trigger_bindings)
            if configuration is None
            else tuple(
                TriggerBindingView(
                    type=item.kind.value,
                    enabled=item.enabled,
                    event_source=item.event_source,
                    cron=item.cron,
                    timezone=item.timezone,
                    misfire_policy=(
                        None if item.misfire_policy is None else item.misfire_policy.value
                    ),
                    misfire_grace_seconds=item.misfire_grace_seconds,
                )
                for item in configuration.trigger_bindings
            )
        ),
        connector_bindings=(
            {
                key: _connector_view(record.connector_bindings[key])
                for key in sorted(record.connector_bindings)
            }
            if configuration is None
            else {
                family: ConnectorBindingView(
                    connector_family=binding.connector_family,
                    binding_id=binding.binding_id,
                    enabled=binding.enabled,
                )
                for family, binding in configuration.connector_bindings.items()
            }
        ),
        schedule=(
            _schedule_view(record.schedule)
            if configuration is None
            else (
                None
                if configuration.schedule is None
                else ScheduleBindingView(
                    cron=configuration.schedule.cron,
                    timezone=configuration.schedule.timezone,
                    misfire_policy=configuration.schedule.misfire_policy.value,
                    misfire_grace_seconds=configuration.schedule.misfire_grace_seconds,
                )
            )
        ),
        configuration_revision=(
            record.configuration_revision
            if configuration is None
            else configuration.configuration_revision
        ),
        configuration_etag=instance_configuration_etag(
            record.configuration_revision
            if configuration is None
            else configuration.configuration_revision
        ),
    )


def _representation(label: str, payload: CatalogApiModel) -> CatalogRepresentation:
    encoded = json.dumps(
        payload.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CatalogRepresentation(
        label=label,
        model_type=type(payload),
        content=encoded,
        etag=_representation_etag(label, encoded),
        _seal=_REPRESENTATION_SEAL,
    )


def enrich_instance_runtime_representation(
    representation: CatalogRepresentation,
    *,
    instance_id: str,
    status_summary: InstanceStatusSummary,
    recent_runs: tuple[RunResource, ...],
) -> CatalogRepresentation:
    """Merge a bounded runtime overlay without changing any static representation."""

    if (
        type(representation) is not CatalogRepresentation
        or not representation.is_valid_for(
            AgentInstanceDetailResponse,
            f"agent-instance:{instance_id}",
        )
        or type(status_summary) is not InstanceStatusSummary
        or status_summary.scope != "single-local-installation"
        or type(status_summary.items) is not tuple
        or len(status_summary.items) != 1
        or type(status_summary.items[0]) is not InstanceRuntimeStatus
        or status_summary.items[0].instance_id != instance_id
        or type(status_summary.etag) is not str
        or re.fullmatch(
            r'^"instance-status-sha256-v1:[0-9a-f]{64}"$',
            status_summary.etag,
        )
        is None
        or type(recent_runs) is not tuple
        or len(recent_runs) > 5
        or any(
            type(item) is not RunResource or item.instance_id != instance_id for item in recent_runs
        )
        or tuple((item.created_at, item.run_id) for item in recent_runs)
        != tuple(
            sorted(
                ((item.created_at, item.run_id) for item in recent_runs),
                reverse=True,
            )
        )
    ):
        raise CatalogQueryUnavailable("instance runtime projection is unavailable")
    status_item = status_summary.items[0]
    expected_instance_url = f"/api/v1/agent-instances/{instance_id}"
    expected_latest_run_url = (
        None if status_item.latest_run_id is None else f"/api/v1/runs/{status_item.latest_run_id}"
    )
    latest = None if not recent_runs else recent_runs[0]
    if (
        status_item.instance_url != expected_instance_url
        or status_item.latest_run_url != expected_latest_run_url
        or any(item.run_url != f"/api/v1/runs/{item.run_id}" for item in recent_runs)
        or (latest is None and status_item.status != "never_run")
        or (latest is None and status_item.latest_run_id is not None)
        or (
            latest is not None
            and (
                status_item.latest_run_id != latest.run_id
                or status_item.latest_run_state != latest.state
                or status_item.latest_run_created_at != latest.created_at
                or status_item.latest_run_updated_at != latest.updated_at
                or status_item.status != latest.state
            )
        )
    ):
        raise CatalogQueryUnavailable("instance runtime projection is unavailable")
    try:
        static = AgentInstanceDetailResponse.model_validate_json(representation.content)
        enriched = AgentInstanceRuntimeDetailResponse(
            **static.model_dump(),
            runtime_watermark=status_summary.etag[1:-1],
            runtime_status=InstanceRuntimeStatusView(
                status=status_item.status,
                latest_run_id=status_item.latest_run_id,
                latest_run_state=status_item.latest_run_state,
                latest_run_created_at=status_item.latest_run_created_at,
                latest_run_updated_at=status_item.latest_run_updated_at,
                latest_run_url=status_item.latest_run_url,
            ),
            recent_runs=tuple(
                InstanceRecentRunView(
                    id=item.run_id,
                    state=item.state,
                    workflow_id=item.workflow_id,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    run_url=item.run_url,
                )
                for item in recent_runs
            ),
        )
    except (TypeError, ValueError):
        raise CatalogQueryUnavailable("instance runtime projection is unavailable") from None
    return _representation(f"agent-instance-runtime:{instance_id}", enriched)


def _resolved_capabilities(
    template: AgentTemplateRecord,
    capability_by_id: Mapping[str, ToolCapabilityRecord],
) -> tuple[ToolCapabilityRecord, ...]:
    try:
        return tuple(
            capability_by_id[identifier] for identifier in template.allowed_tool_capability_ids
        )
    except KeyError:
        raise CatalogQueryUnavailable("template capability reference is invalid") from None


def _safe_schema(
    schemas: Mapping[str, Mapping[str, object]],
    template_id: str,
) -> dict[str, object]:
    try:
        schema = schemas[template_id]
    except KeyError:
        raise CatalogQueryUnavailable("template schema is unavailable") from None
    copied = copy.deepcopy(dict(schema))
    if type(copied) is not dict:
        raise CatalogQueryUnavailable("template schema is malformed")
    return copied


def _validate_relationships(
    *,
    functions: Sequence[FunctionRecord],
    templates: Sequence[AgentTemplateRecord],
    instances: Sequence[AgentInstanceRecord],
    department_by_id: Mapping[str, DepartmentRecord],
    function_by_id: Mapping[str, FunctionRecord],
    template_by_id: Mapping[str, AgentTemplateRecord],
    capability_by_id: Mapping[str, ToolCapabilityRecord],
    policy_by_id: Mapping[str, ApprovalPolicyRecord],
) -> None:
    for function in functions:
        if function.department_id not in department_by_id:
            raise CatalogQueryUnavailable("function department reference is invalid")
    for template in templates:
        selected_function = function_by_id.get(template.function_id)
        if (
            template.department_id not in department_by_id
            or selected_function is None
            or selected_function.department_id != template.department_id
            or template.approval_policy_id not in policy_by_id
            or any(
                identifier not in capability_by_id
                for identifier in template.allowed_tool_capability_ids
            )
        ):
            raise CatalogQueryUnavailable("template relationship is invalid")
    for instance in instances:
        if instance.template_id not in template_by_id:
            raise CatalogQueryUnavailable("instance template reference is invalid")


def project_catalog(
    compiled: CompiledCatalog,
    configurations: Mapping[str, InstanceConfiguration] | None = None,
) -> CatalogDocuments:
    """Create source-authoritative views with an optional complete deployment overlay."""

    if type(compiled) is not CompiledCatalog:
        raise CatalogQueryUnavailable("catalog source has the wrong boundary type")
    department_by_id = _index_by_id(compiled.departments, kind="department")
    function_by_id = _index_by_id(compiled.functions, kind="function")
    template_by_id = _index_by_id(compiled.templates, kind="template")
    instance_by_id = _index_by_id(compiled.instances, kind="instance")
    capability_by_id = _index_by_id(compiled.tool_capabilities, kind="capability")
    policy_by_id = _index_by_id(compiled.approval_policies, kind="approval policy")
    _validate_relationships(
        functions=compiled.functions,
        templates=compiled.templates,
        instances=compiled.instances,
        department_by_id=department_by_id,
        function_by_id=function_by_id,
        template_by_id=template_by_id,
        capability_by_id=capability_by_id,
        policy_by_id=policy_by_id,
    )

    department_views = tuple(
        DepartmentView(
            id=item.id,
            display_name=item.display_name,
            display_order=item.display_order,
            source_references=item.source_references,
        )
        for item in compiled.departments
    )
    function_views = tuple(
        FunctionView(
            id=item.id,
            department_id=item.department_id,
            display_name=item.display_name,
            display_order=item.display_order,
            source_references=item.source_references,
        )
        for item in compiled.functions
    )
    capability_views = tuple(_capability_view(item) for item in compiled.tool_capabilities)
    capability_view_by_id = {item.id: item for item in capability_views}
    policy_views = tuple(_approval_policy_view(item) for item in compiled.approval_policies)
    policy_view_by_id = {item.id: item for item in policy_views}
    template_views = tuple(_template_view(item) for item in compiled.templates)
    template_view_by_id = {item.id: item for item in template_views}
    configuration_by_id: Mapping[str, InstanceConfiguration]
    if configurations is None:
        configuration_by_id = MappingProxyType({})
    else:
        if not isinstance(configurations, Mapping):
            raise CatalogQueryUnavailable("effective instance configuration is malformed")
        normalized_configurations: dict[str, InstanceConfiguration] = {}
        for instance_id, configuration in configurations.items():
            if (
                type(instance_id) is not str
                or type(configuration) is not InstanceConfiguration
                or configuration.instance_id != instance_id
                or instance_id in normalized_configurations
            ):
                raise CatalogQueryUnavailable("effective instance configuration is malformed")
            normalized_configurations[instance_id] = configuration
        if set(normalized_configurations) != set(instance_by_id):
            raise CatalogQueryUnavailable(
                "effective instance configuration must cover the complete catalog"
            )
        for instance_id, configuration in normalized_configurations.items():
            source_instance = instance_by_id[instance_id]
            template = template_by_id[source_instance.template_id]
            configured_trigger_types = {
                binding.kind.value for binding in configuration.trigger_bindings
            }
            if not configured_trigger_types.issubset(template.supported_trigger_types):
                raise CatalogQueryUnavailable(
                    "effective instance configuration contradicts its template triggers"
                )
            try:
                validate_mock_connector_bindings(
                    compiled,
                    instance_id,
                    configuration.connector_bindings,
                )
            except InstanceConfigurationConstraintError:
                raise CatalogQueryUnavailable(
                    "effective instance configuration contradicts its template connectors"
                ) from None
        configuration_by_id = MappingProxyType(normalized_configurations)
    instance_views = tuple(
        _instance_view(item, configuration_by_id.get(item.id)) for item in compiled.instances
    )
    instance_view_by_id = {item.id: item for item in instance_views}

    department_counts = Counter[str]()
    deployment_counts = Counter[str]()
    for instance in compiled.instances:
        template = template_by_id[instance.template_id]
        department_counts[template.department_id] += 1
        deployment_counts[template.id] += 1
    department_count_views = tuple(
        DepartmentCountView(
            department_id=department.id,
            instance_count=department_counts[department.id],
        )
        for department in compiled.departments
    )
    hierarchy_counts = HierarchyCounts(
        departments=len(compiled.departments),
        functions=len(compiled.functions),
        templates=len(compiled.templates),
        instances=len(compiled.instances),
    )
    counts = CatalogCounts(
        departments=hierarchy_counts.departments,
        functions=hierarchy_counts.functions,
        templates=hierarchy_counts.templates,
        instances=hierarchy_counts.instances,
        tool_capabilities=len(compiled.tool_capabilities),
        approval_policies=len(compiled.approval_policies),
    )

    catalog = CatalogResponse(
        manifest=CatalogManifestView(
            format_version=compiled.manifest.format_version,
            content_version=compiled.manifest.content_version,
            json_schema_dialect=compiled.manifest.json_schema_dialect,
        ),
        catalog_version=compiled.manifest.content_version,
        catalog_hash=compiled.content_hash,
        counts=counts,
        department_counts=department_count_views,
        departments=department_views,
        functions=function_views,
        templates=template_views,
        instances=instance_views,
        tool_capabilities=capability_views,
        approval_policies=policy_views,
    )

    hierarchy_departments: list[HierarchyDepartmentView] = []
    for department in compiled.departments:
        hierarchy_functions: list[HierarchyFunctionView] = []
        for function in (
            item for item in compiled.functions if item.department_id == department.id
        ):
            hierarchy_instances: list[HierarchyInstanceView] = []
            for instance in compiled.instances:
                template = template_by_id[instance.template_id]
                if template.function_id != function.id:
                    continue
                if instance.variant is None:
                    raise CatalogQueryUnavailable("instance source ordinal is unavailable")
                capabilities = _resolved_capabilities(template, capability_by_id)
                hierarchy_instances.append(
                    HierarchyInstanceView(
                        id=instance.id,
                        template_id=template.id,
                        display_name=template.display_name,
                        purpose=template.purpose,
                        display_order=instance.display_order,
                        enabled=instance_view_by_id[instance.id].enabled,
                        operation_classification=template.operation_classification,
                        trigger_types=template.supported_trigger_types,
                        capability_summaries=tuple(
                            CapabilitySummaryView(
                                id=capability.id,
                                display_name=capability_view_by_id[capability.id].display_name,
                                connector_family=capability.connector_family,
                                effect=capability.effect,
                            )
                            for capability in capabilities
                        ),
                        source_ordinal=instance.variant.source_ordinal,
                    )
                )
            hierarchy_functions.append(
                HierarchyFunctionView(
                    id=function.id,
                    display_name=function.display_name,
                    display_order=function.display_order,
                    instances=tuple(hierarchy_instances),
                )
            )
        hierarchy_departments.append(
            HierarchyDepartmentView(
                id=department.id,
                display_name=department.display_name,
                display_order=department.display_order,
                functions=tuple(hierarchy_functions),
            )
        )
    hierarchy = CatalogHierarchyResponse(
        catalog_version=compiled.manifest.content_version,
        catalog_hash=compiled.content_hash,
        counts=hierarchy_counts,
        department_counts=department_count_views,
        departments=tuple(hierarchy_departments),
    )

    template_details: dict[str, CatalogRepresentation] = {}
    for template in compiled.templates:
        capabilities = _resolved_capabilities(template, capability_by_id)
        template_detail = AgentTemplateDetailResponse(
            catalog_version=compiled.manifest.content_version,
            catalog_hash=compiled.content_hash,
            template=template_view_by_id[template.id],
            deployment_count=deployment_counts[template.id],
            capabilities=tuple(capability_view_by_id[item.id] for item in capabilities),
            approval_policy=policy_view_by_id[template.approval_policy_id],
            input_schema=_safe_schema(compiled.input_schema_by_template, template.id),
            output_schema=_safe_schema(compiled.output_schema_by_template, template.id),
            source_references=template.source_references,
            implementation_notes=template.implementation_notes,
        )
        template_details[template.id] = _representation(
            f"agent-template:{template.id}", template_detail
        )

    instance_details: dict[str, CatalogRepresentation] = {}
    for instance in compiled.instances:
        template = template_by_id[instance.template_id]
        capabilities = _resolved_capabilities(template, capability_by_id)
        instance_detail = AgentInstanceDetailResponse(
            catalog_version=compiled.manifest.content_version,
            catalog_hash=compiled.content_hash,
            instance=instance_view_by_id[instance.id],
            template=template_view_by_id[template.id],
            shared_template_deployment_count=deployment_counts[template.id],
            capabilities=tuple(capability_view_by_id[item.id] for item in capabilities),
            approval_policy=policy_view_by_id[template.approval_policy_id],
            input_schema=_safe_schema(compiled.input_schema_by_template, template.id),
            output_schema=_safe_schema(compiled.output_schema_by_template, template.id),
            template_source_references=template.source_references,
            template_implementation_notes=template.implementation_notes,
            configuration_schema=(f"/api/v1/agent-instances/{instance.id}/configuration-schema"),
        )
        instance_details[instance.id] = _representation(
            f"agent-instance:{instance.id}", instance_detail
        )

    templates = AgentTemplateListResponse(
        catalog_version=compiled.manifest.content_version,
        catalog_hash=compiled.content_hash,
        count=len(template_views),
        templates=template_views,
    )
    tool_capabilities = ToolCapabilityListResponse(
        catalog_version=compiled.manifest.content_version,
        catalog_hash=compiled.content_hash,
        count=len(capability_views),
        tool_capabilities=capability_views,
    )
    approval_policies = ApprovalPolicyListResponse(
        catalog_version=compiled.manifest.content_version,
        catalog_hash=compiled.content_hash,
        count=len(policy_views),
        approval_policies=policy_views,
    )
    instances = AgentInstanceListResponse(
        catalog_version=compiled.manifest.content_version,
        catalog_hash=compiled.content_hash,
        count=len(instance_views),
        instances=instance_views,
    )
    return CatalogDocuments(
        catalog=_representation("catalog", catalog),
        hierarchy=_representation("catalog-hierarchy", hierarchy),
        templates=_representation("agent-templates", templates),
        tool_capabilities=_representation("tool-capabilities", tool_capabilities),
        approval_policies=_representation("approval-policies", approval_policies),
        instances=_representation("agent-instances", instances),
        template_details=MappingProxyType(template_details),
        instance_details=MappingProxyType(instance_details),
    )
