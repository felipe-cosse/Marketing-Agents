"""ARCH-06: deterministic default and exact opt-in real LLM composition."""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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
from marketing_agents.infrastructure.adapters.llm.validation import LLMResponsePolicyError
from marketing_agents.security.content_trust import ExternalContentKind, UntrustedContentPart
from pydantic import JsonValue, ValidationError

TEMPLATE_ID = "tpl.social-media.new-content.linkedin-post-drafter"
SCHEMA_ID = "schema:linkedin-draft:v1"
OUTPUT_SCHEMA: dict[str, JsonValue] = {
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
    max_output_tokens: int = 128,
) -> LLMRequest:
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
        output_schema=output_schema or OUTPUT_SCHEMA,
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
                renderer=_render_draft,
            ),
        )
    )


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
        renderer=_render_draft,
    )
    with pytest.raises(DeterministicRendererError, match="duplicate deterministic renderer"):
        DeterministicRendererRegistry((registration, registration))

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
