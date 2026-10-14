"""Deployed HTTP smoke assertions; never prints fixtures, CSRF tokens, or payloads."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_SOCKET = "/var/run/marketing-agents/api.sock"
# Independent deployed-smoke contract: a response cannot redefine which demo
# must cross the approval barrier, or replace it with another read-only demo.
EXPECTED_SCENARIOS = {
    "demo.blog-seo.content-review.v1": 0,
    "demo.community.reminder-draft.v1": 0,
    "demo.email.signup-onboarding.v1": 2,
    "demo.partnerships.application-review.v1": 0,
    "demo.social-media.content-draft.v1": 0,
}


class SmokeFailure(RuntimeError):
    pass


def require(condition: object, code: str) -> None:
    if not condition:
        raise SmokeFailure(code)


class LocalClient:
    def __init__(self, origin: str, timeout: int = 90, *, unix_socket: str | None = None) -> None:
        parsed = urllib.parse.urlsplit(origin)
        require(
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment,
            "smoke_origin_must_be_explicit_loopback",
        )
        self.origin = origin.rstrip("/")
        self.timeout = timeout
        require(unix_socket is None or unix_socket == API_SOCKET, "smoke_requires_fixed_api_socket")

        # Neither proxy environment nor redirects may widen the network boundary.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                raise SmokeFailure("unexpected_http_redirect")

        class UnixConnection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                try:
                    self.sock.connect(unix_socket)
                except BaseException:
                    self.sock.close()
                    raise

        class UnixHandler(urllib.request.HTTPHandler):
            def http_open(self, request):
                return self.do_open(UnixConnection, request)

        handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
        if unix_socket is not None:
            handlers.append(UnixHandler())
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, path: str, *, body: dict | None = None, key: str | None = None) -> dict:
        require(path.startswith("/") and not path.startswith("//"), "unsafe_api_path")
        headers = {"Accept": "application/json"}
        if body is not None:
            session = self.request("/api/v1/session")
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Origin": self.origin,
                    "Sec-Fetch-Site": "same-origin",
                    "X-CSRF-Token": session["csrfToken"],
                }
            )
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.origin + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise SmokeFailure(f"http_status_{exc.code}") from None

    def wait(self, path: str, expected: str) -> dict:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            value = self.request(path)
            if value["state"] == expected:
                return value
            require(value["state"] not in {"failed", "cancelled", "rejected"}, "run_failed")
            time.sleep(0.2)
        raise SmokeFailure("run_deadline_exceeded")

    def ready(self) -> dict:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                return self.request("/health/ready")
            except (SmokeFailure, urllib.error.URLError, TimeoutError):
                time.sleep(0.25)
        raise SmokeFailure("readiness_deadline_exceeded")


def safe_session(client: LocalClient) -> dict:
    client.ready()
    session = client.request("/api/v1/session")
    expected = {
        "authMode": "local",
        "environment": "local",
        "modelMode": "mock",
        "connectorMode": "mock",
        "networkPermission": False,
    }
    require(all(session.get(key) == value for key, value in expected.items()), "unsafe_session")
    catalog = client.request("/api/v1/catalog/hierarchy")
    counts = {
        key: catalog["counts"][key]
        for key in ("departments", "functions", "templates", "instances")
    }
    require(list(counts.values()) == [5, 12, 36, 43], "catalog_count_drift")
    return {"session": expected, "counts": counts}


def action_summary(run: dict) -> list[dict]:
    actions = run.get("external_actions") if type(run) is dict else None
    require(type(actions) is list, "demo_action_summary_invalid")
    fields = ("id", "state", "delivery_attempt_count", "receipt_id")
    require(
        all(
            type(action) is dict
            and all(field in action for field in fields)
            and type(action["id"]) is str
            and bool(action["id"])
            and type(action["state"]) is str
            and type(action["delivery_attempt_count"]) is int
            and action["delivery_attempt_count"] >= 0
            and (
                action["receipt_id"] is None
                or (type(action["receipt_id"]) is str and bool(action["receipt_id"]))
            )
            for action in actions
        ),
        "demo_action_summary_invalid",
    )
    return [{key: action[key] for key in fields} for action in actions]


def validate_scenario_inventory(scenarios: object) -> list[dict]:
    require(type(scenarios) is list and len(scenarios) == 5, "expected_five_demos")
    seen = set()
    for scenario in scenarios:
        require(type(scenario) is dict, "demo_inventory_mismatch")
        scenario_id = scenario.get("id")
        require(
            type(scenario_id) is str
            and scenario_id in EXPECTED_SCENARIOS
            and scenario_id not in seen,
            "demo_inventory_mismatch",
        )
        expected = scenario.get("expected")
        writes = expected.get("externalWrites") if type(expected) is dict else None
        require(
            type(writes) is int and writes == EXPECTED_SCENARIOS[scenario_id],
            "demo_write_classification_mismatch",
        )
        seen.add(scenario_id)
    require(seen == set(EXPECTED_SCENARIOS), "demo_inventory_mismatch")
    return scenarios


def validate_completed_actions(actions: list[dict], expected_writes: int) -> None:
    if not expected_writes:
        require(actions == [], "read_demo_external_actions")
        return
    require(
        len(actions) == 2
        and len({item["id"] for item in actions}) == 2
        and len({item["receipt_id"] for item in actions}) == 2,
        "email_expected_two_receipts",
    )
    require(
        all(
            item["state"] == "succeeded"
            and item["delivery_attempt_count"] == 1
            and item["receipt_id"]
            for item in actions
        ),
        "email_expected_one_call_each",
    )


def validate_replay(replay: object, session: dict) -> dict[str, dict]:
    """Validate the prior local witness before any replay admission request.

    This checks report consistency, not authenticity. The coordinator supplies
    its just-observed fresh-run result; an arbitrary caller can forge JSON.
    """
    require(
        type(replay) is dict
        and replay.get("email_preapproval_zero_calls") is True
        and type(replay.get("restart_replay")) is bool
        and replay.get("session") == session["session"]
        and replay.get("counts") == session["counts"]
        and replay.get("ok", True) is True,
        "invalid_demo_replay_witness",
    )
    summaries = replay.get("demos")
    require(type(summaries) is list and len(summaries) == 5, "invalid_demo_replay_witness")
    indexed = {}
    run_ids = set()
    for summary in summaries:
        require(type(summary) is dict, "invalid_demo_replay_witness")
        scenario_id = summary.get("scenario")
        run_id = summary.get("run_id")
        require(
            type(scenario_id) is str
            and scenario_id in EXPECTED_SCENARIOS
            and scenario_id not in indexed
            and type(run_id) is str
            and bool(run_id)
            and run_id not in run_ids
            and summary.get("state") == "completed",
            "invalid_demo_replay_witness",
        )
        actions = action_summary({"external_actions": summary.get("actions")})
        validate_completed_actions(actions, EXPECTED_SCENARIOS[scenario_id])
        indexed[scenario_id] = summary
        run_ids.add(run_id)
    require(set(indexed) == set(EXPECTED_SCENARIOS), "invalid_demo_replay_witness")
    return indexed


def exercise_demos(client: LocalClient, *, replay: dict | None = None) -> dict:
    result = safe_session(client)
    inventory = client.request("/api/v1/demo-scenarios")
    scenarios = validate_scenario_inventory(
        inventory.get("items") if type(inventory) is dict else None
    )
    previous_summaries = {} if replay is None else validate_replay(replay, result)
    email_before_zero = False
    email_partial_zero = False
    email_completed_once = False
    summaries = []
    for scenario in scenarios:
        scenario_id = scenario["id"]
        receipt = client.request(
            f"/api/v1/demo-scenarios/{scenario_id}/runs",
            body={},
            key="del-05-clean-" + hashlib.sha256(scenario_id.encode()).hexdigest(),
        )
        run_path = receipt["runUrl"]
        previous = previous_summaries.get(scenario_id)
        if previous is not None:
            require(receipt["runId"] == previous["run_id"], "restart_duplicate_run")
        mutating = EXPECTED_SCENARIOS[scenario_id] > 0
        if mutating and replay is None:
            run = client.wait(run_path, "awaiting_approval")
            pending_actions = action_summary(run)
            pending_action_ids = {action["id"] for action in pending_actions}
            require(
                len(pending_actions) == len(pending_action_ids) == 2,
                "email_expected_two_actions",
            )
            pending_approvals = run.get("pending_approvals")
            require(
                type(pending_approvals) is list
                and len(pending_approvals) == 2
                and all(
                    type(approval) is dict
                    and type(approval.get("approval_url")) is str
                    and bool(approval["approval_url"])
                    for approval in pending_approvals
                )
                and len({approval["approval_url"] for approval in pending_approvals}) == 2,
                "email_expected_two_approvals",
            )
            require(
                all(
                    action["delivery_attempt_count"] == 0 and action["receipt_id"] is None
                    for action in pending_actions
                ),
                "email_preapproval_calls",
            )
            email_before_zero = True
            approvals = tuple(pending_approvals)
            for index, pending in enumerate(approvals):
                approval = client.request(pending["approval_url"])
                client.request(
                    pending["approval_url"] + "/approve",
                    body={
                        "expected_generation": approval["generation"],
                        "expected_payload_hash": approval["payload_hash"],
                    },
                )
                if index == 0:
                    after_one = client.request(run_path)
                    require(after_one["state"] == "awaiting_approval", "email_partial_release")
                    after_one_actions = action_summary(after_one)
                    require(
                        len(after_one_actions) == 2
                        and {action["id"] for action in after_one_actions} == pending_action_ids
                        and all(
                            action["delivery_attempt_count"] == 0 and action["receipt_id"] is None
                            for action in after_one_actions
                        ),
                        "email_calls_after_one_approval",
                    )
                    email_partial_zero = True
        run = client.wait(run_path, "completed")
        require(bool(run["artifact_summaries"]), "demo_missing_artifact")
        actions = action_summary(run)
        validate_completed_actions(actions, EXPECTED_SCENARIOS[scenario_id])
        if mutating:
            if replay is None:
                require(
                    {action["id"] for action in actions} == pending_action_ids,
                    "email_completed_action_identity_changed",
                )
            email_completed_once = True
        if previous is not None:
            require(actions == previous["actions"], "restart_duplicate_action")
        summaries.append(
            {
                "scenario": scenario_id,
                "run_id": receipt["runId"],
                "state": run["state"],
                "actions": actions,
            }
        )
    result["demos"] = summaries
    result["email_preapproval_zero_calls"] = (
        (email_before_zero and email_partial_zero)
        if replay is None
        else replay["email_preapproval_zero_calls"]
    ) and email_completed_once
    require(
        result["email_preapproval_zero_calls"],
        "email_approval_observation_incomplete",
    )
    result["restart_replay"] = replay is not None
    return result


def database_snapshot(database: Path, key: Path) -> dict:
    """Read-only whole-database logical hash and the non-secret paired identity."""
    from marketing_agents.security.digest_key import (
        digest_key_fingerprint,
        load_or_create_digest_key,
    )

    require(database.is_file() and not database.is_symlink(), "database_missing_or_unsafe")
    require(key.is_file() and not key.is_symlink(), "key_missing_or_unsafe")
    fingerprint = digest_key_fingerprint(
        load_or_create_digest_key(key, persistent_state_exists=True)
    )
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        identity = connection.execute(
            "SELECT format_version, key_fingerprint FROM local_runtime_identity"
        ).fetchall()
        require(identity == [(1, fingerprint)], "database_key_pair_mismatch")
        logical_hash = hashlib.sha256()
        for line in connection.iterdump():
            logical_hash.update(line.encode())
            logical_hash.update(b"\n")
        counts = {}
        for table in (
            "connector_action_receipts",
            "external_actions",
            "webhook_receipts",
            "schedule_occurrences",
        ):
            counts[table] = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    return {
        "format_version": 1,
        "key_fingerprint": fingerprint,
        "logical_database_sha256": logical_hash.hexdigest(),
        "counts": counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ready", "demos", "replay"):
        command = sub.add_parser(name)
        command.add_argument("--origin", required=True)
        if name == "replay":
            command.add_argument("--previous", type=Path, required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--database", type=Path, required=True)
    snapshot.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            result = database_snapshot(args.database, args.key)
        else:
            client = LocalClient(args.origin)
            result = (
                safe_session(client)
                if args.command == "ready"
                else exercise_demos(
                    client,
                    replay=json.loads(args.previous.read_text())
                    if args.command == "replay"
                    else None,
                )
            )
    except (SmokeFailure, KeyError, ValueError, OSError) as exc:
        code = str(exc) if isinstance(exc, SmokeFailure) else "smoke_contract_failure"
        print(json.dumps({"ok": False, "code": code}))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
