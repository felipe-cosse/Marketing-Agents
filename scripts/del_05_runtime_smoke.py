"""Deployed HTTP smoke assertions; never prints fixtures, CSRF tokens, or payloads."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import json
import os
import re
import socket
import stat
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


def readonly_database_engine(database: Path | str):
    """Own an explicitly read-only engine; never create a missing SQLite file."""
    from marketing_agents.infrastructure.db.url import parse_database_url
    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import create_async_engine

    url = (
        URL.create("sqlite+aiosqlite", database=str(database.absolute()))
        if isinstance(database, Path)
        else parse_database_url(database)
    )
    if url.get_backend_name() == "sqlite":
        require(
            not url.query and url.database not in {None, ":memory:"}, "unsafe_snapshot_database"
        )
        path = Path(url.database)
        require(path.is_file() and not path.is_symlink(), "database_missing_or_unsafe")
        return create_async_engine(
            URL.create(
                "sqlite+aiosqlite",
                database="file:" + urllib.parse.quote(str(path.resolve()), safe="/"),
                query={"mode": "ro", "uri": "true"},
            )
        )
    require(
        url.host == "localhost"
        and url.port == 5432
        and url.password is None
        and set(url.query) == {"host"}
        and type(url.query["host"]) is str,
        "snapshot_requires_owned_postgresql_socket",
    )
    socket_directory = Path(url.query["host"])
    compose = (
        str(socket_directory) == "/var/run/marketing-agents-postgres"
        and url.username == url.database == "marketing_agents"
    )
    native = (
        re.fullmatch(r"/(?:private/)?tmp/ma-pg-[a-z0-9_]{8}", str(socket_directory)) is not None
        and url.username == url.database == "marketing_agents"
    )
    fixture = (
        os.environ.get("MARKETING_AGENTS_TEST_POSTGRES") == "1"
        and re.fullmatch(r"/(?:private/)?tmp/ma-pg-[a-z0-9_]{8}/socket", str(socket_directory))
        is not None
        and re.fullmatch(r"del04_[0-9a-f]{32}", url.database or "") is not None
        and re.fullmatch(r"del04_test|primary_[0-9a-f]{32}", url.username or "") is not None
    )
    require(compose or native or fixture, "snapshot_requires_owned_postgresql_socket")
    require(
        socket_directory.is_dir() and not socket_directory.is_symlink(),
        "snapshot_requires_owned_postgresql_socket",
    )
    if not compose:
        for directory in (
            (socket_directory.parent, socket_directory) if fixture else (socket_directory,)
        ):
            metadata = directory.lstat()
            require(
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o700,
                "snapshot_requires_private_postgresql_socket",
            )
    socket_metadata = (socket_directory / ".s.PGSQL.5432").lstat()
    require(
        stat.S_ISSOCK(socket_metadata.st_mode)
        and (compose or socket_metadata.st_uid == os.getuid()),
        "snapshot_requires_owned_postgresql_socket",
    )
    return create_async_engine(
        url,
        isolation_level="REPEATABLE READ",
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )


def _postgresql_sequence_snapshot(connection) -> tuple[list[dict], list[list]]:
    """Read managed sequence state without advancing it; sequences are not MVCC."""
    from sqlalchemy import text

    expected = {
        ("audit_events_global_sequence_seq", "audit_events", "global_sequence"),
        ("audit_feed_sequence_singleton_id_seq", "audit_feed_sequence", "singleton_id"),
        ("catalog_current_release_singleton_id_seq", "catalog_current_release", "singleton_id"),
    }
    definitions = [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT n.nspname AS schema, c.relname AS name, "
                "tn.nspname AS table_schema, t.relname AS table_name, a.attname AS column_name, "
                "d.deptype::text AS dependency_type, typ.typname AS data_type, "
                "s.seqstart AS start, s.seqincrement AS increment, s.seqmin AS minimum, "
                "s.seqmax AS maximum, s.seqcache AS cache, s.seqcycle AS cycle, "
                "c.relowner=t.relowner AND pg_get_userbyid(c.relowner)=current_user AS owned "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_sequence s ON s.seqrelid=c.oid JOIN pg_type typ ON typ.oid=s.seqtypid "
                "LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid "
                "AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') "
                "LEFT JOIN pg_class t ON t.oid=d.refobjid "
                "LEFT JOIN pg_namespace tn ON tn.oid=t.relnamespace "
                "LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid "
                "WHERE c.relkind='S' ORDER BY n.nspname, c.relname"
            )
        ).mappings()
    ]
    require(
        len(definitions) == len(expected)
        and {(row["name"], row["table_name"], row["column_name"]) for row in definitions}
        == expected
        and all(
            row["schema"] == row["table_schema"] == "public"
            and row["dependency_type"] == "a"
            and row["owned"] is True
            for row in definitions
        ),
        "snapshot_unsupported_sequences",
    )
    quote = connection.dialect.identifier_preparer.quote
    states = []
    for definition in definitions:
        name = definition["name"]
        state = connection.exec_driver_sql(
            f"SELECT last_value, is_called FROM {quote('public')}.{quote(name)}"
        ).one()
        require(
            type(state.last_value) is int and type(state.is_called) is bool,
            "snapshot_sequence_state_invalid",
        )
        states.append([name, state.last_value, state.is_called])
    return definitions, states


def _logical_database_snapshot(
    connection, fingerprint: str, *, include_table_digests: bool = False
) -> dict:
    """Hash schema and sorted rows, never exposing payloads in the report."""
    from datetime import UTC, date, datetime, time
    from decimal import Decimal

    from sqlalchemy import CheckConstraint, MetaData, Table, inspect, select, text
    from sqlalchemy.schema import AddConstraint, CreateColumn, CreateIndex

    def encode(value):
        if value is None or type(value) in {bool, int, str}:
            return value
        if isinstance(value, datetime):
            return {
                "datetime": value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
            }
        if isinstance(value, (date, time)):
            return {type(value).__name__: value.isoformat()}
        if isinstance(value, Decimal):
            return {"decimal": str(value)}
        if isinstance(value, float):
            return {"float": value.hex()}
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        if isinstance(value, (list, tuple)):
            return [encode(item) for item in value]
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        raise SmokeFailure("unsupported_snapshot_value")

    def canonical(value) -> bytes:
        return json.dumps(encode(value), sort_keys=True, separators=(",", ":")).encode()

    def constraint_definitions(table):
        checks = sorted(
            (value for value in table.constraints if isinstance(value, CheckConstraint)),
            key=lambda value: str(value.name),
        )
        normalized = {}
        if connection.dialect.name == "postgresql" and checks:
            expressions = ", ".join(
                "("
                + str(
                    value.sqltext.compile(
                        dialect=connection.dialect,
                        compile_kwargs={"literal_binds": True, "include_table": False},
                    )
                )
                + ")"
                for value in checks
            )
            quoted_table = connection.dialect.identifier_preparer.quote(table.name)
            # Match the established schema validator: no ANALYZE, no rows and
            # no user routines. PostgreSQL itself normalizes equivalent CHECK
            # casts that pg_dump/restore may spell at array or element level.
            plan = connection.exec_driver_sql(
                "EXPLAIN (VERBOSE, FORMAT JSON, COSTS FALSE) SELECT "
                + expressions
                + f" FROM {quoted_table} LIMIT 0"
            ).scalar_one()
            if isinstance(plan, str):
                plan = json.loads(plan)
            require(
                type(plan) is list
                and len(plan) == 1
                and type(plan[0]) is dict
                and type(plan[0].get("Plan")) is dict,
                "snapshot_check_plan_invalid",
            )
            output = plan[0]["Plan"].get("Output")
            require(
                type(output) is list
                and len(output) == len(checks)
                and all(type(value) is str and value for value in output),
                "snapshot_check_plan_invalid",
            )
            normalized = dict(zip(checks, output, strict=True))
        definitions = []
        for constraint in table.constraints:
            if constraint in normalized:
                definitions.append(
                    {
                        "kind": "check",
                        "name": None if constraint.name is None else str(constraint.name),
                        "expression": normalized[constraint],
                        "deferrable": constraint.deferrable,
                        "initially": constraint.initially,
                        "dialect_options": dict(constraint.dialect_kwargs),
                    }
                )
            else:
                definitions.append(
                    {
                        "definition": str(
                            AddConstraint(constraint).compile(dialect=connection.dialect)
                        )
                    }
                )
        return sorted(definitions, key=canonical)

    inspector = inspect(connection)
    if connection.dialect.name == "sqlite":
        require(
            not connection.execute(
                text(
                    "SELECT 1 FROM sqlite_schema WHERE type IN ('view', 'trigger') "
                    "OR name='sqlite_sequence' LIMIT 1"
                )
            ).first(),
            "snapshot_unsupported_schema_objects",
        )
    else:
        require(
            connection.dialect.default_schema_name == "public"
            and set(inspector.get_schema_names()) <= {"public", "information_schema"}
            and not inspector.get_view_names()
            and not inspector.get_materialized_view_names()
            and set(connection.execute(text("SELECT extname FROM pg_extension")).scalars())
            == {"plpgsql"}
            and not connection.execute(
                text("SELECT 1 FROM pg_trigger WHERE NOT tgisinternal LIMIT 1")
            ).first()
            and not connection.execute(
                text(
                    "SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' LIMIT 1"
                )
            ).first()
            and not connection.execute(text("SELECT 1 FROM pg_policy LIMIT 1")).first()
            and not connection.execute(
                text("SELECT 1 FROM pg_class WHERE relrowsecurity OR relforcerowsecurity LIMIT 1")
            ).first()
            and not connection.execute(
                text(
                    "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' LIMIT 1"
                )
            ).first()
            and not connection.execute(text("SELECT 1 FROM pg_foreign_table LIMIT 1")).first(),
            "snapshot_unsupported_schema_objects",
        )
        require(
            not connection.execute(
                text(
                    "SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                    "LEFT JOIN pg_class c ON c.oid=t.typrelid "
                    "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
                    "AND (t.typtype IN ('e','d','r','m') OR (t.typtype='c' AND c.relkind='c') "
                    "OR (t.typtype='b' AND t.typelem=0)) LIMIT 1"
                )
            ).first(),
            "snapshot_unsupported_schema_objects",
        )
    table_names = sorted(inspector.get_table_names())
    if connection.dialect.name == "postgresql":
        # Reflection does not faithfully construct every PostgreSQL CHECK
        # option (notably NOT VALID). Refuse those before Table reflection,
        # rather than silently losing options or treating them as equivalent.
        require(
            not any(
                check.get("dialect_options")
                for name in table_names
                for check in inspector.get_check_constraints(name)
            ),
            "snapshot_unsupported_constraint_options",
        )
    sequences = (
        _postgresql_sequence_snapshot(connection)
        if connection.dialect.name == "postgresql"
        else None
    )
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, autoload_with=connection, resolve_fks=False)
        for name in table_names
    }
    require("local_runtime_identity" in tables, "database_key_pair_mismatch")
    identity = tables["local_runtime_identity"]
    require(
        connection.execute(select(identity.c.format_version, identity.c.key_fingerprint)).all()
        == [(1, fingerprint)],
        "database_key_pair_mismatch",
    )
    logical_hash = hashlib.sha256()
    counts = {}
    table_digests = {}
    if sequences is not None:
        logical_hash.update(canonical(["postgresql_sequences", *sequences]) + b"\n")
        if include_table_digests:
            table_digests["__postgresql_sequences__"] = {
                "schema_sha256": hashlib.sha256(canonical(sequences[0])).hexdigest(),
                "rows_sha256": hashlib.sha256(canonical(sequences[1])).hexdigest(),
                "row_count": len(sequences[1]),
            }
    for name, table in tables.items():
        # Constraint declaration/reflection order is not semantic and may change
        # through pg_dump/pg_restore. Keep column order, but canonicalize the
        # complete individual PK/FK/UNIQUE/CHECK definitions independently.
        schema = {
            "columns": [
                str(CreateColumn(column).compile(dialect=connection.dialect))
                for column in table.columns
            ],
            "constraints": constraint_definitions(table),
        }
        indexes = sorted(
            str(CreateIndex(index).compile(dialect=connection.dialect)) for index in table.indexes
        )
        rows = sorted(canonical(tuple(row)) for row in connection.execute(select(table)))
        logical_hash.update(canonical([name, schema, indexes, len(rows)]) + b"\n")
        for row in rows:
            logical_hash.update(row + b"\n")
        if include_table_digests:
            table_digests[name] = {
                "schema_sha256": hashlib.sha256(canonical([name, schema, indexes])).hexdigest(),
                "rows_sha256": hashlib.sha256(b"".join(row + b"\n" for row in rows)).hexdigest(),
                "row_count": len(rows),
            }
        if name in {
            "connector_action_receipts",
            "external_actions",
            "webhook_receipts",
            "schedule_occurrences",
        }:
            counts[name] = len(rows)
    if sequences is not None:
        # Unlike table rows, nextval/setval state is not isolated by REPEATABLE
        # READ. Reject observed movement. Exact restore comparisons require a
        # quiescent owned source; this is not an atomic online sequence snapshot.
        require(
            _postgresql_sequence_snapshot(connection) == sequences,
            "snapshot_sequences_changed",
        )
    require(len(counts) == 4 and "alembic_version" in tables, "snapshot_runtime_tables_missing")
    result = {
        "format_version": 1,
        "key_fingerprint": fingerprint,
        "logical_database_sha256": logical_hash.hexdigest(),
        "counts": counts,
    }
    if include_table_digests:
        result["table_digests"] = table_digests
    return result


async def async_database_snapshot(
    database: Path | str, key: Path, *, include_table_digests: bool = False
) -> dict:
    """Read every table in one consistent, read-only database transaction."""
    from marketing_agents.security.digest_key import (
        digest_key_fingerprint,
        load_or_create_digest_key,
    )

    require(key.is_file() and not key.is_symlink(), "key_missing_or_unsafe")
    fingerprint = digest_key_fingerprint(
        load_or_create_digest_key(key, persistent_state_exists=True)
    )
    engine = readonly_database_engine(database)
    try:
        async with engine.begin() as connection:
            if engine.dialect.name == "sqlite":
                # SQLite's driver otherwise delays BEGIN until a write. A real
                # read transaction is necessary to keep all table reads paired.
                await connection.exec_driver_sql("BEGIN")
            return await connection.run_sync(
                _logical_database_snapshot, fingerprint, include_table_digests=include_table_digests
            )
    finally:
        await engine.dispose()


def database_snapshot(
    database: Path | str, key: Path, *, include_table_digests: bool = False
) -> dict:
    """Compatibility entry point for CLI and existing synchronous smoke callers."""
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            async_database_snapshot(database, key, include_table_digests=include_table_digests)
        )
    # Existing async smoke tests call this synchronous helper. Own and join one
    # thread so its engine/event loop never leaks into the caller's event loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            lambda: asyncio.run(
                async_database_snapshot(database, key, include_table_digests=include_table_digests)
            )
        ).result()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ready", "demos", "replay"):
        command = sub.add_parser(name)
        command.add_argument("--origin", required=True)
        if name == "replay":
            command.add_argument("--previous", type=Path, required=True)
    snapshot = sub.add_parser("snapshot")
    source = snapshot.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", type=Path)
    source.add_argument("--database-url")
    snapshot.add_argument("--table-digests", action="store_true")
    snapshot.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            result = database_snapshot(
                args.database or args.database_url,
                args.key,
                include_table_digests=args.table_digests,
            )
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
    except Exception as exc:
        code = str(exc) if isinstance(exc, SmokeFailure) else "smoke_contract_failure"
        print(json.dumps({"ok": False, "code": code}))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
