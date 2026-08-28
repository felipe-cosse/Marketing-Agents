"""RUN-10: FastAPI derives identity from its provider and rejects transport assertions."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.api.dependencies import require_authenticated_principal
from marketing_agents.application.ports.identity import IdentityProvider
from marketing_agents.config import Settings
from marketing_agents.domain.identity import AuthenticatedPrincipal

from tests.support.api import assert_problem
from tests.support.identity import FalseyStaticIdentityProvider, human_principal


def _probe_app(provider: IdentityProvider | None = None) -> FastAPI:
    application = create_app(Settings(_env_file=None), identity_provider=provider)

    @application.get("/_run-10/principal")
    async def principal_probe(
        principal: Annotated[
            AuthenticatedPrincipal,
            Depends(require_authenticated_principal),
        ],
    ) -> dict[str, object]:
        return {
            "actor_id": principal.actor_id,
            "authentication_method": principal.authentication_method.value,
            "roles": sorted(principal.roles),
            "scopes": sorted(principal.scopes),
        }

    return application


@pytest.mark.asyncio
async def test_run_10_default_local_identity_is_fixed_and_bearer_is_not_a_fallback() -> None:
    application = _probe_app()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/_run-10/principal")
        assert response.status_code == 200
        assert response.json() == {
            "actor_id": "local-operator",
            "authentication_method": "local_fixed",
            "roles": ["approver", "local_admin", "operator", "viewer"],
            "scopes": [
                "approvals:decide",
                "approvals:read",
                "approvals:request",
                "scope.external-write",
            ],
        }
        token = "transport-secret-canary"
        rejected = await client.get(
            "/_run-10/principal",
            headers={"authorization": f"Bearer {token}"},
        )
        assert rejected.status_code == 401
        assert token not in rejected.text


@pytest.mark.parametrize(
    "header_name",
    [
        "X-Actor",
        "x-actor-id",
        "X-User",
        "X-Role",
        "X-Roles",
        "X-Scope",
        "X-Scopes",
        "X-Principal",
        "X-Auth-User",
        "Remote-User",
        "X-Forwarded-User",
        "X-Forwarded-Email",
        "X-Forwarded-Actor",
        "X-Forwarded-Role",
        "X-Forwarded-Roles",
        "X-Forwarded-Scope",
        "X-Forwarded-Scopes",
    ],
)
@pytest.mark.asyncio
async def test_run_10_spoofed_identity_and_forwarded_headers_fail_before_provider(
    header_name: str,
) -> None:
    provider = FalseyStaticIdentityProvider(human_principal())
    application = _probe_app(provider)
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/_run-10/principal",
            headers={header_name: "forged-authority"},
        )
    assert response.status_code == 400
    assert provider.evidence == []


@pytest.mark.parametrize(
    "header_name",
    ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"],
)
@pytest.mark.asyncio
async def test_api_09_direct_mode_proxy_headers_fail_before_provider(
    header_name: str,
) -> None:
    provider = FalseyStaticIdentityProvider(human_principal())
    application = _probe_app(provider)
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/_run-10/principal",
            headers={header_name: "proxy-metadata"},
        )
    assert_problem(response, status_code=400, code="forwarded_header_forbidden")
    assert provider.evidence == []


@pytest.mark.parametrize(
    "authorization",
    ["Basic abc", "Bearer", "Bearer ", " Bearer abc", "Bearer abc def"],
)
@pytest.mark.asyncio
async def test_run_10_malformed_authorization_is_a_transport_error(
    authorization: str,
) -> None:
    provider = FalseyStaticIdentityProvider(human_principal())
    application = _probe_app(provider)
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/_run-10/principal",
            headers={"authorization": authorization},
        )
    assert response.status_code == 400
    assert provider.evidence == []


@pytest.mark.asyncio
async def test_run_10_injected_falsey_provider_is_used_and_untrusted_host_is_rejected() -> None:
    principal = human_principal(actor_id="principal.test.injected")
    provider = FalseyStaticIdentityProvider(principal)
    application = _probe_app(provider)
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        accepted = await client.get("/_run-10/principal")
        rejected_host = await client.get(
            "/_run-10/principal",
            headers={"host": "evil.example"},
        )
    assert accepted.status_code == 200
    assert accepted.json()["actor_id"] == "principal.test.injected"
    assert len(provider.evidence) == 1
    assert provider.evidence[0].safe_snapshot() == {"bearer_present": False}
    assert rejected_host.status_code == 400


@pytest.mark.asyncio
async def test_run_10_missing_identity_provider_returns_generic_unauthorized() -> None:
    application = _probe_app()
    del application.state.identity_provider
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/_run-10/principal")
    assert_problem(response, status_code=401, code="authentication_required")
