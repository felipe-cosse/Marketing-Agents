"""Qualify local M3 -> M4 -> M5 before proceeding to API/UI requirement work.

This is current qualification, not proof of historical implementation order.
The complete verifier and AC-15 isolation runner remain separate requirements.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts import verify_local as local

ROOT = Path(__file__).resolve().parents[1]


def gates() -> tuple[local.Gate, ...]:
    """Fixed, whole-file inventories; no selection or command override interface."""
    command = (
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-q",
        "--disable-socket",
        "--allow-unix-socket",
    )
    return (
        local.Gate(
            "m3-domain-persistence",
            (
                *command,
                "tests/unit/domain",
                "tests/integration/db/test_run_01_lifecycle_persistence.py",
                "tests/integration/db/test_del_04_migrations.py",
                "tests/integration/db/test_del_04_catalog_seed.py",
                "tests/integration/db/test_del_04_readiness.py",
                "tests/integration/db/test_del_04_trigger_projection.py",
                "tests/integration/db/test_del_04_postgresql_fixture.py",
                "tests/integration/db/test_del_04_database_cli.py",
            ),
            240,
        ),
        local.Gate(
            "m4-read-runtime-mocks",
            (
                *command,
                "tests/contract/test_del_03_durable_mock_composition.py",
                "tests/contract/test_arch_06_llm_provider.py",
                "tests/contract/test_arch_07_connector_contract_matrix.py",
                "tests/integration/db/test_orch_06_controlled_read_executor.py",
                "tests/integration/db/test_orch_09_audited_step_state.py",
                "tests/integration/runtime/test_del_05_process_composition.py",
                "tests/acceptance/test_exec_04_foundation_restart.py",
            ),
            240,
        ),
        local.Gate(
            "m5-approval-safety",
            (
                *command,
                "tests/acceptance/test_ac_08_dispatch_approval_boundary.py",
                "tests/acceptance/test_ac_09_approval_rejection.py",
                "tests/acceptance/test_ac_11_mock_crash_recovery.py",
            ),
            240,
        ),
    )


def verify(root: Path = ROOT, *, executor: local.GateExecutor | None = None) -> dict[str, Any]:
    """Preserve caller source; any failure or drift stops all later milestones."""
    run = local.execute_gate if executor is None else executor
    completed: list[local.GateResult] = []
    baseline = None
    error = None
    try:
        baseline = local.source_snapshot(root)
        # These can replace loaders/plugins before the mandatory socket guard.
        # Ordinary Make scheduling and pytest selection overrides are removed by
        # the shared environment builder, never propagated to milestone children.
        if any(
            os.environ.get(key)
            for key in (
                "PYTHONPATH",
                "PYTHONHOME",
                "PYTEST_PLUGINS",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            )
        ):
            raise local.VerificationError("unsafe_foundation_environment")
        with tempfile.TemporaryDirectory(prefix="marketing-agents-exec04-") as directory:
            artifact_root = Path(directory).resolve()
            if artifact_root.is_relative_to(root.resolve()):
                raise local.VerificationError("foundation_storage_must_be_outside_source")
            environment = local.gate_environment(artifact_root / ".coverage")
            with local._signals():
                for gate in gates():
                    print(
                        json.dumps(
                            {
                                "milestone": gate.name,
                                "status": "started",
                                "timeout_seconds": gate.timeout_seconds,
                            }
                        ),
                        flush=True,
                    )
                    result = run(gate, root, environment)
                    completed.append(result)
                    print(json.dumps(asdict(result), sort_keys=True), flush=True)
                    if (
                        result.name != gate.name
                        or result.status != "passed"
                        or result.returncode != 0
                    ):
                        error = "foundation_gate_failed"
                        break
                    if local.source_snapshot(root) != baseline:
                        error = "source_drift_detected"
                        break
    except (KeyboardInterrupt, local.VerificationInterrupted):
        error = "verification_interrupted"
    except local.VerificationError as failure:
        error = str(failure)
    except OSError:
        error = "foundation_storage_or_process_error"
    finally:
        if baseline is not None:
            try:
                if local.source_snapshot(root) != baseline:
                    error = "source_drift_detected"
            except local.VerificationError:
                error = "source_snapshot_failed_after_gates"
    return {
        "valid": error is None and len(completed) == 3,
        "error": error,
        "milestones": [asdict(result) for result in completed],
    }


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    result = verify()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
