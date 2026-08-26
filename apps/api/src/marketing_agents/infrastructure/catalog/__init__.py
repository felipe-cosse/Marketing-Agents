"""Public catalog compilation boundary."""

from .compiler import compile_catalog, validate_catalog
from .errors import CatalogCompilationError, CatalogIssue
from .instance_configuration_seed import (
    InstanceConfigurationSeedError,
    InstanceConfigurationSeedResult,
    catalog_instance_configuration_defaults,
    seed_instance_configurations,
)
from .models import (
    MARKETING_AGENTS_V1_CONTRACT,
    CatalogContract,
    CatalogValidationReport,
    CompiledCatalog,
)

__all__ = [
    "MARKETING_AGENTS_V1_CONTRACT",
    "CatalogCompilationError",
    "CatalogContract",
    "CatalogIssue",
    "CatalogValidationReport",
    "CompiledCatalog",
    "InstanceConfigurationSeedError",
    "InstanceConfigurationSeedResult",
    "catalog_instance_configuration_defaults",
    "compile_catalog",
    "seed_instance_configurations",
    "validate_catalog",
]
