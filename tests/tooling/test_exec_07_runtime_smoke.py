"""EXEC-07 cannot substitute demos or invent an Email approval observation."""

from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.del_05_runtime_smoke import EXPECTED_SCENARIOS, SmokeFailure, exercise_demos

EMAIL = "demo.email.signup-onboarding.v1"
EXPECTED = {
    "demo.blog-seo.content-review.v1": 0,
    "demo.community.reminder-draft.v1": 0,
    EMAIL: 2,
    "demo.partnerships.application-review.v1": 0,
    "demo.social-media.content-draft.v1": 0,
}
SESSION = {
    "authMode": "local",
    "environment": "local",
    "modelMode": "mock",
    "connectorMode": "mock",
    "networkPermission": False,
}
COUNTS = {"departments": 5, "functions": 12, "templates": 36, "instances": 43}


def actions(*, completed=False):
    return [
        {
            "id": f"action-{index}",
            "state": "succeeded" if completed else "awaiting_approval",
            "delivery_attempt_count": 1 if completed else 0,
            "receipt_id": f"receipt-{index}" if completed else None,
        }
        for index in range(2)
    ]


class SmokeClient:
    """Fixed same-process responses, never an HTTP server or real connector."""

    def __init__(self):
        self.inventory = {
            "items": [
                {"id": scenario, "expected": {"externalWrites": count}}
                for scenario, count in EXPECTED.items()
            ]
        }
        self.mutations = []
        self.run_ids = {scenario: f"run-{index}" for index, scenario in enumerate(EXPECTED)}
        self.before = {
            "state": "awaiting_approval",
            "external_actions": actions(),
            "pending_approvals": [
                {"approval_url": f"/api/v1/approvals/{index}"} for index in range(2)
            ],
        }
        self.partial = deepcopy(self.before)
        self.completed = {
            "state": "completed",
            "external_actions": actions(completed=True),
            "artifact_summaries": [{"id": "artifact-email"}],
        }

    def ready(self):
        return {"ok": True}

    def request(self, path, *, body=None, key=None):
        if body is not None:
            self.mutations.append((path, deepcopy(body), key))
        if path == "/api/v1/session":
            return deepcopy(SESSION)
        if path == "/api/v1/catalog/hierarchy":
            return {"counts": deepcopy(COUNTS)}
        if path == "/api/v1/demo-scenarios":
            return deepcopy(self.inventory)
        if path.startswith("/api/v1/demo-scenarios/"):
            scenario = path.removeprefix("/api/v1/demo-scenarios/").removesuffix("/runs")
            return {
                "runId": self.run_ids[scenario],
                "runUrl": f"/api/v1/runs/{self.run_ids[scenario]}",
            }
        if path.startswith("/api/v1/approvals/"):
            return {"generation": 1, "payload_hash": "synthetic-hash"}
        assert path == f"/api/v1/runs/{self.run_ids[EMAIL]}"
        return deepcopy(self.partial)

    def wait(self, path, expected):
        if expected == "awaiting_approval":
            assert path == f"/api/v1/runs/{self.run_ids[EMAIL]}"
            return deepcopy(self.before)
        assert expected == "completed"
        if path == f"/api/v1/runs/{self.run_ids[EMAIL]}":
            return deepcopy(self.completed)
        return {"state": "completed", "external_actions": [], "artifact_summaries": [{}]}


def test_exec_07_independent_inventory_matches_actual_registered_definitions():
    from marketing_agents.demos.registry import build_demo_scenario_registry

    registry = build_demo_scenario_registry()
    assert (
        EXPECTED_SCENARIOS
        == EXPECTED
        == {scenario.id: scenario.expected_external_actions for scenario in registry.list()}
    )


