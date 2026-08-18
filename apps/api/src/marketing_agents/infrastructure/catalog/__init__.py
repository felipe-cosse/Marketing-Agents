"""Public catalog compilation boundary."""

from .compiler import compile_catalog, validate_catalog
from .errors import CatalogCompilationError, CatalogIssue
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
    "compile_catalog",
    "validate_catalog",
]
