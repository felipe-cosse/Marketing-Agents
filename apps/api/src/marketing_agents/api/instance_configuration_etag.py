"""Strong revision validators for one agent-instance configuration resource."""

from __future__ import annotations


def instance_configuration_etag(configuration_revision: int) -> str:
    """Return an opaque strong ETag that changes on every material configuration update."""

    if type(configuration_revision) is not int or configuration_revision < 1:
        raise ValueError("configuration revision must be a positive integer")
    return f'"instance-configuration-v1-{configuration_revision}"'
