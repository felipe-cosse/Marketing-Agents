"""API-01: process liveness and fail-closed traffic readiness."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.api.routes import health as health_routes
from marketing_agents.application.ports.readiness import (
    READINESS_CHECK_ORDER,
    CatalogReadinessMetadata,
    ReadinessCheck,
    ReadinessCheckName,
    ReadinessCheckStatus,
    ReadinessCode,
    ReadinessProbe,
    ReadinessReport,
)
from marketing_agents.config import Settings
from marketing_agents.infrastructure import readiness as readiness_infrastructure
from marketing_agents.infrastructure.db import Base, create_database_runtime
from marketing_agents.infrastructure.readiness import LocalReadinessProbe
from sqlalchemy import text

CATALOG_METADATA = CatalogReadinessMetadata(
    content_version="1.0.0",
    content_hash="catalog-sha256-v1:" + "1" * 64,
    departments=5,
    functions=12,
    templates=36,
    instances=43,
)


def _report(
    failures: dict[ReadinessCheckName, ReadinessCode] | None = None,
) -> ReadinessReport:
    failures = failures or {}
    return ReadinessReport(
        checks=tuple(
            ReadinessCheck(
                name,
                (
                    ReadinessCheckStatus.NOT_READY
                    if name in failures
                    else ReadinessCheckStatus.READY
                ),
                failures.get(name, ReadinessCode.READY),
            )
            for name in READINESS_CHECK_ORDER
        ),
        catalog=CATALOG_METADATA,
    )


class RecordingProbe:
    def __init__(self, reports: Sequence[ReadinessReport]) -> None:
        self._reports = tuple(reports)
        self.calls = 0

    async def check(self) -> ReadinessReport:
        selected = self._reports[min(self.calls, len(self._reports) - 1)]
        self.calls += 1
        return selected


class FalseyRecordingProbe(RecordingProbe):
    def __bool__(self) -> bool:
        return False


class ThrowingProbe:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    async def check(self) -> ReadinessReport:
        self.calls += 1
        raise RuntimeError(self.message)


class SlowProbe:
    async def check(self) -> ReadinessReport:
        await asyncio.sleep(1)
        return _report()


class MalformedProbe:
    async def check(self) -> ReadinessReport:
        return cast(ReadinessReport, object())


def _app(probe: ReadinessProbe | None) -> object:
    return create_app(Settings(_env_file=None), readiness_probe=probe)


async def _request(app: object, path: str) -> tuple[int, dict[str, object], str]:
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        response = await client.get(path)
    return response.status_code, response.json(), response.headers["cache-control"]


def _expected_checks(
    failures: dict[ReadinessCheckName, ReadinessCode] | None = None,
) -> list[dict[str, str]]:
    failures = failures or {}
    return [
        {
            "name": name.value,
            "status": "not_ready" if name in failures else "ready",
            "code": failures.get(name, ReadinessCode.READY).value,
        }
        for name in READINESS_CHECK_ORDER
    ]


@pytest.mark.asyncio
async def test_api_01_liveness_never_invokes_readiness_probe() -> None:
    probe = ThrowingProbe("liveness-must-not-touch-this-canary")
    status_code, body, cache_control = await _request(_app(probe), "/health/live")

    assert status_code == 200
    assert body == {"status": "ok", "service": "marketing-agents-api"}
    assert cache_control == "no-store"
    assert probe.calls == 0


@pytest.mark.asyncio
async def test_api_01_ready_returns_exact_typed_no_store_projection() -> None:
    probe = FalseyRecordingProbe((_report(),))
    status_code, body, cache_control = await _request(_app(probe), "/health/ready")

    assert status_code == 200
    assert body == {
        "status": "ready",
        "service": "marketing-agents-api",
        "checks": _expected_checks(),
        "catalog": {
            "content_version": "1.0.0",
            "content_hash": "catalog-sha256-v1:" + "1" * 64,
            "departments": 5,
            "functions": 12,
            "templates": 36,
            "instances": 43,
        },
    }
    assert cache_control == "no-store"
    assert probe.calls == 1


def test_api_01_openapi_declares_stable_typed_200_and_503_contracts() -> None:
    application = create_app(Settings(_env_file=None), readiness_probe=RecordingProbe((_report(),)))
    operation = application.openapi()["paths"]["/health/ready"]["get"]

    assert operation["operationId"] == "getHealthReadiness"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadyHealth"
    }
    assert operation["responses"]["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadyHealth"
    }


@pytest.mark.asyncio
async def test_api_01_readiness_is_recomputed_and_not_cached() -> None:
    failure = {
        ReadinessCheckName.DATABASE: ReadinessCode.DATABASE_UNAVAILABLE,
    }
    probe = RecordingProbe((_report(), _report(failure)))
    app = _app(probe)

    first = await _request(app, "/health/ready")
    second = await _request(app, "/health/ready")

    assert (first[0], first[1]["status"], first[2]) == (200, "ready", "no-store")
    assert (second[0], second[1]["status"], second[2]) == (
        503,
        "not_ready",
        "no-store",
    )
    assert second[1]["checks"] == _expected_checks(failure)
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_api_01_missing_probe_fails_closed_without_breaking_liveness() -> None:
    app = create_app(Settings(_env_file=None), readiness_probe=RecordingProbe((_report(),)))
    del app.state.readiness_probe

    ready = await _request(app, "/health/ready")
    live = await _request(app, "/health/live")

    generic = {name: ReadinessCode.READINESS_UNAVAILABLE for name in READINESS_CHECK_ORDER}
    assert ready == (
        503,
        {
            "status": "not_ready",
            "service": "marketing-agents-api",
            "checks": _expected_checks(generic),
            "catalog": None,
        },
        "no-store",
    )
    assert live == (
        200,
        {"status": "ok", "service": "marketing-agents-api"},
        "no-store",
    )


@pytest.mark.asyncio
async def test_api_01_probe_exception_is_sanitized_and_not_cached() -> None:
    canary = "postgresql://private-user:secret@private-db/path/catalog/private.yaml"
    probe = ThrowingProbe(canary)

    first = await _request(_app(probe), "/health/ready")
    second = await _request(_app(probe), "/health/ready")

    for status_code, body, cache_control in (first, second):
        rendered = str(body)
        assert status_code == 503
        assert body["status"] == "not_ready"
        assert body["catalog"] is None
        assert cache_control == "no-store"
        assert canary not in rendered
        assert "private-user" not in rendered
        assert "secret" not in rendered
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_api_01_timeout_and_malformed_report_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health_routes, "READINESS_PROBE_TIMEOUT_SECONDS", 0.001)
    timed_out = await _request(_app(SlowProbe()), "/health/ready")
    malformed = await _request(_app(MalformedProbe()), "/health/ready")

    assert timed_out[0] == 503
    assert {check["code"] for check in cast(list[dict[str, str]], timed_out[1]["checks"])} == {
        "readiness_timeout"
    }
    assert malformed[0] == 503
    assert {check["code"] for check in cast(list[dict[str, str]], malformed[1]["checks"])} == {
        "readiness_unavailable"
    }


@pytest.mark.asyncio
async def test_api_01_blocking_catalog_io_is_offloaded_and_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    original = readiness_infrastructure._compile_catalog

    def blocking_compile(root: Path) -> object:
        release.wait(timeout=1)
        return original(root)

    monkeypatch.setattr(readiness_infrastructure, "_compile_catalog", blocking_compile)
    monkeypatch.setattr(health_routes, "READINESS_PROBE_TIMEOUT_SECONDS", 0.001)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'absent.db'}",
    )
    try:
        response = await _request(create_app(settings), "/health/ready")
    finally:
        release.set()

    assert response[0] == 503
    assert {check["code"] for check in cast(list[dict[str, str]], response[1]["checks"])} == {
        "readiness_timeout"
    }


@pytest.mark.parametrize(
    ("name", "code"),
    [
        (ReadinessCheckName.DATABASE, ReadinessCode.DATABASE_UNAVAILABLE),
        (
            ReadinessCheckName.MIGRATION,
            ReadinessCode.MIGRATION_VERIFICATION_UNAVAILABLE,
        ),
        (ReadinessCheckName.CATALOG, ReadinessCode.CATALOG_INVALID),
        (
            ReadinessCheckName.PROVIDER_REGISTRY,
            ReadinessCode.PROVIDER_REGISTRY_UNAVAILABLE,
        ),
        (
            ReadinessCheckName.CONNECTOR_REGISTRY,
            ReadinessCode.CONNECTOR_REGISTRY_UNAVAILABLE,
        ),
        (
            ReadinessCheckName.WORKER_SCHEMA,
            ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_api_01_dependency_failure_returns_503_and_liveness_stays_up(
    name: ReadinessCheckName,
    code: ReadinessCode,
) -> None:
    failures = {name: code}
    probe = RecordingProbe((_report(failures),))
    app = _app(probe)

    ready = await _request(app, "/health/ready")
    live = await _request(app, "/health/live")

    assert ready[0] == 503
    assert ready[1]["status"] == "not_ready"
    assert ready[1]["checks"] == _expected_checks(failures)
    assert ready[2] == "no-store"
    assert live[0] == 200
    assert probe.calls == 1


async def _create_worker_schema(path: Path) -> None:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    try:
        async with runtime.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await runtime.dispose()


def _check_by_name(report: ReadinessReport) -> dict[ReadinessCheckName, ReadinessCheck]:
    return {check.name: check for check in report.checks}


@pytest.mark.asyncio
async def test_api_01_file_backed_sqlite_probe_is_read_only_and_restart_stable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "readiness.db"
    await _create_worker_schema(database_path)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path}",
    )
    before_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
    before_files = tuple(sorted(path.name for path in tmp_path.iterdir()))

    first = await LocalReadinessProbe(settings).check()
    second = await LocalReadinessProbe(settings).check()

    after_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
    after_files = tuple(sorted(path.name for path in tmp_path.iterdir()))
    first_checks = _check_by_name(first)
    assert first == second
    assert before_hash == after_hash
    assert before_files == after_files == ("readiness.db",)
    assert first_checks[ReadinessCheckName.DATABASE].status is ReadinessCheckStatus.READY
    assert first_checks[ReadinessCheckName.WORKER_SCHEMA].status is ReadinessCheckStatus.READY
    assert first_checks[ReadinessCheckName.PROVIDER_REGISTRY].status is ReadinessCheckStatus.READY
    assert first_checks[ReadinessCheckName.CONNECTOR_REGISTRY].status is ReadinessCheckStatus.READY
    assert first_checks[ReadinessCheckName.MIGRATION].code is (
        ReadinessCode.MIGRATION_VERIFICATION_UNAVAILABLE
    )
    assert first_checks[ReadinessCheckName.CATALOG].code is (
        ReadinessCode.CATALOG_SEED_VERIFICATION_UNAVAILABLE
    )
    assert first.catalog is not None
    assert first.catalog.content_hash.startswith("catalog-sha256-v1:")
    assert (
        first.catalog.departments,
        first.catalog.functions,
        first.catalog.templates,
        first.catalog.instances,
    ) == (5, 12, 36, 43)


@pytest.mark.asyncio
async def test_api_01_absent_database_is_not_created_by_readiness(tmp_path: Path) -> None:
    database_path = tmp_path / "absent.db"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path}",
    )
    app = create_app(settings)

    assert not database_path.exists()
    live = await _request(app, "/health/live")
    assert not database_path.exists()
    ready = await _request(app, "/health/ready")

    checks = cast(list[dict[str, str]], ready[1]["checks"])
    assert live[0] == 200
    assert ready[0] == 503
    assert checks[0] == {
        "name": "database",
        "status": "not_ready",
        "code": "database_missing",
    }
    assert not database_path.exists()


@pytest.mark.asyncio
async def test_api_01_unwritable_database_directory_has_specific_safe_code(
    tmp_path: Path,
) -> None:
    database_directory = tmp_path / "database"
    database_directory.mkdir()
    database_path = database_directory / "readiness.db"
    await _create_worker_schema(database_path)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path}",
    )
    database_directory.chmod(0o500)
    try:
        report = await LocalReadinessProbe(settings).check()
    finally:
        database_directory.chmod(0o700)

    database = _check_by_name(report)[ReadinessCheckName.DATABASE]
    assert database.status is ReadinessCheckStatus.NOT_READY
    assert database.code is ReadinessCode.DATABASE_DIRECTORY_UNAVAILABLE


@pytest.mark.asyncio
async def test_api_01_wrong_worker_column_types_are_incompatible(tmp_path: Path) -> None:
    database_path = tmp_path / "wrong-types.db"
    await _create_worker_schema(database_path)
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text("DROP TABLE run_step_dependencies"))
            await connection.execute(
                text(
                    """
                    CREATE TABLE run_step_dependencies (
                        step_id INTEGER NOT NULL,
                        dependency_key INTEGER NOT NULL,
                        run_id INTEGER NOT NULL,
                        step_key INTEGER NOT NULL,
                        PRIMARY KEY (step_id, dependency_key),
                        FOREIGN KEY (step_id, run_id, step_key)
                            REFERENCES run_steps (id, run_id, key) ON DELETE RESTRICT,
                        FOREIGN KEY (run_id, dependency_key)
                            REFERENCES run_steps (run_id, key) ON DELETE RESTRICT,
                        CHECK (step_key <> dependency_key)
                    )
                    """
                )
            )
    finally:
        await runtime.dispose()

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path}",
    )
    report = await LocalReadinessProbe(settings).check()
    checks = _check_by_name(report)
    assert checks[ReadinessCheckName.DATABASE].status is ReadinessCheckStatus.READY
    assert checks[ReadinessCheckName.WORKER_SCHEMA].status is (ReadinessCheckStatus.NOT_READY)
    assert checks[ReadinessCheckName.WORKER_SCHEMA].code is (
        ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE
    )


@pytest.mark.asyncio
async def test_api_01_missing_worker_constraints_are_incompatible(tmp_path: Path) -> None:
    database_path = tmp_path / "missing-constraints.db"
    await _create_worker_schema(database_path)
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text("DROP TABLE run_step_dependencies"))
            await connection.execute(
                text(
                    """
                    CREATE TABLE run_step_dependencies (
                        step_id VARCHAR(240) NOT NULL,
                        dependency_key VARCHAR(240) NOT NULL,
                        run_id VARCHAR(240) NOT NULL,
                        step_key VARCHAR(240) NOT NULL,
                        PRIMARY KEY (step_id, dependency_key)
                    )
                    """
                )
            )
    finally:
        await runtime.dispose()

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path}",
    )
    report = await LocalReadinessProbe(settings).check()
    checks = _check_by_name(report)
    assert checks[ReadinessCheckName.DATABASE].status is ReadinessCheckStatus.READY
    assert checks[ReadinessCheckName.WORKER_SCHEMA].code is (
        ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE
    )


@pytest.mark.asyncio
async def test_api_01_local_probe_reports_schema_catalog_and_registry_failures(
    tmp_path: Path,
) -> None:
    empty_database = tmp_path / "empty.db"
    empty_database.touch()
    invalid_catalog_settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{empty_database}",
        catalog_root=tmp_path / "private-catalog-canary",
    )
    invalid_catalog = await LocalReadinessProbe(invalid_catalog_settings).check()
    catalog_checks = _check_by_name(invalid_catalog)
    assert catalog_checks[ReadinessCheckName.DATABASE].status is ReadinessCheckStatus.READY
    assert catalog_checks[ReadinessCheckName.WORKER_SCHEMA].code is (
        ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE
    )
    assert catalog_checks[ReadinessCheckName.CATALOG].code is ReadinessCode.CATALOG_INVALID
    assert catalog_checks[ReadinessCheckName.CONNECTOR_REGISTRY].code is (
        ReadinessCode.CONNECTOR_REGISTRY_UNAVAILABLE
    )
    assert invalid_catalog.catalog is None

    real_adapter_settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{empty_database}",
        llm_provider="unregistered-real-provider",
        allow_external_network=True,
        real_llm_opt_in=True,
        real_llm_api_key="private-provider-key-canary",
    )
    invalid_registry = await LocalReadinessProbe(real_adapter_settings).check()
    registry_checks = _check_by_name(invalid_registry)
    assert registry_checks[ReadinessCheckName.PROVIDER_REGISTRY].code is (
        ReadinessCode.PROVIDER_REGISTRY_UNAVAILABLE
    )
    assert registry_checks[ReadinessCheckName.CONNECTOR_REGISTRY].code is (
        ReadinessCode.CONNECTOR_REGISTRY_UNAVAILABLE
    )
