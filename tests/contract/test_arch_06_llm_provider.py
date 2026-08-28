"""ARCH-06: deterministic default and exact opt-in real LLM composition."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.policies.json_schema import (
    DRAFT_2020_12_DIALECT,
    JsonSchemaPolicyError,
)
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
    RuntimePolicyViolation,
)
from marketing_agents.application.ports.llm import (
    LLMInvocationContext,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    TrustedSystemInstructions,
)
from marketing_agents.config import Settings
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.infrastructure.adapters.llm import deterministic, factory
from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicLLMProvider,
    DeterministicRenderContext,
    DeterministicRendererError,
    DeterministicRendererRegistry,
    RendererKey,
    RendererRegistration,
)
from marketing_agents.infrastructure.adapters.llm.factory import (
    LLMProviderConfigurationError,
    LLMProviderSettings,
    RealLLMProviderRegistry,
    RealProviderRegistration,
    ValidatingRealLLMProvider,
    build_llm_provider,
)
from marketing_agents.infrastructure.adapters.llm.validation import (
    LLMRequestPolicyError,
    LLMResponsePolicyError,
)
from marketing_agents.security.content_trust import ExternalContentKind, UntrustedContentPart
from pydantic import JsonValue, ValidationError

TEMPLATE_ID = "tpl.social-media.new-content.linkedin-post-drafter"
SCHEMA_ID = "schema:linkedin-draft:v1"
OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "$schema": DRAFT_2020_12_DIALECT,
    "$id": SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": ["draft", "fixture"],
    "properties": {
        "draft": {"type": "string"},
        "fixture": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
    },
}


def _guard(*, output_max_bytes: int = 4096, max_json_depth: int = 8) -> RuntimePolicyGuard:
    return RuntimePolicyGuard(
        RuntimePolicySnapshot(
            allowed_capabilities=(
                CapabilityPolicy(
                    capability_id="model.generate_structured",
                    effect="read",
                    connector_family="llm",
                ),
            ),
            input_max_bytes=4096,
            output_max_bytes=output_max_bytes,
            max_json_depth=max_json_depth,
            max_content_parts=4,
            max_content_characters=1024,
            max_model_calls=4,
            max_tool_calls=0,
            rate_window_max_calls=4,
            rate_window_seconds=60,
            step_timeout_seconds=30,
            run_timeout_seconds=60,
        )
    )


def _request(
    *,
    run_id: str = "run:1",
    content: str = "Write about deterministic orchestration.",
    output_schema_id: str = SCHEMA_ID,
    output_schema: dict[str, JsonValue] | None = None,
    output_schema_hash: str | None = None,
    max_output_tokens: int = 128,
) -> LLMRequest:
    selected_schema = dict(OUTPUT_SCHEMA) if output_schema is None else output_schema
    if output_schema is None and output_schema_id != SCHEMA_ID:
        selected_schema["$id"] = output_schema_id
    return LLMRequest(
        system_instructions=TrustedSystemInstructions(
            template_id=TEMPLATE_ID,
            catalog_content_hash="a" * 64,
            content="Return only the schema-bound draft.",
        ),
        retrieved_content=(
            UntrustedContentPart(
                kind=ExternalContentKind.USER_INPUT,
                source_id="brief:1",
                content=content,
                provenance_ids=("brief:1",),
            ),
        ),
        output_schema_id=output_schema_id,
        output_schema_hash=(
            canonical_schema_hash(selected_schema)
            if output_schema_hash is None
            else output_schema_hash
        ),
        output_schema=selected_schema,
        context=LLMInvocationContext(
            run_id=run_id,
            step_id="step:draft",
            correlation_id=f"correlation:{run_id}",
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            max_output_tokens=max_output_tokens,
        ),
    )


def _render_draft(
    request: LLMRequest,
    context: DeterministicRenderContext,
) -> dict[str, JsonValue]:
    return {
        "draft": f"Draft for {request.retrieved_content[0].source_id}",
        "fixture": context.fixture_key[:12],
    }


def _renderer_registry() -> DeterministicRendererRegistry:
    return DeterministicRendererRegistry(
        (
            RendererRegistration(
                key=RendererKey(TEMPLATE_ID, SCHEMA_ID),
                version="fixture-v1",
                output_schema_hash=canonical_schema_hash(OUTPUT_SCHEMA),
                renderer=_render_draft,
            ),
        )
    )


def _changed_output_schema() -> dict[str, JsonValue]:
    return {
        "$schema": DRAFT_2020_12_DIALECT,
        "$id": SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": ["replacement"],
        "properties": {"replacement": {"type": "boolean"}},
    }


def test_arch_06_default_factory_is_offline_deterministic_and_exact_keyed() -> None:
    provider = build_llm_provider(
        Settings(_env_file=None),
        renderer_registry=_renderer_registry(),
        guard=_guard(),
    )
    assert isinstance(provider, DeterministicLLMProvider)

    first = asyncio.run(provider.generate_structured(_request(run_id="run:1")))
    same_business_input = asyncio.run(provider.generate_structured(_request(run_id="run:2")))
    changed_input = asyncio.run(
        provider.generate_structured(_request(run_id="run:3", content="A changed brief."))
    )

    assert first.structured_payload == same_business_input.structured_payload
    assert first.structured_payload["fixture"] != changed_input.structured_payload["fixture"]
    assert (first.provider, first.model, first.version) == ("mock", "deterministic", "v1")
    assert first.finish_reason == "complete"

    with pytest.raises(DeterministicRendererError, match="no deterministic renderer"):
        asyncio.run(
            provider.generate_structured(
                _request(
                    output_schema_id="schema:unregistered:v1",
                    output_schema={"type": "object"},
                )
            )
        )


def test_arch_06_duplicate_renderers_and_network_capable_imports_are_rejected() -> None:
    registration = RendererRegistration(
        key=RendererKey(TEMPLATE_ID, SCHEMA_ID),
        version="fixture-v1",
        output_schema_hash=canonical_schema_hash(OUTPUT_SCHEMA),
        renderer=_render_draft,
    )
    with pytest.raises(DeterministicRendererError, match="duplicate deterministic renderer"):
        DeterministicRendererRegistry((registration, registration))
    with pytest.raises(DeterministicRendererError, match="schema hash must be canonical"):
        RendererRegistration(
            key=RendererKey(TEMPLATE_ID, SCHEMA_ID),
            version="fixture-v1",
            output_schema_hash="not-a-schema-hash",
            renderer=_render_draft,
        )

    forbidden_roots = {"aiohttp", "boto3", "httpx", "openai", "requests", "urllib3"}
    for module in (deterministic, factory):
        source_path = Path(module.__file__ or "")
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert imported_roots.isdisjoint(forbidden_roots)


def test_api_08_same_id_changed_schema_is_rejected_before_renderer_call() -> None:
    renderer_calls = 0

    def counting_renderer(
        request: LLMRequest,
        context: DeterministicRenderContext,
    ) -> dict[str, JsonValue]:
        nonlocal renderer_calls
        renderer_calls += 1
        return _render_draft(request, context)

    registry = DeterministicRendererRegistry(
        (
            RendererRegistration(
                key=RendererKey(TEMPLATE_ID, SCHEMA_ID),
                version="fixture-v1",
                output_schema_hash=canonical_schema_hash(OUTPUT_SCHEMA),
                renderer=counting_renderer,
            ),
        )
    )
    provider = DeterministicLLMProvider(registry, _guard())

    with pytest.raises(LLMRequestPolicyError) as changed_schema:
        asyncio.run(provider.generate_structured(_request(output_schema=_changed_output_schema())))
    assert changed_schema.value.code == "renderer_schema_hash_mismatch"
    assert renderer_calls == 0

    with pytest.raises(LLMRequestPolicyError) as false_hash:
        asyncio.run(
            provider.generate_structured(
                _request(
                    output_schema=_changed_output_schema(),
                    output_schema_hash=canonical_schema_hash(OUTPUT_SCHEMA),
                )
            )
        )
    assert false_hash.value.code == "schema_hash_mismatch"
    assert renderer_calls == 0


@pytest.mark.parametrize(
    ("schema", "code"),
    [
        (
            {
                "$schema": DRAFT_2020_12_DIALECT,
                "$id": SCHEMA_ID,
                "$ref": "https://schemas.example.test/remote.json",
            },
            "schema_reference_nonlocal",
        ),
        (
            {
                "$schema": DRAFT_2020_12_DIALECT,
                "$id": SCHEMA_ID,
                "type": 7,
            },
            "schema_invalid",
        ),
        (
            {
                "$schema": DRAFT_2020_12_DIALECT,
                "$id": SCHEMA_ID,
                "$ref": "#/$defs/missing",
            },
            "schema_invalid",
        ),
        (
            {
                "$schema": DRAFT_2020_12_DIALECT,
                "$id": "schema:different-output:v1",
                "type": "object",
            },
            "schema_identity_mismatch",
        ),
    ],
)
def test_api_08_remote_or_malformed_schema_never_reaches_renderer_or_provider(
    schema: dict[str, JsonValue],
    code: str,
) -> None:
    renderer_calls = 0

    def counting_renderer(
        request: LLMRequest,
        context: DeterministicRenderContext,
    ) -> dict[str, JsonValue]:
        nonlocal renderer_calls
        renderer_calls += 1
        return _render_draft(request, context)

    deterministic_provider = DeterministicLLMProvider(
        DeterministicRendererRegistry(
            (
                RendererRegistration(
                    key=RendererKey(TEMPLATE_ID, SCHEMA_ID),
                    version="fixture-v1",
                    output_schema_hash=canonical_schema_hash(OUTPUT_SCHEMA),
                    renderer=counting_renderer,
                ),
            )
        ),
        _guard(),
    )
    with pytest.raises(JsonSchemaPolicyError) as renderer_error:
        asyncio.run(deterministic_provider.generate_structured(_request(output_schema=schema)))
    assert renderer_error.value.code == code
    assert renderer_calls == 0

    class NeverCalledProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_structured(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            raise AssertionError("invalid schema reached real provider")

    delegate = NeverCalledProvider()
    real_provider = ValidatingRealLLMProvider(delegate, _guard(), expected_provider="openai")
    with pytest.raises(JsonSchemaPolicyError) as provider_error:
        asyncio.run(real_provider.generate_structured(_request(output_schema=schema)))
    assert provider_error.value.code == code
    assert delegate.calls == 0


def test_api_08_non_request_objects_and_constructed_corruption_never_reach_provider() -> None:
    class NeverCalledProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_structured(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            raise AssertionError("invalid request reached provider")

    delegate = NeverCalledProvider()
    provider = ValidatingRealLLMProvider(delegate, _guard(), expected_provider="openai")
    invalid_requests = (
        cast(LLMRequest, {"not": "an LLMRequest"}),
        LLMRequest.model_construct(),
    )
    for invalid_request in invalid_requests:
        with pytest.raises(LLMRequestPolicyError) as captured:
            asyncio.run(provider.generate_structured(invalid_request))
        assert captured.value.code == "request_invalid"
    assert delegate.calls == 0


def test_api_08_deterministic_renderer_failures_are_context_free_and_non_reflective() -> None:
    canary = "api-08-provider-secret-canary"

    class HostileDict(dict[str, JsonValue]):
        def items(self):  # type: ignore[no-untyped-def]
            raise RuntimeError(canary)

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, _key: str) -> object:
            raise RuntimeError(canary)

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise RuntimeError(canary)

        def __len__(self) -> int:
            raise RuntimeError(canary)

    def raise_directly(_request, _context):  # type: ignore[no-untyped-def]
        raise RuntimeError(canary)

    renderers = (
        raise_directly,
        lambda _request, _context: HostileDict(),
        lambda _request, _context: {
            "draft": HostileMapping(),
            "fixture": "a" * 12,
        },
    )
    for renderer in renderers:
        registry = DeterministicRendererRegistry(
            (
                RendererRegistration(
                    key=RendererKey(TEMPLATE_ID, SCHEMA_ID),
                    version="fixture-v1",
                    output_schema_hash=canonical_schema_hash(OUTPUT_SCHEMA),
                    renderer=renderer,  # type: ignore[arg-type]
                ),
            )
        )
        provider = DeterministicLLMProvider(registry, _guard())
        with pytest.raises(LLMResponsePolicyError) as captured:
            asyncio.run(provider.generate_structured(_request()))
        assert captured.value.code == "provider_response_invalid"
        assert canary not in str(captured.value)
        assert canary not in repr(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("renderer", "guard", "input_request", "error_type", "code"),
    [
        (
            lambda _request, _context: {"wrong": True},
            _guard(),
            _request(),
            RuntimePolicyViolation,
            "output_schema_invalid",
        ),
        (
            lambda _request, _context: {"draft": "x" * 100, "fixture": "a" * 12},
            _guard(output_max_bytes=64),
            _request(),
            RuntimePolicyViolation,
            "output_byte_limit",
        ),
        (
            _render_draft,
            _guard(),
            _request(max_output_tokens=1),
            LLMResponsePolicyError,
            "output_token_limit",
        ),
    ],
)
def test_arch_06_mock_output_is_independently_schema_and_bounds_validated(
    renderer: object,
    guard: RuntimePolicyGuard,
    input_request: LLMRequest,
    error_type: type[Exception],
    code: str,
) -> None:
    registry = DeterministicRendererRegistry(
        (
            RendererRegistration(
                key=RendererKey(TEMPLATE_ID, SCHEMA_ID),
                version="fixture-v1",
                output_schema_hash=canonical_schema_hash(OUTPUT_SCHEMA),
                renderer=renderer,  # type: ignore[arg-type]
            ),
        )
    )
    provider = DeterministicLLMProvider(registry, guard)
    with pytest.raises(error_type) as captured:
        asyncio.run(provider.generate_structured(input_request))
    assert isinstance(captured.value, RuntimePolicyViolation | LLMResponsePolicyError)
    assert captured.value.code == code


class _FakeRealProvider:
    def __init__(self, *, provider_id: str = "openai", malformed: bool = False) -> None:
        self.provider_id = provider_id
        self.malformed = malformed

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, JsonValue] = (
            {"wrong": True} if self.malformed else {"draft": "fake real draft", "fixture": "b" * 12}
        )
        return LLMResponse(
            structured_payload=payload,
            provider=self.provider_id,
            model="fake-structured-model",
            version="test-v1",
            finish_reason="complete",
            usage=LLMUsage(input_tokens=10, output_tokens=10),
        )


class _StaticRealProvider:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return cast(LLMResponse, self.response)


@pytest.mark.parametrize(
    "response",
    [
        {"not": "an LLMResponse"},
        LLMResponse.model_construct(
            structured_payload={"draft": "x", "fixture": "b" * 12},
            provider="openai",
            model=17,
            version="test-v1",
            finish_reason="complete",
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        ),
        LLMResponse.model_construct(
            structured_payload={"draft": float("nan"), "fixture": "b" * 12},
            provider="openai",
            model="fake-structured-model",
            version="test-v1",
            finish_reason="complete",
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        ),
    ],
)
def test_api_08_real_response_requires_exact_canonical_llm_response(response: object) -> None:
    delegate = _StaticRealProvider(response)
    provider = ValidatingRealLLMProvider(delegate, _guard(), expected_provider="openai")

    with pytest.raises(LLMResponsePolicyError) as captured:
        asyncio.run(provider.generate_structured(_request()))
    assert captured.value.code == "provider_response_invalid"
    assert delegate.calls == 1


def test_api_08_non_complete_response_fails_before_payload_schema_validation() -> None:
    response = LLMResponse(
        structured_payload={"wrong": True},
        provider="openai",
        model="fake-structured-model",
        version="test-v1",
        finish_reason="length",
        usage=LLMUsage(input_tokens=1, output_tokens=1),
    )
    provider = ValidatingRealLLMProvider(
        _StaticRealProvider(response),
        _guard(),
        expected_provider="openai",
    )

    with pytest.raises(LLMResponsePolicyError) as captured:
        asyncio.run(provider.generate_structured(_request()))
    assert captured.value.code == "provider_response_incomplete"


def test_api_08_response_is_canonical_detached_data_and_schema_snapshot_cannot_drift() -> None:
    response = LLMResponse(
        structured_payload={"draft": "Cafe\u0301", "fixture": "b" * 12},
        provider="openai",
        model="fake-structured-model",
        version="test-v1",
        finish_reason="complete",
        usage=LLMUsage(input_tokens=1, output_tokens=1),
    )
    provider = ValidatingRealLLMProvider(
        _StaticRealProvider(response),
        _guard(),
        expected_provider="openai",
    )

    validated = asyncio.run(provider.generate_structured(_request()))
    assert validated is not response
    assert validated.structured_payload["draft"] == "Café"
    response.structured_payload["draft"] = "mutated after return"
    assert validated.structured_payload["draft"] == "Café"

    class SchemaMutatingProvider:
        async def generate_structured(self, request: LLMRequest) -> LLMResponse:
            request.output_schema.clear()
            request.output_schema.update(
                {
                    "$schema": DRAFT_2020_12_DIALECT,
                    "$id": SCHEMA_ID,
                    "type": "object",
                }
            )
            return LLMResponse(
                structured_payload={"wrong": True},
                provider="openai",
                model="fake-structured-model",
                version="test-v1",
                finish_reason="complete",
                usage=LLMUsage(input_tokens=1, output_tokens=1),
            )

    mutating_provider = ValidatingRealLLMProvider(
        SchemaMutatingProvider(),
        _guard(),
        expected_provider="openai",
    )
    with pytest.raises(RuntimePolicyViolation) as captured:
        asyncio.run(mutating_provider.generate_structured(_request()))
    assert captured.value.code == "output_schema_invalid"

    class BudgetMutatingProvider:
        async def generate_structured(self, request: LLMRequest) -> LLMResponse:
            object.__setattr__(request.context, "max_output_tokens", 32_768)
            return LLMResponse(
                structured_payload={"draft": "valid", "fixture": "b" * 12},
                provider="openai",
                model="fake-structured-model",
                version="test-v1",
                finish_reason="complete",
                usage=LLMUsage(input_tokens=1, output_tokens=129),
            )

    budget_mutating_provider = ValidatingRealLLMProvider(
        BudgetMutatingProvider(),
        _guard(),
        expected_provider="openai",
    )
    with pytest.raises(LLMResponsePolicyError) as budget_error:
        asyncio.run(budget_mutating_provider.generate_structured(_request()))
    assert budget_error.value.code == "output_token_limit"


def _fake_real_factory(_settings: LLMProviderSettings) -> LLMProvider:
    return _FakeRealProvider()


def _real_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "llm_provider": "openai",
        "allow_external_network": True,
        "real_llm_opt_in": True,
        "real_llm_api_key": "test-only-credential",
    }
    values.update(updates)
    return Settings(**values)


def test_arch_06_real_provider_requires_all_settings_and_exact_registered_factory() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            llm_provider="openai",
            real_llm_api_key="test-only-credential",
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            llm_provider="openai",
            allow_external_network=True,
            real_llm_api_key="test-only-credential",
        )

    valid = _real_settings()
    with pytest.raises(LLMProviderConfigurationError, match="not explicitly registered"):
        build_llm_provider(valid, renderer_registry=_renderer_registry(), guard=_guard())

    with pytest.raises(LLMProviderConfigurationError, match="non-empty credential"):
        build_llm_provider(
            _real_settings(real_llm_api_key=""),
            renderer_registry=_renderer_registry(),
            guard=_guard(),
            real_registry=RealLLMProviderRegistry(
                (RealProviderRegistration("openai", _fake_real_factory),)
            ),
        )

    wrong_case_registry = RealLLMProviderRegistry(
        (RealProviderRegistration("OpenAI", _fake_real_factory),)
    )
    with pytest.raises(LLMProviderConfigurationError, match="not explicitly registered"):
        build_llm_provider(
            valid,
            renderer_registry=_renderer_registry(),
            guard=_guard(),
            real_registry=wrong_case_registry,
        )

    exact_registry = RealLLMProviderRegistry(
        (RealProviderRegistration("openai", _fake_real_factory),)
    )
    provider = build_llm_provider(
        valid,
        renderer_registry=_renderer_registry(),
        guard=_guard(),
        real_registry=exact_registry,
    )
    assert isinstance(provider, ValidatingRealLLMProvider)
    assert asyncio.run(provider.generate_structured(_request())).provider == "openai"


def test_arch_06_real_errors_propagate_without_mock_fallback_and_outputs_are_checked() -> None:
    class FactoryFailure(RuntimeError):
        pass

    def fail_factory(_settings: LLMProviderSettings) -> LLMProvider:
        raise FactoryFailure("real factory failed")

    failing_registry = RealLLMProviderRegistry((RealProviderRegistration("openai", fail_factory),))
    with pytest.raises(FactoryFailure, match="real factory failed"):
        build_llm_provider(
            _real_settings(),
            renderer_registry=_renderer_registry(),
            guard=_guard(),
            real_registry=failing_registry,
        )

    def malformed_factory(_settings: LLMProviderSettings) -> LLMProvider:
        return _FakeRealProvider(malformed=True)

    malformed = build_llm_provider(
        _real_settings(),
        renderer_registry=_renderer_registry(),
        guard=_guard(),
        real_registry=RealLLMProviderRegistry(
            (RealProviderRegistration("openai", malformed_factory),)
        ),
    )
    with pytest.raises(RuntimePolicyViolation) as captured:
        asyncio.run(malformed.generate_structured(_request()))
    assert captured.value.code == "output_schema_invalid"

    def wrong_identity_factory(_settings: LLMProviderSettings) -> LLMProvider:
        return _FakeRealProvider(provider_id="other")

    wrong_identity = build_llm_provider(
        _real_settings(),
        renderer_registry=_renderer_registry(),
        guard=_guard(),
        real_registry=RealLLMProviderRegistry(
            (RealProviderRegistration("openai", wrong_identity_factory),)
        ),
    )
    with pytest.raises(LLMResponsePolicyError) as identity_error:
        asyncio.run(wrong_identity.generate_structured(_request()))
    assert identity_error.value.code == "provider_identity_mismatch"
