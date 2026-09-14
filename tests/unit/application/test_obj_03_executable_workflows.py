"""OBJ-03: admission and workers share exact executable identities and schemas."""

from __future__ import annotations

from collections import Counter
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

import pytest
from marketing_agents.application.orchestration.executable_workflows import (
    CatalogRoleExecutionKind,
    ExecutableWorkflowHandler,
    ExecutableWorkflowRegistry,
    ExecutableWorkflowRegistryError,
    catalog_role_workflow_id,
)
from marketing_agents.application.ports.manual_work import ManualAdmissionResolutionError
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.incoming_work_validation import (
    IncomingWorkValidationError,
)
from marketing_agents.application.services.manual_work_intake import ManualDryRunCommand
from marketing_agents.demos import DEMO_SCENARIOS
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    InstanceTriggerBinding,
)
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    catalog_instance_configuration_defaults,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.executable_workflows import build_executable_workflow_registry
from marketing_agents.infrastructure.manual_work import CompiledCatalogManualAdmissionResolver
from marketing_agents.infrastructure.scheduling.cron_recurrence import CroniterRecurrenceCalculator
from marketing_agents.infrastructure.webhook_ingress import CompiledCatalogWebhookAdmissionResolver

_TEMPLATE = "tpl.social-media.new-content.linkedin-post-drafter"
_INSTANCE = "inst.social-media.new-content.linkedin-post-drafter.01"
_PAYLOAD = {"request_id": "request.obj-03.registry", "source_content": "Supplied role content."}


@pytest.fixture(scope="module")
def catalog() -> CompiledCatalog:
    return compile_catalog(Path(__file__).resolve().parents[3] / "catalog" / "v1")


@pytest.fixture(scope="module")
def registry(catalog: CompiledCatalog) -> ExecutableWorkflowRegistry:
    return build_executable_workflow_registry(catalog, demo_scenarios=DEMO_SCENARIOS.list())


@pytest.fixture(scope="module")
def configurations(catalog: CompiledCatalog) -> dict[str, InstanceConfiguration]:
    return {
        item.instance_id: item
        for item in catalog_instance_configuration_defaults(catalog, CroniterRecurrenceCalculator())
    }


def test_obj_03_all_36_roles_and_43_instances_have_real_schema_bound_contracts(
    catalog: CompiledCatalog, registry: ExecutableWorkflowRegistry
) -> None:
    counts: Counter[CatalogRoleExecutionKind] = Counter()
    for template in catalog.templates:
        manual = registry.for_catalog_role(template.id, TriggerKind.MANUAL)
        assert manual.execution_kind is not None
        counts[manual.execution_kind] += 1
        assert manual.id == f"workflow.manual.{template.id.removeprefix('tpl.')}.v1"
        assert manual.input_schema_id == template.input_schema_id
        assert manual.output_schema_id == template.output_schema_id
        assert canonical_json_bytes(manual.input_schema) == canonical_json_bytes(
            catalog.input_schema_by_template[template.id]
        )
        assert canonical_json_bytes(manual.output_schema) == canonical_json_bytes(
            catalog.output_schema_by_template[template.id]
        )
        assert manual.demo_scenario_id is None
        assert manual.capability_id in template.allowed_tool_capability_ids
        assert manual.expected_model_calls <= template.budget_policy.max_model_calls
        assert set(manual.target_instance_ids) == {
            item.id for item in catalog.instances if item.template_id == template.id
        }
        for raw_kind in template.supported_trigger_types:
            definition = registry.for_catalog_role(template.id, TriggerKind(raw_kind))
            assert definition.id == catalog_role_workflow_id(template.id, TriggerKind(raw_kind))
            for instance_id in definition.target_instance_ids:
                assert (
                    registry.require_match(
                        definition.id,
                        instance_id=instance_id,
                        template_id=template.id,
                        trigger_kind=TriggerKind(raw_kind),
                        mode=definition.allowed_modes[0],
                        catalog_content_hash=catalog.content_hash,
                    )
                    is definition
                )
    assert counts == {
        CatalogRoleExecutionKind.MODEL: 26,
        CatalogRoleExecutionKind.LOCAL_TRANSFORM: 6,
        CatalogRoleExecutionKind.WRITE: 4,
    }
    assert len({item for entry in registry.list() for item in entry.target_instance_ids}) == 43


