"""Pytest-wide external network denial; loopback remains available for local clients."""

from __future__ import annotations

import pytest

from tests.network.python_network_guard import deny_external_network


@pytest.fixture(autouse=True)
def _safe_11_no_external_network():
    """Requirement SAFE-11: every Python pytest runs with socket/DNS denial."""
    with deny_external_network():
        yield