def test_exec_07_complete_fresh_smoke_observes_both_approvals_and_preserves_report_shape():
    client = SmokeClient()
    # Response order is not a product contract; exact unique membership is.
    client.inventory["items"].reverse()
    report = exercise_demos(client)
    assert set(report) == {
        "session",
        "counts",
        "demos",
        "email_preapproval_zero_calls",
        "restart_replay",
    }
    assert report["email_preapproval_zero_calls"] is True
    assert report["restart_replay"] is False
    assert {entry["scenario"] for entry in report["demos"]} == set(EXPECTED)
    assert all(entry["state"] == "completed" for entry in report["demos"])
    assert len(client.mutations) == 7
    approvals = [entry for entry in client.mutations if entry[0].endswith("/approve")]
    assert [entry[0] for entry in approvals] == [
        "/api/v1/approvals/0/approve",
        "/api/v1/approvals/1/approve",
    ]
    assert all(
        entry[1] == {"expected_generation": 1, "expected_payload_hash": "synthetic-hash"}
        for entry in approvals
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "not_object",
        "missing_items",
        "not_list",
        "missing_demo",
        "extra_demo",
        "duplicate_read_demo",
        "unknown_demo",
        "malformed_demo",
        "unhashable_id",
        "missing_expected",
        "malformed_expected",
        "email_read_only",
        "read_demo_mutating",
        "email_one_write",
        "boolean_write_count",
        "floating_write_count",
        "string_write_count",
    ),
)
def test_exec_07_invalid_inventory_fails_before_any_mutation(mutation):
    client = SmokeClient()
    items = client.inventory["items"]
    email = next(item for item in items if item["id"] == EMAIL)
    if mutation == "not_object":
        client.inventory = []
    elif mutation == "missing_items":
        client.inventory = {}
    elif mutation == "not_list":
        client.inventory["items"] = {}
    elif mutation == "missing_demo":
        items.pop()
    elif mutation == "extra_demo":
        items.append(deepcopy(items[0]))
    elif mutation == "duplicate_read_demo":
        items[items.index(email)] = deepcopy(items[0])
    elif mutation == "unknown_demo":
        email["id"] = "demo.synthetic.unexpected.v1"
    elif mutation == "malformed_demo":
        items[-1] = None
    elif mutation == "unhashable_id":
        email["id"] = []
    elif mutation == "missing_expected":
        del email["expected"]
    elif mutation == "malformed_expected":
        email["expected"] = []
    else:
        target = items[0] if mutation == "read_demo_mutating" else email
        target["expected"]["externalWrites"] = {
            "email_read_only": 0,
            "read_demo_mutating": 2,
            "email_one_write": 1,
            "boolean_write_count": True,
            "floating_write_count": 2.0,
            "string_write_count": "2",
        }[mutation]
    with pytest.raises(SmokeFailure):
        exercise_demos(client)
    assert client.mutations == []


@pytest.mark.parametrize(
    "mutation",
    (
        "before_calls",
        "before_receipt",
        "before_duplicate_id",
        "before_boolean_count",
        "duplicate_approval",
        "partial_calls",
        "partial_receipt",
        "partial_empty_actions",
        "partial_swapped_action",
        "partial_released",
        "completed_calls",
        "completed_missing_receipt",
        "completed_duplicate_receipt",
        "completed_duplicate_action",
        "completed_swapped_action",
        "completed_boolean_count",
        "completed_missing_artifact",
    ),
)
def test_exec_07_incomplete_email_observations_cannot_report_success(mutation):
    client = SmokeClient()
    stage, change = mutation.split("_", 1)
    if mutation == "duplicate_approval":
        client.before["pending_approvals"][1] = deepcopy(client.before["pending_approvals"][0])
    elif change == "missing_artifact":
        client.completed["artifact_summaries"] = []
    elif change == "released":
        client.partial["state"] = "completed"
    elif change == "empty_actions":
        client.partial["external_actions"] = []
    else:
        observed = getattr(client, stage)["external_actions"]
        if change == "calls":
            observed[0]["delivery_attempt_count"] = 2 if stage == "completed" else 1
        elif change in {"receipt", "missing_receipt"}:
            observed[0]["receipt_id"] = None if stage == "completed" else "unexpected-receipt"
        elif change in {"duplicate_id", "duplicate_action"}:
            observed[1]["id"] = observed[0]["id"]
        elif change == "duplicate_receipt":
            observed[1]["receipt_id"] = observed[0]["receipt_id"]
        elif change == "swapped_action":
            observed[0]["id"] = "different-action"
        elif change == "boolean_count":
            observed[0]["delivery_attempt_count"] = stage == "completed"
        else:
            raise AssertionError("unknown fixture mutation")
    with pytest.raises(SmokeFailure):
        exercise_demos(client)


