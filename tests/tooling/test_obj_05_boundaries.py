"""OBJ-05 architecture witness controls; these are not runtime qualification."""

from pathlib import Path

import pytest

from scripts.verify_obj_05_boundaries import verify, violations

NEUTRAL = """
from .bindings import ConnectorBindingSource
class RegistryConnectorReadAdapter:
    def __init__(self, bundle: ConnectorBindingSource):
        self._bindings = bundle.binding_registry
class RegistryConnectorWriteGateway:
    def __init__(self, bundle: ConnectorBindingSource):
        self._bindings = bundle.binding_registry
"""


def test_obj_05_actual_bridge_has_no_concrete_provider_dependency() -> None:
    verify(Path(__file__).resolve().parents[2])


def test_obj_05_neutral_source_passes_architecture_gate() -> None:
    assert violations(NEUTRAL) == []


@pytest.mark.parametrize(
    "extra",
    (
        "from .mock.families import MockConnectorBundle as Bundle",
        "import marketing_agents.infrastructure.adapters.connectors.mock.families as bundle",
        "from .real.vendor import Vendor",
        "ledger = bundle.ledger",
        "ledger = self._bundle.ledger",
        'output(provider_mode="mock")',
        'output(provider_name="vendor")',
        'output(provider_version="v1")',
    ),
)
def test_obj_05_concrete_coupling_controls_fail(extra: str) -> None:
    assert violations(NEUTRAL + "\n" + extra)


def test_obj_05_missing_bridge_or_concrete_annotation_fails() -> None:
    assert violations("")
    assert violations(NEUTRAL.replace("bundle: ConnectorBindingSource", "bundle: ConcreteBundle"))