def test_obj_03_writes_are_explicit_and_never_claim_zero_mode_independent_dispatch(
    catalog: CompiledCatalog, registry: ExecutableWorkflowRegistry
) -> None:
    capabilities = {item.id: item for item in catalog.tool_capabilities}
    for definition in registry.list():
        if definition.handler_kind is ExecutableWorkflowHandler.CATALOG_WRITE:
            assert definition.execution_kind is CatalogRoleExecutionKind.WRITE
            assert definition.capability_id is not None
            assert capabilities[definition.capability_id].effect == "write"
            assert definition.expected_model_calls == 0
            assert definition.expected_connector_calls is None
            assert definition.expected_external_actions is None
            assert definition.expected_approvals is None
        elif definition.handler_kind is ExecutableWorkflowHandler.CATALOG_ROLE:
            assert definition.expected_connector_calls == 0
            assert definition.expected_external_actions == 0
            assert definition.expected_approvals == 0


def test_obj_03_five_existing_demos_keep_exact_contract_hashes_and_handlers(
    registry: ExecutableWorkflowRegistry,
) -> None:
    for scenario in DEMO_SCENARIOS.list():
        definition = registry.for_demo(scenario.id)
        assert registry.get(scenario.workflow_id) is definition
        assert definition.definition_hash == scenario.definition_hash
        assert definition.input_schema == scenario.input_schema
        assert definition.output_schema == scenario.output_schema
        assert definition.expected_model_calls == scenario.expected_model_calls
        assert definition.expected_connector_calls == scenario.expected_connector_calls
        assert definition.target_instance_ids == (scenario.instance_id,)
        assert definition.eligible_trigger_kinds == (TriggerKind.MANUAL,)
        assert definition.handler_kind is (
            ExecutableWorkflowHandler.DEMO_EMAIL
            if scenario.effect == "mutating"
            else ExecutableWorkflowHandler.DEMO_READ
        )


@pytest.mark.parametrize(
    "change",
    [
        {"instance_id": "inst.unknown"},
        {"template_id": "tpl.unknown"},
        {"trigger_kind": TriggerKind.WEBHOOK},
        {"trigger_kind": "manual"},
        {"mode": "dry_run"},
        {"catalog_content_hash": "catalog-sha256-v1:" + "f" * 64},
    ],
)
def test_obj_03_registry_rejects_every_route_identity_mismatch(
    registry: ExecutableWorkflowRegistry, change: dict[str, Any]
) -> None:
    definition = registry.for_catalog_role(_TEMPLATE, TriggerKind.MANUAL)
    request = {
        "instance_id": _INSTANCE,
        "template_id": _TEMPLATE,
        "trigger_kind": TriggerKind.MANUAL,
        "mode": WorkMode.DRY_RUN,
        "catalog_content_hash": registry.catalog_content_hash,
    } | change
    with pytest.raises(ExecutableWorkflowRegistryError, match="admitted route") as failure:
        registry.require_match(definition.id, **request)
    assert failure.value.code == "workflow_binding_mismatch"


def test_obj_03_registry_preserves_trigger_specific_mode_limits(
    registry: ExecutableWorkflowRegistry,
) -> None:
    for definition in registry.list():
        if definition.demo_scenario_id is not None:
            continue
        kind = definition.eligible_trigger_kinds[0]
        if kind is TriggerKind.SCHEDULE:
            assert definition.allowed_modes == (WorkMode.DRY_RUN,)
        elif kind is TriggerKind.WEBHOOK:
            assert definition.allowed_modes == (WorkMode.MOCK_EXECUTION,)


