"""Read-only structural comparison of the deployed schema and mapped metadata."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import CheckConstraint, Column, UniqueConstraint, inspect, literal, text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.schema import Constraint, Index

from .base import Base

_SQL_TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]|\s+|.", re.S)


def _normalized_sql(value: object | None) -> str | None:
    if value is None:
        return None
    tokens: list[str] = []
    for match in _SQL_TOKEN.finditer(str(value)):
        token = match.group()
        if token.startswith("'"):
            # Literal case and embedded spaces are meaningful constraint data.
            tokens.append(token)
        elif token.startswith(('"', "`", "[")):
            tokens.append(token[1:-1].casefold())
        elif not token.isspace():
            tokens.append(token.casefold())
    return "".join(tokens)


def _normalized_default(value: object | None) -> str | None:
    rendered = _normalized_sql(value)
    if rendered is None:
        return None
    # SQLite quotes numeric server defaults; their SQL values remain identical.
    if re.fullmatch(r"'[+-]?[0-9]+'", rendered):
        return rendered[1:-1]
    return rendered


def _constraint_name(connection: Connection, constraint: Constraint | Index) -> str:
    """Match the dialect's emitted name, including PostgreSQL's bounded hash suffix."""
    return str(
        connection.dialect.identifier_preparer.format_constraint(constraint, _alembic_quote=False)
    )


def _postgres_expressions_match(
    connection: Connection, table_name: str, pairs: list[tuple[str, str]]
) -> bool:
    """Compare server-normalized projections without executing them or changing schema.

    PostgreSQL rewrites checks with casts, parentheses, and ANY/array expressions.
    Its VERBOSE plan renders both expressions against the same column types. Never
    use ANALYZE: no table rows, defaults, or volatile expressions are evaluated.
    Schema administrators remain trusted, including their immutable SQL functions.
    """
    if not pairs:
        return True
    table = connection.dialect.identifier_preparer.quote(table_name)
    expressions = ", ".join(f"({value})" for pair in pairs for value in pair)
    plan = connection.exec_driver_sql(
        f"EXPLAIN (VERBOSE, FORMAT JSON, COSTS FALSE) SELECT {expressions} FROM {table} LIMIT 0"
    ).scalar_one()
    if isinstance(plan, str):
        plan = json.loads(plan)
    output = plan[0]["Plan"].get("Output", [])
    return len(output) == len(pairs) * 2 and all(
        output[index] == output[index + 1] for index in range(0, len(output), 2)
    )


def _postgres_default_matches(
    connection: Connection, table_name: str, column: Column[Any], actual: Mapping[str, Any]
) -> bool:
    default = actual.get("default")
    if actual.get("computed") or actual.get("identity"):
        # Current models emit neither generated columns nor IDENTITY syntax.
        return False
    if column.server_default is None:
        if column.table.autoincrement_column is not column:
            return default is None
        if default is None or not actual.get("autoincrement"):
            return False
        # SERIAL is the implicit PostgreSQL default for mapped integer identity
        # columns. Verify the owned sequence, not merely any nextval expression.
        sequence = connection.execute(
            text("SELECT pg_get_serial_sequence(:table, :column)"),
            {"table": table_name, "column": column.name},
        ).scalar_one()
        if sequence is None:
            return False
        value = literal(sequence).compile(
            dialect=connection.dialect, compile_kwargs={"literal_binds": True}
        )
        expected = f"nextval({value}::regclass)"
    else:
        if default is None:
            return False
        expected = str(getattr(column.server_default, "arg", column.server_default))
        expected_type = column.type.compile(dialect=connection.dialect)
        expected = f"CAST(({expected}) AS {expected_type})"
    return _postgres_expressions_match(connection, table_name, [(expected, str(default))])


def _mapped_constraints_are_compatible(connection: Connection, table_name: str) -> bool:
    inspector = inspect(connection)
    table = Base.metadata.tables[table_name]
    actual_primary = inspector.get_pk_constraint(table_name)
    if _constraint_name(connection, table.primary_key) != str(actual_primary.get("name")) or tuple(
        table.primary_key.columns.keys()
    ) != tuple(actual_primary.get("constrained_columns", ())):
        return False

    expected_unique = {
        (_constraint_name(connection, constraint), tuple(constraint.columns.keys()))
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    actual_unique = {
        (str(constraint.get("name")), tuple(constraint.get("column_names", ())))
        for constraint in inspector.get_unique_constraints(table_name)
    }
    if expected_unique != actual_unique:
        return False

    expected_foreign_keys = {
        (
            _constraint_name(connection, constraint),
            tuple(constraint.column_keys),
            tuple(
                f"{element.column.table.schema or connection.dialect.default_schema_name}."
                f"{element.column.table.name}.{element.column.name}"
                for element in constraint.elements
            ),
            constraint.ondelete,
            constraint.onupdate,
            constraint.deferrable,
            constraint.initially,
        )
        for constraint in table.foreign_key_constraints
    }
    actual_foreign_keys = {
        (
            str(constraint.get("name")),
            tuple(constraint.get("constrained_columns", ())),
            tuple(
                f"{constraint.get('referred_schema') or connection.dialect.default_schema_name}."
                f"{constraint.get('referred_table')}.{column}"
                for column in constraint.get("referred_columns", ())
            ),
            constraint.get("options", {}).get("ondelete"),
            constraint.get("options", {}).get("onupdate"),
            constraint.get("options", {}).get("deferrable"),
            constraint.get("options", {}).get("initially"),
        )
        for constraint in inspector.get_foreign_keys(table_name)
    }
    if expected_foreign_keys != actual_foreign_keys:
        return False

    expected_checks = {
        _constraint_name(connection, constraint): str(
            constraint.sqltext.compile(
                dialect=connection.dialect,
                compile_kwargs={"literal_binds": True, "include_table": False},
            )
        )
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and not (
            getattr(constraint, "_type_bound", False) and connection.dialect.supports_native_boolean
        )
    }
    reflected_checks = inspector.get_check_constraints(table_name)
    actual_checks = {
        str(constraint.get("name")): str(constraint.get("sqltext"))
        for constraint in reflected_checks
    }
    if set(expected_checks) != set(actual_checks) or any(
        constraint.get("dialect_options") for constraint in reflected_checks
    ):
        return False
    if connection.dialect.name == "postgresql":
        if not _postgres_expressions_match(
            connection,
            table_name,
            [(expected_checks[name], actual_checks[name]) for name in sorted(expected_checks)],
        ):
            return False
    elif {name: _normalized_sql(value) for name, value in expected_checks.items()} != {
        name: _normalized_sql(value) for name, value in actual_checks.items()
    }:
        return False

    expected_indexes = {
        (
            _constraint_name(connection, index),
            tuple(index.columns.keys()),
            bool(index.unique),
            _normalized_sql(index.dialect_options[connection.dialect.name].get("where")),
        )
        for index in table.indexes
    }
    actual_indexes = {
        (
            str(index.get("name")),
            tuple(index.get("column_names", ())),
            bool(index.get("unique", False)),
            _normalized_sql(
                index.get("dialect_options", {}).get(f"{connection.dialect.name}_where")
            ),
        )
        for index in inspector.get_indexes(table_name)
        if not index.get("duplicates_constraint")
    }
    if any(
        index.get("column_sorting")
        or index.get("include_columns")
        or any(
            value
            for key, value in index.get("dialect_options", {}).items()
            if key != f"{connection.dialect.name}_where"
        )
        for index in inspector.get_indexes(table_name)
    ):
        # Current mapped indexes have plain ascending columns only.
        return False
    return expected_indexes == actual_indexes


def schema_matches_metadata(connection: Connection, *, exact_tables: bool = True) -> bool:
    """Compare columns, named keys, checks, and indexes without repairing anything.

    The Alembic bookkeeping table is the only permitted extra table in exact mode.
    Migration currency itself is checked separately by the migration inspector.
    """

    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
    expected_tables = set(Base.metadata.tables)
    if not expected_tables or not expected_tables.issubset(actual_tables):
        return False
    if exact_tables and actual_tables != expected_tables:
        return False
    for table_name, table in Base.metadata.tables.items():
        actual_columns: dict[str, Mapping[str, Any]] = {
            str(column["name"]): column for column in inspector.get_columns(table_name)
        }
        primary_columns = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns", ())
        )
        if set(actual_columns) != set(table.columns.keys()):
            return False
        for expected in table.columns:
            actual = actual_columns[expected.name]
            actual_type = actual.get("type")
            expected_type = expected.type.dialect_impl(connection.dialect)
            if getattr(actual_type, "_type_affinity", None) is not getattr(
                expected_type, "_type_affinity", None
            ):
                return False
            for attribute in ("length", "precision", "scale", "timezone"):
                expected_value = getattr(expected_type, attribute, None)
                if (
                    expected_value is not None
                    and getattr(actual_type, attribute, None) != expected_value
                ):
                    return False
            if (expected.name in primary_columns) != bool(expected.primary_key):
                return False
            if bool(actual.get("nullable", True)) != bool(expected.nullable):
                return False
            if connection.dialect.name == "postgresql":
                if not _postgres_default_matches(connection, table_name, expected, actual):
                    return False
                continue
            expected_default = (
                None
                if expected.server_default is None
                else _normalized_default(
                    getattr(expected.server_default, "arg", expected.server_default)
                )
            )
            if _normalized_default(actual.get("default")) != expected_default:
                return False
        if not _mapped_constraints_are_compatible(connection, table_name):
            return False
    return True
