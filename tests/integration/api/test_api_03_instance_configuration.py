"""API-03: safe mutable instance configuration over immutable seeded templates."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.dependencies import InstanceConfigurationExecutor
from marketing_agents.api.instance_configuration_etag import instance_configuration_etag
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationSchema,
    InstanceConfigurationServiceError,
    InstanceConfigurationSnapshot,
    InstanceConfigurationUpdateResult,
    UpdateInstanceConfigurationCommand,
)
from marketing_agents.config import Settings
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    InstanceConnectorBinding,
    InstanceTriggerBinding,
)
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    catalog_instance_configuration_defaults,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.scheduling.cron_recurrence import (
    CroniterRecurrenceCalculator,
)

from tests.support.identity import (
    StaticIdentityProvider,
    human_principal,
    service_principal,
)

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
MUTABLE_FIELDS = {
    "enabled",
    "variantLabel",
    "triggerBindings",
    "connectorBindings",
    "schedule",
}
IMMUTABLE_FIELD_CANARY = "immutable-prompt-secret-canary"


class InMemoryConfigurationExecutor:
    """Strict async test seam with durable state across requests to one app."""

    def __init__(
        self,
        configurations: tuple[InstanceConfiguration, ...],
        *,
        template_id: str,
    ) -> None:
        self.configurations = {item.instance_id: item for item in configurations}
        self.template_id = template_id
        self.read_principals: list[AuthenticatedPrincipal] = []
        self.read_all_principals: list[AuthenticatedPrincipal] = []
        self.schema_principals: list[AuthenticatedPrincipal] = []
        self.update_principals: list[AuthenticatedPrincipal] = []
        self.commands: list[UpdateInstanceConfigurationCommand] = []
        self.read_error: Exception | None = None
        self.schema_error: Exception | None = None
        self.update_error: Exception | None = None
        self.read_result_override: object | None = None
        self.schema_result_override: object | None = None
        self.update_result_override: object | None = None

    async def read(
        self,
        instance_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfiguration:
        self.read_principals.append(principal)
        if self.read_error is not None:
            raise self.read_error
        if self.read_result_override is not None:
            return cast(InstanceConfiguration, self.read_result_override)
        configuration = self.configurations.get(instance_id)
        if configuration is None:
            raise InstanceConfigurationServiceError(
                "instance_not_found",
                "test instance does not exist",
            )
        return configuration

    async def read_all(
        self,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationSnapshot:
        self.read_all_principals.append(principal)
        configurations = tuple(
            self.configurations[instance_id] for instance_id in sorted(self.configurations)
        )
        material = "|".join(
            f"{item.instance_id}:{item.configuration_revision}" for item in configurations
        ).encode()
        return InstanceConfigurationSnapshot(
            configurations=configurations,
            version=hashlib.sha256(material).hexdigest(),
        )

    async def schema(
        self,
        instance_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationSchema:
        self.schema_principals.append(principal)
        if self.schema_error is not None:
            raise self.schema_error
        if self.schema_result_override is not None:
            return cast(InstanceConfigurationSchema, self.schema_result_override)
        if instance_id not in self.configurations:
            raise InstanceConfigurationServiceError(
                "instance_not_found",
                "test instance does not exist",
            )
        return InstanceConfigurationSchema(
            instance_id=instance_id,
            template_id=self.template_id,
            configuration_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "variantLabel": {"type": ["string", "null"]},
                    "triggerBindings": {"type": "array"},
                    "connectorBindings": {"type": "object"},
                    "schedule": {"type": ["object", "null"]},
                },
            },
        )

    async def update(
        self,
        command: UpdateInstanceConfigurationCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationUpdateResult:
        self.update_principals.append(principal)
        self.commands.append(command)
        if self.update_error is not None:
            raise self.update_error
        if self.update_result_override is not None:
            return cast(InstanceConfigurationUpdateResult, self.update_result_override)
        current = self.configurations.get(command.instance_id)
        if current is None:
            raise InstanceConfigurationServiceError(
                "instance_not_found",
                "test instance does not exist",
            )
        if command.expected_revision != current.configuration_revision:
            raise InstanceConfigurationServiceError(
                "configuration_revision_conflict",
                "test revision changed",
                current_revision=current.configuration_revision,
            )
        candidate = command.patch.apply(current)
        changed = candidate != current
        if changed:
            candidate = candidate.with_revision(current.configuration_revision + 1)
            self.configurations[command.instance_id] = candidate
        return InstanceConfigurationUpdateResult(configuration=candidate, changed=changed)


class SynchronousConfigurationExecutor:
    """Malformed seam: FastAPI must reject it before any method is called."""

    def __init__(self) -> None:
        self.called = False

    def read(self, *_args: object, **_kwargs: object) -> object:
        self.called = True
        return object()

    def read_all(self, *_args: object, **_kwargs: object) -> object:
        self.called = True
        return object()

    def schema(self, *_args: object, **_kwargs: object) -> object:
        self.called = True
        return object()

    def update(self, *_args: object, **_kwargs: object) -> object:
        self.called = True
        return object()


@pytest.fixture(scope="module")
def compiled() -> CompiledCatalog:
    return compile_catalog(CATALOG_ROOT)


@pytest.fixture(scope="module")
def defaults(compiled: CompiledCatalog) -> tuple[InstanceConfiguration, ...]:
    return catalog_instance_configuration_defaults(compiled, CroniterRecurrenceCalculator())


@pytest.fixture(scope="module")
def target(defaults: tuple[InstanceConfiguration, ...]) -> InstanceConfiguration:
    return defaults[0]


def _settings() -> Settings:
    return Settings(_env_file=None, catalog_root=CATALOG_ROOT)


def _executor(
    defaults: tuple[InstanceConfiguration, ...],
    compiled: CompiledCatalog,
    *,
    target_only: bool = True,
) -> InMemoryConfigurationExecutor:
    configurations = defaults[:1] if target_only else defaults
    target_id = configurations[0].instance_id
    template_id = next(item.template_id for item in compiled.instances if item.id == target_id)
    return InMemoryConfigurationExecutor(configurations, template_id=template_id)


def _app(
    executor: object | None,
    *,
    principal: AuthenticatedPrincipal | None = None,
) -> FastAPI:
    provider = StaticIdentityProvider(
        principal
        if principal is not None
        else human_principal(
            actor_id="principal.test.local-admin",
            roles=frozenset({"local_admin"}),
            scopes=frozenset(),
        )
    )
    return create_app(
        _settings(),
        identity_provider=provider,
        instance_configuration_service=cast(
            InstanceConfigurationExecutor | None,
            executor,
        ),
    )


def _configuration_path(instance_id: str) -> str:
    return f"/api/v1/agent-instances/{instance_id}/configuration"


def _schema_path(instance_id: str) -> str:
    return f"/api/v1/agent-instances/{instance_id}/configuration-schema"


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: Any,
) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


def _find_instance(body: Mapping[str, Any], instance_id: str) -> Mapping[str, Any]:
    return next(item for item in body["instances"] if item["id"] == instance_id)


def _find_hierarchy_instance(
    body: Mapping[str, Any],
    instance_id: str,
) -> Mapping[str, Any]:
    return next(
        instance
        for department in body["departments"]
        for function in department["functions"]
        for instance in function["instances"]
        if instance["id"] == instance_id
    )


@pytest.mark.asyncio
async def test_api_03_viewer_reads_deployment_only_configuration_schema(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    viewer = human_principal(
        actor_id="principal.test.viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset(),
    )
    response = await _request(
        _app(executor, principal=viewer),
        "GET",
        _schema_path(target.instance_id),
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    body = response.json()
    assert body["projectionVersion"] == "instance-configuration-schema-v1"
    assert body["instanceId"] == target.instance_id
    assert set(body["configurationSchema"]["properties"]) == MUTABLE_FIELDS
    assert body["configurationSchema"]["additionalProperties"] is False
    assert not {
        "id",
        "templateId",
        "displayOrder",
        "prompt",
        "purpose",
        "capabilities",
        "operationClassification",
        "approvalPolicyId",
        "retryPolicy",
        "timeoutPolicy",
        "budgetPolicy",
    }.intersection(body["configurationSchema"]["properties"])
    assert executor.schema_principals == [viewer]

    for denied in (
        human_principal(roles=frozenset({"auditor"}), scopes=frozenset()),
        service_principal(roles=frozenset({"viewer"}), scopes=frozenset()),
    ):
        denied_executor = _executor(defaults, compiled)
        denied_response = await _request(
            _app(denied_executor, principal=denied),
            "GET",
            _schema_path(target.instance_id),
        )
        assert denied_response.status_code == 403
        assert denied_response.json() == {"detail": "catalog read is forbidden"}
        assert denied_executor.schema_principals == []


@pytest.mark.asyncio
async def test_api_03_mutation_requires_human_local_admin_before_service_lookup(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    path = _configuration_path(target.instance_id)
    etag = instance_configuration_etag(target.configuration_revision)
    for denied in (
        human_principal(roles=frozenset({"viewer"}), scopes=frozenset()),
        service_principal(roles=frozenset({"local_admin"}), scopes=frozenset()),
    ):
        executor = _executor(defaults, compiled)
        response = await _request(
            _app(executor, principal=denied),
            "PATCH",
            path,
            headers={"If-Match": etag},
            json={"enabled": not target.enabled},
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "instance configuration mutation is forbidden"}
        assert executor.read_principals == []
        assert executor.update_principals == []

    executor = _executor(defaults, compiled)
    executor.update_error = InstanceConfigurationServiceError(
        "configuration_admin_role_missing",
        "defense-in-depth-secret-reason",
    )
    admin = human_principal(
        actor_id="principal.test.local-admin",
        roles=frozenset({"local_admin"}),
        scopes=frozenset(),
    )
    defended = await _request(
        _app(executor, principal=admin),
        "PATCH",
        path,
        headers={"If-Match": etag},
        json={"enabled": not target.enabled},
    )
    assert defended.status_code == 403
    assert "defense-in-depth-secret-reason" not in defended.text
    assert executor.read_principals == [admin]
    assert executor.update_principals == [admin]


@pytest.mark.asyncio
async def test_api_03_patch_accepts_only_one_json_media_type(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    app = _app(executor)
    path = _configuration_path(target.instance_id)
    etag = instance_configuration_etag(target.configuration_revision)
    raw_body = b'{"enabled":false}'
    cases: tuple[list[tuple[str, str]], ...] = (
        [("If-Match", etag)],
        [("If-Match", etag), ("Content-Type", "text/plain")],
        [("If-Match", etag), ("Content-Type", "application/merge-patch+json")],
        [
            ("If-Match", etag),
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ],
    )
    for headers in cases:
        response = await _request(
            app,
            "PATCH",
            path,
            headers=headers,
            content=raw_body,
        )
        assert response.status_code == 415
        assert response.json() == {"detail": "instance configuration requires application/json"}
    assert executor.read_principals == []
    assert executor.update_principals == []

    accepted = await _request(
        app,
        "PATCH",
        path,
        headers={
            "If-Match": etag,
            "Content-Type": "application/json; charset=utf-8",
        },
        content=raw_body,
    )
    assert accepted.status_code == 200
    assert len(executor.update_principals) == 1


@pytest.mark.parametrize(
    "body",
    (
        b'\xef\xbb\xbf{"enabled":false}',
        b'{"enabled":true,"enabled":false}',
        '{"é":1,"e\u0301":2}'.encode(),
        b'{"variantLabel":"\xff"}',
        b'{"enabled":NaN}',
        b'{"enabled":Infinity}',
        b'{"enabled":1e100000}',
        b'{"variantLabel":"\\ud800"}',
    ),
    ids=(
        "bom",
        "duplicate-key",
        "unicode-normalization-collision",
        "invalid-utf8",
        "nan",
        "infinity",
        "numeric-overflow",
        "lone-surrogate",
    ),
)
@pytest.mark.asyncio
async def test_api_08_api_03_patch_rejects_non_strict_json_before_service_lookup(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
    body: bytes,
) -> None:
    executor = _executor(defaults, compiled)

    response = await _request(
        _app(executor),
        "PATCH",
        _configuration_path(target.instance_id),
        headers={
            "Content-Type": "application/json",
            "If-Match": instance_configuration_etag(target.configuration_revision),
        },
        content=body,
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "configuration_invalid",
        "message": "instance configuration is invalid",
        "currentRevision": None,
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert executor.read_principals == []
    assert executor.update_principals == []


@pytest.mark.asyncio
async def test_api_08_api_03_mounted_patch_keeps_strict_json_boundary(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    parent = FastAPI()
    parent.mount("/mounted", _app(executor))
    async with AsyncClient(
        transport=ASGITransport(app=parent),
        base_url="http://testserver",
    ) as client:
        response = await client.patch(
            "/mounted" + _configuration_path(target.instance_id),
            content=b'{"enabled":true,"enabled":false}',
            headers={
                "Content-Type": "application/json",
                "If-Match": instance_configuration_etag(target.configuration_revision),
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "configuration_invalid"
    assert executor.read_principals == []
    assert executor.update_principals == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    (
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        {"Content-Type": "application/json; charset=iso-8859-1"},
        {"Content-Type": "application/json; charset=utf-8; charset=utf-8"},
    ),
)
async def test_api_08_api_03_rejects_encoded_or_ambiguous_json_transport(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
    headers: dict[str, str],
) -> None:
    executor = _executor(defaults, compiled)
    response = await _request(
        _app(executor),
        "PATCH",
        _configuration_path(target.instance_id),
        content=b'{"enabled":false}',
        headers={
            **headers,
            "If-Match": instance_configuration_etag(target.configuration_revision),
        },
    )

    assert response.status_code == 415
    assert response.json() == {"detail": "instance configuration requires application/json"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert executor.read_principals == []
    assert executor.update_principals == []


@pytest.mark.asyncio
async def test_api_08_api_03_patch_bounds_depth_and_streamed_bytes_before_service_lookup(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    app = _app(executor)
    headers = {
        "Content-Type": "application/json",
        "If-Match": instance_configuration_etag(target.configuration_revision),
    }
    deep_body = b'{"variantLabel":' + (b"[" * 64) + b'"safe"' + (b"]" * 64) + b"}"

    async def streamed_oversized_body() -> AsyncIterator[bytes]:
        yield b'{"enabled":false}'
        yield b" " * 1_048_576

    deep = await _request(
        app,
        "PATCH",
        _configuration_path(target.instance_id),
        headers=headers,
        content=deep_body,
    )
    oversized = await _request(
        app,
        "PATCH",
        _configuration_path(target.instance_id),
        headers=headers,
        content=streamed_oversized_body(),
    )

    for response in (deep, oversized):
        assert response.status_code == 422
        assert response.json() == {
            "code": "configuration_invalid",
            "message": "instance configuration is invalid",
            "currentRevision": None,
        }
        assert len(response.content) < 256
    assert executor.read_principals == []
    assert executor.update_principals == []


@pytest.mark.asyncio
async def test_api_03_immutable_and_unknown_input_is_rejected_without_reflection(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    app = _app(executor)
    path = _configuration_path(target.instance_id)
    etag = instance_configuration_etag(target.configuration_revision)
    payloads = (
        {"enabled": False, "prompt": IMMUTABLE_FIELD_CANARY},
        {"enabled": False, "templateId": IMMUTABLE_FIELD_CANARY},
        {"enabled": False, "variant_label": IMMUTABLE_FIELD_CANARY},
        {
            "connectorBindings": {
                "email": {
                    "connectorFamily": "email",
                    "bindingId": "mock.email.default",
                    "enabled": True,
                    "credential": IMMUTABLE_FIELD_CANARY,
                }
            }
        },
    )
    for payload in payloads:
        response = await _request(
            app,
            "PATCH",
            path,
            headers={"If-Match": etag},
            json=payload,
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "request_validation_failed"
        assert IMMUTABLE_FIELD_CANARY not in response.text
        assert all(
            item
            == {
                "pointer": "/body",
                "code": "extra_forbidden",
                "message": "invalid request field",
            }
            for item in response.json()["detail"]["field_errors"]
        )
    assert executor.read_principals == []
    assert executor.update_principals == []


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ({"enabled": None}, "value_error"),
        ({"triggerBindings": None}, "value_error"),
        ({"connectorBindings": None}, "value_error"),
        ({}, "value_error"),
        ({"enabled": 1}, "bool_type"),
    ],
)
@pytest.mark.asyncio
async def test_api_03_patch_rejects_empty_null_required_and_coerced_values(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
    body: dict[str, object],
    expected_code: str,
) -> None:
    executor = _executor(defaults, compiled)
    response = await _request(
        _app(executor),
        "PATCH",
        _configuration_path(target.instance_id),
        headers={"If-Match": instance_configuration_etag(target.configuration_revision)},
        json=body,
    )
    assert response.status_code == 422
    field_errors = response.json()["detail"]["field_errors"]
    assert any(item["code"] == expected_code for item in field_errors)
    assert executor.read_principals == []
    assert executor.update_principals == []


@pytest.mark.asyncio
async def test_api_03_if_match_requires_one_exact_strong_revision_validator(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    app = _app(executor)
    path = _configuration_path(target.instance_id)
    etag = instance_configuration_etag(target.configuration_revision)
    invalid_cases: tuple[tuple[list[tuple[str, str]], int], ...] = (
        ([("Content-Type", "application/json")], 428),
        ([("Content-Type", "application/json"), ("If-Match", f"W/{etag}")], 400),
        ([("Content-Type", "application/json"), ("If-Match", "*")], 400),
        (
            [
                ("Content-Type", "application/json"),
                ("If-Match", f"{etag},{etag}"),
            ],
            400,
        ),
        (
            [
                ("Content-Type", "application/json"),
                ("If-Match", etag),
                ("If-Match", etag),
            ],
            400,
        ),
        (
            [
                ("Content-Type", "application/json"),
                ("If-Match", etag.strip('"')),
            ],
            400,
        ),
        (
            [("Content-Type", "application/json"), ("If-Match", '"bad value"')],
            400,
        ),
        (
            [("Content-Type", "application/json"), ("If-Match", '"a"b"')],
            400,
        ),
    )
    for headers, expected_status in invalid_cases:
        response = await _request(
            app,
            "PATCH",
            path,
            headers=headers,
            content=b'{"enabled":true}',
        )
        assert response.status_code == expected_status
    assert executor.read_principals == []
    assert executor.update_principals == []


@pytest.mark.asyncio
async def test_api_03_patch_preserves_omissions_supports_null_clear_and_reports_noop(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    admin = human_principal(
        actor_id="principal.test.configurator",
        roles=frozenset({"local_admin"}),
        scopes=frozenset(),
    )
    app = _app(executor, principal=admin)
    path = _configuration_path(target.instance_id)
    original = executor.configurations[target.instance_id]

    changed = await _request(
        app,
        "PATCH",
        path,
        headers={"If-Match": instance_configuration_etag(1)},
        json={"variantLabel": "Blue Cafe\u0301 deployment"},
    )
    assert changed.status_code == 200
    assert changed.headers["etag"] == instance_configuration_etag(2)
    assert changed.headers["cache-control"] == "no-store"
    changed_body = changed.json()
    assert set(changed_body) == {"projectionVersion", "configuration"}
    assert changed_body["projectionVersion"] == "instance-configuration-v1"
    assert set(changed_body["configuration"]) == {
        "instanceId",
        "enabled",
        "variantLabel",
        "triggerBindings",
        "connectorBindings",
        "schedule",
        "configurationRevision",
    }
    assert changed_body["configuration"]["variantLabel"] == "Blue Café deployment"
    assert changed_body["configuration"]["configurationRevision"] == 2
    after_change = executor.configurations[target.instance_id]
    assert after_change.enabled == original.enabled
    assert after_change.trigger_bindings == original.trigger_bindings
    assert after_change.connector_bindings == original.connector_bindings
    assert after_change.schedule == original.schedule
    first_patch = executor.commands[0].patch
    assert first_patch.variant_label.provided is True
    assert first_patch.variant_label.value == "Blue Cafe\u0301 deployment"
    assert first_patch.enabled.provided is False
    assert first_patch.trigger_bindings.provided is False
    assert first_patch.connector_bindings.provided is False
    assert first_patch.schedule.provided is False

    no_op = await _request(
        app,
        "PATCH",
        path,
        headers={"If-Match": instance_configuration_etag(2)},
        json={"variantLabel": "Blue Café deployment"},
    )
    assert no_op.status_code == 200
    assert no_op.headers["etag"] == instance_configuration_etag(2)
    assert no_op.json() == changed_body
    assert executor.configurations[target.instance_id] == after_change

    cleared = await _request(
        app,
        "PATCH",
        path,
        headers={"If-Match": instance_configuration_etag(2)},
        json={"variantLabel": None},
    )
    assert cleared.status_code == 200
    assert cleared.headers["etag"] == instance_configuration_etag(3)
    assert cleared.json()["configuration"]["variantLabel"] is None
    assert cleared.json()["configuration"]["configurationRevision"] == 3
    clear_patch = executor.commands[-1].patch
    assert clear_patch.variant_label.provided is True
    assert clear_patch.variant_label.value is None
    assert clear_patch.schedule.provided is False
    assert executor.read_principals == [admin, admin, admin]
    assert executor.update_principals == [admin, admin, admin]


@pytest.mark.asyncio
async def test_api_03_stale_validator_returns_current_revision_without_mutation(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled)
    response = await _request(
        _app(executor),
        "PATCH",
        _configuration_path(target.instance_id),
        headers={"If-Match": instance_configuration_etag(99)},
        json={"enabled": not target.enabled},
    )
    assert response.status_code == 409
    assert response.json() == {
        "code": "configuration_revision_conflict",
        "message": "instance configuration revision changed",
        "currentRevision": target.configuration_revision,
    }
    assert response.headers["cache-control"] == "no-store"
    assert len(executor.read_principals) == 1
    assert executor.update_principals == []
    assert executor.configurations[target.instance_id] == target


@pytest.mark.asyncio
async def test_api_03_missing_malformed_and_failing_executors_fail_closed(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    missing = await _request(
        _app(None),
        "GET",
        _schema_path(target.instance_id),
    )
    assert missing.status_code == 503
    assert missing.json() == {"detail": "instance configuration service unavailable"}

    synchronous = SynchronousConfigurationExecutor()
    malformed = await _request(
        _app(synchronous),
        "GET",
        _schema_path(target.instance_id),
    )
    assert malformed.status_code == 503
    assert synchronous.called is False

    throwing = _executor(defaults, compiled)
    throwing.schema_error = RuntimeError("sensitive-schema-backend-canary")
    failed = await _request(
        _app(throwing),
        "GET",
        _schema_path(target.instance_id),
    )
    assert failed.status_code == 503
    assert failed.json()["code"] == "configuration_unavailable"
    assert "sensitive-schema-backend-canary" not in failed.text

    wrong_schema = _executor(defaults, compiled)
    wrong_schema.schema_result_override = object()
    rejected_schema = await _request(
        _app(wrong_schema),
        "GET",
        _schema_path(target.instance_id),
    )
    assert rejected_schema.status_code == 503
    assert rejected_schema.json()["code"] == "configuration_unavailable"

    wrong_update = _executor(defaults, compiled)
    wrong_update.update_result_override = object()
    rejected_update = await _request(
        _app(wrong_update),
        "PATCH",
        _configuration_path(target.instance_id),
        headers={"If-Match": instance_configuration_etag(target.configuration_revision)},
        json={"enabled": not target.enabled},
    )
    assert rejected_update.status_code == 503
    assert rejected_update.json()["code"] == "configuration_unavailable"

    mismatched_update = _executor(defaults, compiled)
    mismatched_update.update_result_override = InstanceConfigurationUpdateResult(
        configuration=target.with_revision(target.configuration_revision + 1),
        changed=True,
    )
    rejected_mismatch = await _request(
        _app(mismatched_update),
        "PATCH",
        _configuration_path(target.instance_id),
        headers={"If-Match": instance_configuration_etag(target.configuration_revision)},
        json={"enabled": not target.enabled},
    )
    assert rejected_mismatch.status_code == 503
    assert rejected_mismatch.json()["code"] == "configuration_unavailable"


@pytest.mark.asyncio
async def test_api_03_effective_catalog_reads_and_etags_follow_persisted_configuration(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
    target: InstanceConfiguration,
) -> None:
    executor = _executor(defaults, compiled, target_only=False)
    app = _app(executor)
    detail_path = f"/api/v1/agent-instances/{target.instance_id}"
    paths = (
        "/api/v1/catalog",
        "/api/v1/catalog/hierarchy",
        "/api/v1/agent-instances",
        detail_path,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        before = {path: await client.get(path) for path in paths}
        assert {response.status_code for response in before.values()} == {200}
        old_representation_etags = {
            path: response.headers["etag"] for path, response in before.items()
        }
        before_detail = before[detail_path].json()
        assert before_detail["instance"]["configurationEtag"] == (
            instance_configuration_etag(target.configuration_revision)
        )

        patched = await client.patch(
            _configuration_path(target.instance_id),
            headers={"If-Match": before_detail["instance"]["configurationEtag"]},
            json={
                "enabled": not target.enabled,
                "variantLabel": "API-03 effective override",
            },
        )
        assert patched.status_code == 200
        assert patched.headers["etag"] == instance_configuration_etag(2)
        assert patched.json()["configuration"]["enabled"] is (not target.enabled)
        assert patched.json()["configuration"]["variantLabel"] == ("API-03 effective override")

        after = {path: await client.get(path) for path in paths}
        assert {response.status_code for response in after.values()} == {200}
        for path in paths:
            assert after[path].headers["etag"] != old_representation_etags[path]
            revalidated_old = await client.get(
                path,
                headers={"If-None-Match": old_representation_etags[path]},
            )
            assert revalidated_old.status_code == 200
            revalidated_current = await client.get(
                path,
                headers={"If-None-Match": after[path].headers["etag"]},
            )
            assert revalidated_current.status_code == 304

    aggregate_instance = _find_instance(after["/api/v1/catalog"].json(), target.instance_id)
    list_instance = _find_instance(
        after["/api/v1/agent-instances"].json(),
        target.instance_id,
    )
    detail_instance = after[detail_path].json()["instance"]
    for instance in (aggregate_instance, list_instance, detail_instance):
        assert instance["enabled"] is (not target.enabled)
        assert instance["variantLabel"] == "API-03 effective override"
        assert instance["configurationRevision"] == 2
        assert instance["configurationEtag"] == instance_configuration_etag(2)
    hierarchy_instance = _find_hierarchy_instance(
        after["/api/v1/catalog/hierarchy"].json(),
        target.instance_id,
    )
    assert hierarchy_instance["enabled"] is (not target.enabled)
    assert (
        before["/api/v1/catalog"].json()["catalogHash"]
        == (after["/api/v1/catalog"].json()["catalogHash"])
    )
    assert after[detail_path].json()["configurationSchema"] == _schema_path(target.instance_id)
    assert len(executor.read_all_principals) >= len(paths) * 4


@pytest.mark.asyncio
async def test_api_03_effective_catalog_rejects_template_mismatched_configuration(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
) -> None:
    templates = {item.id: item for item in compiled.templates}
    source_instances = {item.id: item for item in compiled.instances}
    manual_only = next(
        configuration
        for configuration in defaults
        if templates[
            source_instances[configuration.instance_id].template_id
        ].supported_trigger_types
        == ("manual",)
    )
    executor = _executor(defaults, compiled, target_only=False)
    executor.configurations[manual_only.instance_id] = InstanceConfiguration(
        instance_id=manual_only.instance_id,
        enabled=manual_only.enabled,
        variant_label=manual_only.variant_label,
        trigger_bindings=(
            InstanceTriggerBinding(
                kind=TriggerKind.WEBHOOK,
                event_source="unsupported.source",
            ),
        ),
        connector_bindings=manual_only.connector_bindings,
        schedule=None,
        configuration_revision=manual_only.configuration_revision,
    )

    response = await _request(_app(executor), "GET", "/api/v1/catalog")
    assert response.status_code == 503
    assert response.json()["code"] == "catalog_unavailable"
    assert "unsupported.source" not in response.text

    connector_target = next(
        configuration
        for configuration in defaults
        if "social"
        in {
            capability.connector_family
            for capability in compiled.tool_capabilities
            if capability.id
            in templates[
                source_instances[configuration.instance_id].template_id
            ].allowed_tool_capability_ids
        }
    )
    connector_executor = _executor(defaults, compiled, target_only=False)
    connector_executor.configurations[connector_target.instance_id] = InstanceConfiguration(
        instance_id=connector_target.instance_id,
        enabled=connector_target.enabled,
        variant_label=connector_target.variant_label,
        trigger_bindings=connector_target.trigger_bindings,
        connector_bindings={
            "social": InstanceConnectorBinding(
                connector_family="social",
                binding_id="mock.social.unregistered",
            )
        },
        schedule=connector_target.schedule,
        configuration_revision=connector_target.configuration_revision,
    )

    connector_response = await _request(
        _app(connector_executor),
        "GET",
        "/api/v1/catalog",
    )
    assert connector_response.status_code == 503
    assert connector_response.json()["code"] == "catalog_unavailable"
    assert "mock.social.unregistered" not in connector_response.text


def test_api_03_openapi_exposes_only_typed_deployment_configuration_contracts(
    compiled: CompiledCatalog,
    defaults: tuple[InstanceConfiguration, ...],
) -> None:
    application = _app(_executor(defaults, compiled))
    openapi = application.openapi()

    schema_operation = openapi["paths"][
        "/api/v1/agent-instances/{instance_id}/configuration-schema"
    ]["get"]
    assert schema_operation["operationId"] == "getAgentInstanceConfigurationSchema"
    assert schema_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/InstanceConfigurationSchemaResponse"
    }
    assert set(schema_operation["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "422",
        "503",
    }
    assert schema_operation["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/InstanceConfigurationRequestValidationError"
    }
    schema_forbidden_refs = {
        item["$ref"]
        for item in schema_operation["responses"]["403"]["content"]["application/json"]["schema"][
            "anyOf"
        ]
    }
    assert schema_forbidden_refs == {
        "#/components/schemas/InstanceConfigurationHttpError",
        "#/components/schemas/InstanceConfigurationProblem",
    }

    patch_operation = openapi["paths"]["/api/v1/agent-instances/{instance_id}/configuration"][
        "patch"
    ]
    assert patch_operation["operationId"] == "updateAgentInstanceConfiguration"
    if_match_parameters = [
        parameter
        for parameter in patch_operation["parameters"]
        if parameter.get("in") == "header" and parameter.get("name") == "If-Match"
    ]
    assert if_match_parameters == [
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "description": (
                "One exact strong ETag from the current instance configuration revision."
            ),
            "schema": {
                "type": "string",
                "pattern": '^"instance-configuration-v1-[1-9][0-9]*"$',
                "maxLength": 200,
            },
        }
    ]
    assert set(patch_operation["requestBody"]["content"]) == {"application/json"}
    assert patch_operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/InstanceConfigurationPatchInput"
    }
    assert set(patch_operation["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "415",
        "422",
        "428",
        "503",
    }
    success_headers = patch_operation["responses"]["200"]["headers"]
    assert success_headers["ETag"]["schema"] == {
        "type": "string",
        "pattern": '^"instance-configuration-v1-[1-9][0-9]*"$',
        "maxLength": 200,
    }
    validation_refs = {
        item["$ref"]
        for item in patch_operation["responses"]["422"]["content"]["application/json"]["schema"][
            "anyOf"
        ]
    }
    assert validation_refs == {
        "#/components/schemas/InstanceConfigurationProblem",
        "#/components/schemas/InstanceConfigurationRequestValidationError",
    }
    validation_detail_schema = openapi["components"]["schemas"][
        "InstanceConfigurationRequestValidationDetail"
    ]
    assert "field_errors" in validation_detail_schema["properties"]
    assert "fieldErrors" not in validation_detail_schema["properties"]
    patch_schema = openapi["components"]["schemas"]["InstanceConfigurationPatchInput"]
    assert patch_schema["additionalProperties"] is False
    assert set(patch_schema["properties"]) == MUTABLE_FIELDS
    patch_properties = patch_schema["properties"]
    for field_name in ("enabled", "triggerBindings", "connectorBindings"):
        property_schema = patch_properties[field_name]
        assert "default" not in property_schema
        assert property_schema.get("type") != "null"
        assert all(item.get("type") != "null" for item in property_schema.get("anyOf", []))
    assert patch_properties["triggerBindings"]["maxItems"] == 16
    assert patch_properties["connectorBindings"]["maxProperties"] == 16
    for field_name in ("variantLabel", "schedule"):
        assert any(
            item.get("type") == "null" for item in patch_properties[field_name].get("anyOf", [])
        )
    assert patch_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/InstanceConfigurationResponse"
    }