@pytest.mark.parametrize("unknown", ["manual", "workflow.manual.fake.v1", "demo.fake.v1", [], None])
def test_obj_03_registry_has_no_alias_or_untyped_fallback(
    registry: ExecutableWorkflowRegistry, unknown: Any
) -> None:
    with pytest.raises(ExecutableWorkflowRegistryError) as failure:
        registry.get(unknown)
    assert failure.value.code == "workflow_unknown"


def test_obj_03_registry_and_nested_contracts_are_immutable(
    registry: ExecutableWorkflowRegistry,
) -> None:
    definition = registry.for_catalog_role(_TEMPLATE, TriggerKind.MANUAL)
    with pytest.raises(FrozenInstanceError):
        definition.id = "workflow.changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        definition.input_schema["properties"]["source_content"]["maxLength"] = 1
    with pytest.raises(AttributeError):
        registry.catalog_content_hash = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="unique"):
        ExecutableWorkflowRegistry((definition, definition))
    with pytest.raises(ValueError, match="catalog release"):
        ExecutableWorkflowRegistry(())


def test_obj_03_definition_hash_binds_catalog_schema_and_supported_modes(
    registry: ExecutableWorkflowRegistry,
) -> None:
    definition = registry.for_catalog_role(_TEMPLATE, TriggerKind.MANUAL)
    changed_catalog = replace(definition, catalog_content_hash="catalog-sha256-v1:" + "f" * 64)
    changed_mode = replace(definition, allowed_modes=(WorkMode.DRY_RUN,))
    changed_schema = replace(
        definition, input_schema=dict(definition.input_schema) | {"title": "v2"}
    )
    assert (
        len(
            {
                definition.definition_hash,
                changed_catalog.definition_hash,
                changed_mode.definition_hash,
                changed_schema.definition_hash,
            }
        )
        == 4
    )
    assert changed_schema.input_schema_hash != definition.input_schema_hash
    with pytest.raises(ValueError, match="catalog release"):
        ExecutableWorkflowRegistry(
            (
                definition,
                replace(
                    next(item for item in registry.list() if item.id != definition.id),
                    catalog_content_hash=changed_catalog.catalog_content_hash,
                ),
            )
        )


@pytest.mark.parametrize(
    "change",
    [
        {"id": "workflow.manual.alias.v1"},
        {"expected_model_calls": 2},
        {"expected_connector_calls": 1},
        {"expected_external_actions": 1},
        {"expected_approvals": 1},
        {"capability_id": "cap.newsletter.subscribe"},
        {"source_definition_hash": "f" * 64},
        {"input_schema_id": "schema.forged"},
    ],
)
def test_obj_03_role_definition_cannot_acquire_write_authority_or_schema_alias(
    registry: ExecutableWorkflowRegistry, change: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        replace(registry.for_catalog_role(_TEMPLATE, TriggerKind.MANUAL), **change)


def test_obj_03_factory_rejects_capability_effect_and_write_policy_drift(
    catalog: CompiledCatalog,
) -> None:
    changed_capabilities = tuple(
        item.model_copy(update={"effect": "write"})
        if item.id == "cap.model.generate-structured"
        else item
        for item in catalog.tool_capabilities
    )
    with pytest.raises(ValueError, match="model workflow"):
        build_executable_workflow_registry(replace(catalog, tool_capabilities=changed_capabilities))
    changed_templates = tuple(
        item.model_copy(update={"approval_policy_id": "policy.no-approval.read-only.v1"})
        if item.id == "tpl.email.newsletter.newsletter-subscriber"
        else item
        for item in catalog.templates
    )
    with pytest.raises(ValueError, match="supported execution kind"):
        build_executable_workflow_registry(replace(catalog, templates=changed_templates))


def test_obj_03_factory_rejects_unknown_demo_and_catalog_identity_collision(
    catalog: CompiledCatalog,
) -> None:
    scenario = DEMO_SCENARIOS.get("demo.social-media.content-draft.v1")
    unknown = replace(scenario, id="demo.unknown.v1", workflow_id="demo.unknown.v1")
    with pytest.raises(ValueError, match="handler is unavailable"):
        build_executable_workflow_registry(catalog, demo_scenarios=(unknown,))
    with pytest.raises(ValueError, match="identities are inconsistent"):
        build_executable_workflow_registry(
            replace(catalog, instances=(*catalog.instances, catalog.instances[0]))
        )


def test_obj_03_factory_rejects_nonlocal_schema_reference(catalog: CompiledCatalog) -> None:
    schemas = dict(catalog.input_schema_by_template)
    schemas[_TEMPLATE] = dict(schemas[_TEMPLATE]) | {"$ref": "https://unapproved.invalid/schema"}
    with pytest.raises(ValueError, match="schema document"):
        build_executable_workflow_registry(replace(catalog, input_schema_by_template=schemas))


class _Configurations:
    def __init__(self, entries: dict[str, InstanceConfiguration]) -> None:
        self.entries = entries
        self.locked: list[str] = []

    async def get_for_update(self, instance_id: str) -> InstanceConfiguration | None:
        self.locked.append(instance_id)
        return self.entries.get(instance_id)


class _UnitOfWork:
    def __init__(self, entries: dict[str, InstanceConfiguration]) -> None:
        self.configurations = _Configurations(entries)


def _envelope(
    binding: Any, *, payload: Any = None, mode: WorkMode = WorkMode.DRY_RUN
) -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source=binding.source,
        event_id="event.obj-03.registry",
        instance_id=binding.instance_id,
        trigger_id=binding.trigger_id,
        workflow_id=binding.workflow_id,
        mode=mode,
        brief_id=None,
        brief_revision=None,
        configuration_revision=binding.configuration_revision,
        admitted_payload=_PAYLOAD if payload is None else payload,
    )


