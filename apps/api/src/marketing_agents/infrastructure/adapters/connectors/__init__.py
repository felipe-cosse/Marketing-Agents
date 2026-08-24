"""Typed connector composition and deterministic offline implementations."""

from .dispatch import RegistryConnectorReadAdapter, RegistryConnectorWriteGateway
from .registry import (
    ConnectorBundleConfigurationError,
    ConnectorOperationRegistry,
    build_connector_registry,
)

__all__ = [
    "ConnectorBundleConfigurationError",
    "ConnectorOperationRegistry",
    "RegistryConnectorReadAdapter",
    "RegistryConnectorWriteGateway",
    "build_connector_registry",
]
