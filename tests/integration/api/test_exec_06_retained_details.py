"""EXEC-06: expired details retain authorized, content-free HTTP identities only."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.application.services.retained_resources import RetainedResourceService
from marketing_agents.config import Settings
from marketing_agents.domain.retained_details import DetailExpired, RetainedDetail
from marketing_agents.domain.retention import RetentionCategory

from tests.support.identity import StaticIdentityProvider, human_principal, service_principal

CANARY = "private-expired-payload-and-sealed-binding-canary"
NOW = datetime(2026, 10, 14, tzinfo=UTC)
PATH = "/api/v1/retained-details/work/work.retained"


class Repository:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = []

    async def inspect(self, kind, identity):
        self.calls.append((kind, identity))
        if self.error is not None:
            raise self.error
        return self.value


def retained(**changes):
    values = {
        "category": RetentionCategory.ADMITTED_PAYLOAD,
        "record_kind": "work",
        "record_id": "work.retained",
        "created_at": NOW - timedelta(days=8),
        "expires_at": NOW - timedelta(days=1),
        "purged_at": NOW,
        "run_id": "run.retained",
        "metadata": {"private": CANARY},
    }
    return RetainedDetail(**(values | changes))


def application(repository, principal=None):
    return create_app(
        Settings(),
        identity_provider=StaticIdentityProvider(principal or human_principal()),
        retained_resource_service=RetainedResourceService(repository),
    )


def client(app):
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_retained_identity_omits_all_detail_metadata_and_binding_material():
    repository = Repository(retained())
    async with client(application(repository)) as connection:
        response = await connection.get(PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "record_kind": "work",
        "record_id": "work.retained",
        "category": "admitted_payload",
        "run_id": "run.retained",
        "created_at": "2026-10-06T00:00:00Z",
        "expires_at": "2026-10-13T00:00:00Z",
        "purged_at": "2026-10-14T00:00:00Z",
        "detail_expired": True,
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert CANARY not in response.text
    assert repository.calls == [("work", "work.retained")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal", [service_principal(), human_principal(roles=frozenset({"outsider"}))]
)
async def test_retained_inspection_authorization_precedes_repository(principal):
    repository = Repository(retained())
    async with client(application(repository, principal)) as connection:
        response = await connection.get(PATH)
    assert response.status_code == 403
    assert repository.calls == []
    with pytest.raises(PermissionError):
        await RetainedResourceService(repository).read(
            principal=principal, record_kind="work", record_id="work.retained"
        )
    assert repository.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/retained-details/arbitrary/work.retained",
        "/api/v1/retained-details/work/%20invalid",
    ],
)
async def test_retained_query_bounds_reject_before_repository(path):
    repository = Repository(retained())
    async with client(application(repository)) as connection:
        response = await connection.get(path)
    assert response.status_code == 422
    assert repository.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "error", "expected"),
    [
        (None, None, 404),
        (None, RuntimeError(CANARY), 503),
        (retained(record_id="work.other"), None, 503),
        (SimpleNamespace(record_id="work.retained"), None, 503),
    ],
)
async def test_retained_missing_and_invalid_sources_are_safe(value, error, expected):
    async with client(application(Repository(value, error))) as connection:
        response = await connection.get(PATH)
    assert response.status_code == expected, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert CANARY not in response.text


@pytest.mark.asyncio
async def test_detail_expired_uses_fixed_410_without_reflecting_identifiers():
    app = create_app(Settings())

    @app.get("/fixture-expired")
    async def expire():
        raise DetailExpired(CANARY, CANARY)

    async with client(app) as connection:
        response = await connection.get("/fixture-expired")
    assert response.status_code == 410, response.text
    assert response.json()["code"] == "detail_expired"
    assert response.json()["detail"] == "The retained detail has expired."
    assert CANARY not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