@pytest.mark.asyncio
async def test_obj_03_manual_admission_uses_shared_contract_for_every_instance(
    catalog: CompiledCatalog,
    registry: ExecutableWorkflowRegistry,
    configurations: dict[str, InstanceConfiguration],
) -> None:
    resolver = CompiledCatalogManualAdmissionResolver(
        catalog, mock_connectors_active=True, workflows=registry
    )
    uow = _UnitOfWork(configurations)
    for instance in catalog.instances:
        command = ManualDryRunCommand(
            instance_id=instance.id,
            input_payload=_PAYLOAD,
            correlation_id="correlation.obj-03.registry",
        )
        binding = await resolver.resolve_in_uow(cast(UnitOfWork, uow), command)
        definition = registry.for_catalog_role(instance.template_id, TriggerKind.MANUAL)
        validated = binding.validator.validate(_envelope(binding))
        assert binding.workflow_id == definition.id
        assert validated.snapshot.input_schema_hash == definition.input_schema_hash
        assert validated.snapshot.input_schema_id == definition.input_schema_id
        assert validated.envelope.admitted_payload == _PAYLOAD
    assert set(uow.configurations.locked) == set(configurations)


@pytest.mark.asyncio
async def test_obj_03_manual_demo_schema_never_substitutes_for_generic_schema(
    catalog: CompiledCatalog,
    registry: ExecutableWorkflowRegistry,
    configurations: dict[str, InstanceConfiguration],
) -> None:
    resolver = CompiledCatalogManualAdmissionResolver(
        catalog, mock_connectors_active=True, workflows=registry
    )
    scenario = DEMO_SCENARIOS.get("demo.social-media.content-draft.v1")
    command = ManualDryRunCommand(
        instance_id=_INSTANCE,
        input_payload=scenario.fixture,
        correlation_id="correlation.obj-03.registry",
        demo_scenario_id=scenario.id,
    )
    binding = await resolver.resolve_in_uow(cast(UnitOfWork, _UnitOfWork(configurations)), command)
    validated = binding.validator.validate(_envelope(binding, payload=scenario.fixture))
    assert validated.snapshot.input_schema_id == scenario.input_schema_id
    with pytest.raises(IncomingWorkValidationError):
        binding.validator.validate(_envelope(binding))
    generic = await resolver.resolve_in_uow(
        cast(UnitOfWork, _UnitOfWork(configurations)), replace(command, demo_scenario_id=None)
    )
    with pytest.raises(IncomingWorkValidationError):
        generic.validator.validate(_envelope(generic, payload=scenario.fixture))


