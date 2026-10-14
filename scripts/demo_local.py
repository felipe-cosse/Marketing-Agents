"""Submit one committed synthetic demo preset; never approve or dispatch actions."""

from __future__ import annotations

import argparse
import http.client
import json
import re
from pathlib import Path

from scripts.del_05_runtime_smoke import LocalClient, SmokeFailure, require, safe_session

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "demo.social-media.content-draft.v1": "social-content-draft.json",
    "demo.blog-seo.content-review.v1": "blog-content-review.json",
    "demo.email.signup-onboarding.v1": "email-signup.json",
    "demo.community.reminder-draft.v1": "community-reminder-draft.json",
    "demo.partnerships.application-review.v1": "partnership-application-review.json",
}
EMAIL_SCENARIO = "demo.email.signup-onboarding.v1"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,239}")
_RETRY_KEY = re.compile(r"[\x21-\x7e]{8,240}")


def submit_preset(client: LocalClient, scenario_id: str, idempotency_key: str) -> dict:
    """Check the local mock installation and source preset before one admission."""
    require(scenario_id in SCENARIOS, "demo_scenario_not_supported")
    require(_RETRY_KEY.fullmatch(idempotency_key) is not None, "demo_retry_key_invalid")
    fixture = json.loads((ROOT / "tests/fixtures/demos" / SCENARIOS[scenario_id]).read_text())
    require(type(fixture) is dict, "demo_fixture_invalid")
    safe_session(client)
    scenarios = client.request("/api/v1/demo-scenarios")["items"]
    require(type(scenarios) is list and len(scenarios) == 5, "demo_registry_drift")
    require({item["id"] for item in scenarios} == set(SCENARIOS), "demo_registry_drift")
    scenario = next(item for item in scenarios if item["id"] == scenario_id)
    email = scenario_id == EMAIL_SCENARIO
    require(
        scenario["mode"] == "deterministic_mock"
        and scenario["effect"] == ("mutating" if email else "read_only")
        and scenario["expected"]["approvals"] == (2 if email else 0)
        and scenario["expected"]["externalWrites"] == (2 if email else 0)
        and scenario["preset"] == fixture,
        "demo_preset_drift",
    )
    # All source fields are bound to this request; no arbitrary input file/override
    # option, campaign brief, action decision, or connector invocation is exposed.
    receipt = client.request(
        f"/api/v1/demo-scenarios/{scenario_id}/runs",
        body={"overrides": fixture},
        key=idempotency_key,
    )
    require(
        receipt["status"] == "accepted"
        and receipt["scenarioId"] == scenario_id
        and receipt["disposition"] in {"created", "replayed"}
        and receipt["executionMode"] == ("mock_execute" if email else "dry_run")
        and type(receipt["runId"]) is str
        and _SAFE_ID.fullmatch(receipt["runId"]) is not None
        and type(receipt["workId"]) is str
        and _SAFE_ID.fullmatch(receipt["workId"]) is not None
        and receipt["runUrl"] == f"/api/v1/runs/{receipt['runId']}",
        "demo_receipt_invalid",
    )
    return {
        "ok": True,
        "status": "accepted_only",
        "scenario": scenario_id,
        "disposition": receipt["disposition"],
        "run_id": receipt["runId"],
        "work_id": receipt["workId"],
        "run_url": receipt["runUrl"],
        "approval_policy": "manual_review_required" if email else "no_external_actions",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="explicit http://127.0.0.1:<port>")
    parser.add_argument("--scenario", required=True, choices=tuple(SCENARIOS))
    parser.add_argument("--idempotency-key", required=True, help="8-240 visible ASCII characters")
    args = parser.parse_args(argv)
    try:
        result = submit_preset(LocalClient(args.origin), args.scenario, args.idempotency_key)
    except (
        SmokeFailure,
        KeyError,
        ValueError,
        TypeError,
        OSError,
        StopIteration,
        http.client.HTTPException,
    ):
        # Do not echo exception text, server content, fixture data, tokens, or keys.
        print(json.dumps({"ok": False, "code": "demo_submission_failed"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