def test_exec_07_replay_retains_observed_proof_and_does_not_submit_another_approval():
    first = exercise_demos(SmokeClient())
    client = SmokeClient()
    result = exercise_demos(client, replay={"ok": True, **first})
    assert result == {**first, "restart_replay": True}
    assert len(client.mutations) == 5
    assert all(path.endswith("/runs") for path, _, _ in client.mutations)
    assert exercise_demos(SmokeClient(), replay=result) == result


@pytest.mark.parametrize(
    "mutation",
    (
        "not_object",
        "false_proof",
        "numeric_proof",
        "missing_proof",
        "malformed_replay_flag",
        "failed_report",
        "session_drift",
        "count_drift",
        "not_list",
        "missing_demo",
        "duplicate_demo",
        "unknown_demo",
        "malformed_demo",
        "unhashable_scenario",
        "missing_run_id",
        "duplicate_run_id",
        "unhashable_run_id",
        "not_completed",
        "not_action_list",
        "malformed_action",
        "missing_action_field",
        "boolean_action_count",
        "duplicate_receipt",
        "missing_receipt",
        "read_action",
        "extra_email_action",
    ),
)
def test_exec_07_invalid_replay_witness_fails_before_any_mutation(mutation):
    previous = exercise_demos(SmokeClient())
    email = next(item for item in previous["demos"] if item["scenario"] == EMAIL)
    if mutation == "not_object":
        previous = []
    elif mutation in {"false_proof", "numeric_proof"}:
        previous["email_preapproval_zero_calls"] = False if mutation == "false_proof" else 1
    elif mutation == "missing_proof":
        del previous["email_preapproval_zero_calls"]
    elif mutation == "malformed_replay_flag":
        previous["restart_replay"] = "false"
    elif mutation == "failed_report":
        previous["ok"] = False
    elif mutation == "session_drift":
        previous["session"]["modelMode"] = "real"
    elif mutation == "count_drift":
        previous["counts"]["instances"] = 42
    elif mutation == "not_list":
        previous["demos"] = {}
    elif mutation == "missing_demo":
        previous["demos"].pop()
    elif mutation == "duplicate_demo":
        previous["demos"][-1] = deepcopy(previous["demos"][0])
    elif mutation == "unknown_demo":
        email["scenario"] = "demo.unexpected.v1"
    elif mutation == "malformed_demo":
        previous["demos"][-1] = None
    elif mutation == "unhashable_scenario":
        email["scenario"] = []
    elif mutation == "missing_run_id":
        del email["run_id"]
    elif mutation == "duplicate_run_id":
        email["run_id"] = previous["demos"][0]["run_id"]
    elif mutation == "unhashable_run_id":
        email["run_id"] = []
    elif mutation == "not_completed":
        email["state"] = "awaiting_approval"
    elif mutation == "not_action_list":
        email["actions"] = {}
    elif mutation == "malformed_action":
        email["actions"][0] = None
    elif mutation == "missing_action_field":
        del email["actions"][0]["receipt_id"]
    elif mutation == "boolean_action_count":
        email["actions"][0]["delivery_attempt_count"] = True
    elif mutation == "duplicate_receipt":
        email["actions"][1]["receipt_id"] = email["actions"][0]["receipt_id"]
    elif mutation == "missing_receipt":
        email["actions"][0]["receipt_id"] = None
    elif mutation == "read_action":
        previous["demos"][0]["actions"] = deepcopy(email["actions"])
    elif mutation == "extra_email_action":
        email["actions"].append(deepcopy(email["actions"][0]))
    else:
        raise AssertionError("unknown fixture mutation")
    client = SmokeClient()
    with pytest.raises(SmokeFailure):
        exercise_demos(client, replay=previous)
    assert client.mutations == []


@pytest.mark.parametrize("changed", ("run", "action", "receipt"))
def test_exec_07_replay_rejects_changed_runtime_identity(changed):
    first = exercise_demos(SmokeClient())
    client = SmokeClient()
    if changed == "run":
        client.run_ids[EMAIL] = "different-run"
    else:
        client.completed["external_actions"][0]["id" if changed == "action" else "receipt_id"] = (
            "different-id"
        )
    with pytest.raises(SmokeFailure, match="restart_duplicate"):
        exercise_demos(client, replay=first)