@pytest.mark.asyncio
async def test_obj_03_resolver_constructor_defaults_remain_compatible_and_fail_closed(
    catalog: CompiledCatalog, configurations: dict[str, InstanceConfiguration]
) -> None:
    resolver = CompiledCatalogManualAdmissionResolver(catalog, mock_connectors_active=False)
    command = ManualDryRunCommand(
        instance_id=_INSTANCE, input_payload=_PAYLOAD, correlation_id="correlation.obj-03.registry"
    )
    binding = await resolver.resolve_in_uow(cast(UnitOfWork, _UnitOfWork(configurations)), command)
    assert binding.workflow_id == catalog_role_workflow_id(_TEMPLATE, TriggerKind.MANUAL)
    with pytest.raises(ManualAdmissionResolutionError) as failure:
        await resolver.resolve_in_uow(
            cast(UnitOfWork, _UnitOfWork(configurations)),
            replace(command, mode=WorkMode.MOCK_EXECUTION),
        )
    assert failure.value.code == "work_mode_not_allowed"
    CompiledCatalogWebhookAdmissionResolver(catalog, mock_connectors_active=True)


@pytest.mark.asyncio
async def test_obj_03_injected_registry_missing_route_never_falls_back_to_generated_id(
    catalog: CompiledCatalog,
    registry: ExecutableWorkflowRegistry,
    configurations: dict[str, InstanceConfiguration],
) -> None:
    missing_id = registry.for_catalog_role(_TEMPLATE, TriggerKind.MANUAL).id
    restricted = ExecutableWorkflowRegistry(
        item for item in registry.list() if item.id != missing_id
    )
    resolver = CompiledCatalogManualAdmissionResolver(
        catalog, mock_connectors_active=True, workflows=restricted
    )
    command = ManualDryRunCommand(
        instance_id=_INSTANCE, input_payload=_PAYLOAD, correlation_id="correlation.obj-03.registry"
    )
    with pytest.raises(ManualAdmissionResolutionError) as failure:
        await resolver.resolve_in_uow(cast(UnitOfWork, _UnitOfWork(configurations)), command)
    assert failure.value.code == "manual_binding_unavailable"


@pytest.mark.asyncio
async def test_obj_03_webhook_resolves_configured_fanout_to_actual_exact_workflows(
    catalog: CompiledCatalog,
    registry: ExecutableWorkflowRegistry,
    configurations: dict[str, InstanceConfiguration],
) -> None:
    templates = {item.id: item for item in catalog.templates}
    eligible = {
        instance.id
        for instance in catalog.instances
        if "webhook" in templates[instance.template_id].supported_trigger_types
    }
    entries = {
        instance_id: replace(
            configuration,
            enabled=True,
            schedule=None,
            trigger_bindings=(
                InstanceTriggerBinding(
                    kind=TriggerKind.WEBHOOK, enabled=True, event_source="source.obj-03"
                ),
            )
            if instance_id in eligible
            else (),
        )
        for instance_id, configuration in configurations.items()
    }
    uow = _UnitOfWork(entries)
    resolver = CompiledCatalogWebhookAdmissionResolver(
        catalog, mock_connectors_active=True, workflows=registry
    )
    bindings = await resolver.resolve_all_in_uow(
        cast(UnitOfWork, uow), source="source.obj-03", trigger_id="trigger.obj-03"
    )
    assert {binding.instance_id for binding in bindings} == eligible
    for binding in bindings:
        definition = registry.get(binding.workflow_id)
        validated = binding.validator.validate(_envelope(binding, mode=WorkMode.MOCK_EXECUTION))
        assert validated.snapshot.input_schema_hash == definition.input_schema_hash
        assert definition.eligible_trigger_kinds == (TriggerKind.WEBHOOK,)
        with pytest.raises(IncomingWorkValidationError):
            binding.validator.validate(_envelope(binding, mode=WorkMode.DRY_RUN))
    assert set(uow.configurations.locked) == set(configurations)
