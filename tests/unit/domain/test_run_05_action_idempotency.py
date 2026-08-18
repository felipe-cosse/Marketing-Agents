"""RUN-05: stable seven-field external-action idempotency contract."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest
from marketing_agents.domain.action_hash import ExternalActionKeyMaterial
from marketing_agents.domain.action_idempotency import (
    ACTION_IDEMPOTENCY_KEY_DOMAIN,
    ACTION_IDEMPOTENCY_PREFIX,
    derive_external_action_idempotency_key,
)


def _material() -> ExternalActionKeyMaterial:
    return ExternalActionKeyMaterial(
        run_id="run.run-05.0001",
        plan_hash="a" * 64,
        proposal_revision=1,
        step_key="deliver-campaign",
        action_type="messaging.send-message",
        binding_id="mock.community.default",
        semantic_action_hash="b" * 64,
    )


def test_run_05_key_is_domain_separated_restart_stable_and_golden() -> None:
    material = _material()
    first = derive_external_action_idempotency_key(material)
    second = derive_external_action_idempotency_key(_material())

    assert ACTION_IDEMPOTENCY_KEY_DOMAIN.endswith(b"\x00")
    assert first == second
    assert first.startswith(ACTION_IDEMPOTENCY_PREFIX)
    assert len(first) == len(ACTION_IDEMPOTENCY_PREFIX) + 64
    assert first == (
        "action-idempotency-v1:9c3a98a25bb9b6ceb5e1445fe9c081721d09f6515f45db223cae1f1b5cacbeec"
    )


@pytest.mark.parametrize(
    ("field_name", "changed"),
    [
        ("run_id", "run.run-05.0002"),
        ("plan_hash", "c" * 64),
        ("proposal_revision", 2),
        ("step_key", "deliver-campaign-v2"),
        ("action_type", "crm.upsert-contact"),
        ("binding_id", "mock.crm.default"),
        ("semantic_action_hash", "d" * 64),
    ],
)
def test_run_05_every_stable_material_field_changes_the_key(
    field_name: str, changed: str | int
) -> None:
    baseline = _material()
    mutated = replace(baseline, **{field_name: changed})

    assert derive_external_action_idempotency_key(mutated) != (
        derive_external_action_idempotency_key(baseline)
    )


@pytest.mark.parametrize(
    "imports",
    [
        "import marketing_agents.domain.approval; import marketing_agents.domain.entities",
        "import marketing_agents.domain.action_hash; import marketing_agents.domain.entities",
        "import marketing_agents.domain.entities; import marketing_agents.domain.action_hash",
    ],
)
def test_run_05_action_hash_and_entities_are_cold_import_order_independent(
    imports: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", imports],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
