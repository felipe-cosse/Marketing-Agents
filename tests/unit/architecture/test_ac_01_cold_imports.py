"""AC-01 catalog constraint imports work from a fresh, offline interpreter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
API_SOURCE = ROOT / "apps" / "api" / "src"
NETWORK_EVENTS = {
    "socket.bind",
    "socket.connect",
    "socket.connect_ex",
    "socket.getaddrinfo",
    "socket.gethostbyaddr",
    "socket.gethostbyname",
    "socket.listen",
    "socket.sendto",
}


def run_fresh(source: str) -> subprocess.CompletedProcess[str]:
    preamble = f"""
import sys
sys.path[:0] = [{str(API_SOURCE)!r}, {str(ROOT)!r}]

def deny_network(event, _arguments):
    if event in {NETWORK_EVENTS!r}:
        raise AssertionError("cold import attempted network access")

sys.addaudithook(deny_network)
"""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", preamble + source],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "imports",
    [
        """
import marketing_agents.infrastructure.instance_configuration_constraints
import marketing_agents.infrastructure.catalog.instance_configuration_seed
""",
        """
import marketing_agents.infrastructure.catalog.instance_configuration_seed
import marketing_agents.infrastructure.instance_configuration_constraints
""",
    ],
)
def test_ac_01_catalog_constraints_are_cold_import_order_independent(imports: str) -> None:
    completed = run_fresh(imports + '\nprint("cold-import-ok")\n')

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "cold-import-ok\n"


def test_ac_01_deferred_catalog_import_preserves_exact_runtime_type_guards() -> None:
    completed = run_fresh(
        """
from marketing_agents.infrastructure.instance_configuration_constraints import (
    CompiledCatalogInstanceConfigurationConstraintProvider,
    InstanceConfigurationConstraintError,
    registered_mock_bindings,
)

try:
    CompiledCatalogInstanceConfigurationConstraintProvider(object())
except ValueError as error:
    assert str(error) == "configuration constraint provider requires one compiled catalog"
else:
    raise AssertionError("provider accepted a non-catalog object")

try:
    registered_mock_bindings(object(), "inst.invalid")
except InstanceConfigurationConstraintError as error:
    assert error.code == "catalog_invalid"
else:
    raise AssertionError("constraint lookup accepted a non-catalog object")

print("type-guards-ok")
"""
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "type-guards-ok\n"


def test_ac_01_replay_prepare_crosses_previous_cold_import_failure_boundary() -> None:
    completed = run_fresh(
        """
import asyncio
import sys
import types

from scripts import del_05_replay_fixture as fixture

class ControlledStop(RuntimeError):
    pass

async def stop_after_imports(_settings):
    raise ControlledStop("runtime boundary reached")

composition = types.ModuleType("marketing_agents.workers.runtime.composition")
composition.build_runtime = stop_after_imports
sys.modules[composition.__name__] = composition
fixture.fixture_settings = lambda: object()

try:
    asyncio.run(fixture.prepare())
except ControlledStop as error:
    assert str(error) == "runtime boundary reached"
else:
    raise AssertionError("fixture prepare did not reach the controlled runtime boundary")

print("fixture-entrypoint-ok")
"""
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "fixture-entrypoint-ok\n"


def test_ac_01_fresh_subprocess_environment_has_no_cloud_credentials() -> None:
    completed = run_fresh(
        """
import os

for name in os.environ:
    assert not name.startswith(("AWS_", "AZURE_", "GOOGLE_"))
    assert name not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}

print("credential-free-ok")
"""
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "credential-free-ok\n"
