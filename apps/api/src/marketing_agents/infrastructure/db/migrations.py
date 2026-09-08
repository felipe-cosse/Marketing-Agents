"""Explicit, transactional Alembic upgrades and read-only deployed-schema checks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from marketing_agents.infrastructure.db.schema import schema_matches_metadata
from marketing_agents.infrastructure.db.session import DatabaseRuntime

HEAD_REVISION = "0005"
REVISION_TABLES: dict[str, frozenset[str]] = {
    "0001": frozenset(
        {
            "catalog_releases",
            "catalog_current_release",
            "departments",
            "function_teams",
            "tool_capabilities",
            "approval_policies",
            "agent_templates",
            "agent_template_capabilities",
            "agent_template_trigger_kinds",
            "agent_instances",
            "agent_instance_configs",
            "trigger_definitions",
            "local_runtime_identity",
        }
    ),
    "0002": frozenset(
        {
            "campaign_briefs",
            "work_items",
            "runs",
            "run_plans",
            "run_plan_selected_instances",
            "run_plan_routing_assignments",
            "run_steps",
            "run_step_dependencies",
            "run_step_state_transitions",
            "run_state_transitions",
            "artifacts",
            "artifact_parent_edges",
        }
    ),
    "0003": frozenset(
        {
            "external_actions",
            "external_action_dispatch_attempts",
            "connector_action_receipts",
            "approval_requests",
            "approval_decisions",
            "approval_uses",
            "authorization_sets",
            "authorization_set_heads",
            "authorization_set_members",
            "audit_feed_sequence",
        }
    ),
    "0004": frozenset(
        {
            "webhook_receipts",
            "webhook_receipt_deliveries",
            "schedules",
            "schedule_occurrences",
        }
    ),
    "0005": frozenset(
        {
            "run_execution_controls",
            "execution_operation_policies",
            "rate_limit_windows",
            "execution_attempts",
            "audit_events",
            "maintenance_runs",
        }
    ),
}
_MIGRATION_LOCK = 4_604_001


class DatabaseMigrationError(RuntimeError):
    """Stable diagnostics intentionally omit database URLs and driver exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    revision: str | None
    head: str
    current: bool
    schema_matches: bool


def migration_config() -> Config:
    """Resolve installed package resources, independent of the process working directory."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("alembic")))
    return config


def expected_tables(revision: str | None) -> frozenset[str]:
    if revision is None:
        return frozenset()
    if revision not in REVISION_TABLES:
        raise DatabaseMigrationError("migration_revision_unknown")
    return frozenset().union(
        *(tables for key, tables in REVISION_TABLES.items() if key <= revision)
    )


def _revision(connection: Connection) -> str | None:
    heads = MigrationContext.configure(connection).get_current_heads()
    if len(heads) > 1:
        raise DatabaseMigrationError("migration_multiple_heads")
    revision = heads[0] if heads else None
    if revision is not None and revision not in REVISION_TABLES:
        raise DatabaseMigrationError("migration_revision_unknown")
    return revision


def inspect_migration(connection: Connection) -> MigrationStatus:
    revision = _revision(connection)
    exact = revision == HEAD_REVISION and schema_matches_metadata(connection)
    return MigrationStatus(revision, HEAD_REVISION, revision == HEAD_REVISION, exact)


def _upgrade(connection: Connection, revision: str) -> str:
    target = HEAD_REVISION if revision == "head" else revision
    if target not in REVISION_TABLES:
        raise DatabaseMigrationError("migration_target_unknown")
    config = migration_config()
    if ScriptDirectory.from_config(config).get_current_head() != HEAD_REVISION:
        raise DatabaseMigrationError("migration_package_head_mismatch")
    before = _revision(connection)
    tables = frozenset(inspect(connection).get_table_names()) - {"alembic_version"}
    if before is None and tables:
        raise DatabaseMigrationError("migration_unversioned_schema")
    if before is not None and before > target:
        raise DatabaseMigrationError("migration_downgrade_unsupported")
    if tables != expected_tables(before):
        raise DatabaseMigrationError("migration_schema_drift")
    if before == HEAD_REVISION and not schema_matches_metadata(connection):
        raise DatabaseMigrationError("migration_schema_drift")
    config.attributes["connection"] = connection
    command.upgrade(config, target)
    if _revision(connection) != target:
        raise DatabaseMigrationError("migration_revision_incomplete")
    if (frozenset(inspect(connection).get_table_names()) - {"alembic_version"}) != expected_tables(
        target
    ):
        raise DatabaseMigrationError("migration_schema_drift")
    if target == HEAD_REVISION and not schema_matches_metadata(connection):
        raise DatabaseMigrationError("migration_schema_drift")
    return target


@asynccontextmanager
async def migration_transaction(runtime: DatabaseRuntime) -> AsyncIterator[AsyncConnection]:
    """Serialize migration owners and protect SQLite DDL from legacy autocommit."""
    async with runtime.engine.connect() as connection, connection.begin():
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif connection.dialect.name == "postgresql":
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK}
            )
        yield connection


async def upgrade_database(runtime: DatabaseRuntime, revision: str = "head") -> str:
    """Atomically upgrade schema/version only; never adopt an unversioned schema."""
    async with migration_transaction(runtime) as connection:
        return await connection.run_sync(_upgrade, revision)
