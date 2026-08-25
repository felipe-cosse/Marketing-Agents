"""Framework-independent readiness report and probe contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ReadinessCheckName(StrEnum):
    DATABASE = "database"
    MIGRATION = "migration"
    CATALOG = "catalog"
    PROVIDER_REGISTRY = "provider_registry"
    CONNECTOR_REGISTRY = "connector_registry"
    WORKER_SCHEMA = "worker_schema"


READINESS_CHECK_ORDER = tuple(ReadinessCheckName)


class ReadinessCheckStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


class ReadinessCode(StrEnum):
    READY = "ready"
    DATABASE_MISSING = "database_missing"
    DATABASE_DIRECTORY_UNAVAILABLE = "database_directory_unavailable"
    DATABASE_UNAVAILABLE = "database_unavailable"
    MIGRATION_VERIFICATION_UNAVAILABLE = "migration_verification_unavailable"
    CATALOG_INVALID = "catalog_invalid"
    CATALOG_SEED_VERIFICATION_UNAVAILABLE = "catalog_seed_verification_unavailable"
    PROVIDER_REGISTRY_UNAVAILABLE = "provider_registry_unavailable"
    CONNECTOR_REGISTRY_UNAVAILABLE = "connector_registry_unavailable"
    WORKER_SCHEMA_INCOMPATIBLE = "worker_schema_incompatible"
    READINESS_UNAVAILABLE = "readiness_unavailable"
    READINESS_TIMEOUT = "readiness_timeout"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: ReadinessCheckName
    status: ReadinessCheckStatus
    code: ReadinessCode

    def __post_init__(self) -> None:
        if type(self.name) is not ReadinessCheckName:
            raise TypeError("readiness check name must use the fixed vocabulary")
        if type(self.status) is not ReadinessCheckStatus:
            raise TypeError("readiness check status must use the fixed vocabulary")
        if type(self.code) is not ReadinessCode:
            raise TypeError("readiness check code must use the fixed vocabulary")
        if (self.status is ReadinessCheckStatus.READY) != (self.code is ReadinessCode.READY):
            raise ValueError("ready status and code must agree")


@dataclass(frozen=True, slots=True)
class CatalogReadinessMetadata:
    content_version: str
    content_hash: str
    departments: int
    functions: int
    templates: int
    instances: int

    def __post_init__(self) -> None:
        if (
            type(self.content_version) is not str
            or not self.content_version
            or self.content_version != self.content_version.strip()
            or len(self.content_version) > 64
        ):
            raise ValueError("catalog content version must be a bounded value")
        prefix = "catalog-sha256-v1:"
        if type(self.content_hash) is not str:
            raise ValueError("catalog content hash must use the catalog SHA-256 format")
        digest = self.content_hash.removeprefix(prefix)
        if (
            not self.content_hash.startswith(prefix)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("catalog content hash must use the catalog SHA-256 format")
        counts = (self.departments, self.functions, self.templates, self.instances)
        if any(type(count) is not int for count in counts) or counts != (5, 12, 36, 43):
            raise ValueError("catalog counts must match the exact v1 contract")


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]
    catalog: CatalogReadinessMetadata | None = None

    def __post_init__(self) -> None:
        if type(self.checks) is not tuple or any(
            type(check) is not ReadinessCheck for check in self.checks
        ):
            raise TypeError("readiness checks must be an exact immutable tuple")
        if tuple(check.name for check in self.checks) != READINESS_CHECK_ORDER:
            raise ValueError("readiness checks must contain the fixed ordered check set")
        if self.catalog is not None and type(self.catalog) is not CatalogReadinessMetadata:
            raise TypeError("catalog readiness metadata must use the exact contract")
        catalog_check = self.checks[READINESS_CHECK_ORDER.index(ReadinessCheckName.CATALOG)]
        if catalog_check.status is ReadinessCheckStatus.READY and self.catalog is None:
            raise ValueError("a ready catalog check requires safe catalog metadata")

    @property
    def ready(self) -> bool:
        return all(check.status is ReadinessCheckStatus.READY for check in self.checks)


class ReadinessProbe(Protocol):
    async def check(self) -> ReadinessReport: ...


def unavailable_readiness_report(code: ReadinessCode) -> ReadinessReport:
    if code not in {ReadinessCode.READINESS_UNAVAILABLE, ReadinessCode.READINESS_TIMEOUT}:
        raise ValueError("generic readiness failure requires a generic failure code")
    return ReadinessReport(
        checks=tuple(
            ReadinessCheck(name, ReadinessCheckStatus.NOT_READY, code)
            for name in READINESS_CHECK_ORDER
        )
    )
