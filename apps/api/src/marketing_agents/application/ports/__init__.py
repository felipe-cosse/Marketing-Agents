"""Provider- and transport-independent application ports."""

from .read_adapter import (
    ReadAdapter,
    ReadAdapterCancelledError,
    ReadAdapterContract,
    ReadAdapterError,
    ReadAdapterPermanentError,
    ReadAdapterRequest,
    ReadAdapterResult,
    ReadAdapterTransientError,
)

__all__ = [
    "ReadAdapter",
    "ReadAdapterCancelledError",
    "ReadAdapterContract",
    "ReadAdapterError",
    "ReadAdapterPermanentError",
    "ReadAdapterRequest",
    "ReadAdapterResult",
    "ReadAdapterTransientError",
]
