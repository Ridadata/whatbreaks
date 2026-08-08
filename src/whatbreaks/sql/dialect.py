"""Adapter/dialect mapping and relation-key generation.

`relation_key` is the single join between two worlds: the manifest's node graph
and the identifiers that appear in rendered SQL. The Jinja stub's `ref()` and
`source()` MUST emit exactly what this function returns, or schema inference
silently finds nothing and every model degrades to UNKNOWN. Keeping both sides
calling one function is the only thing preventing that class of bug.
"""

from __future__ import annotations

import re

# dbt adapter_type -> sqlglot dialect name.
# Unmapped adapters are not an error: sqlglot's generic dialect parses most
# ANSI SQL, and reporting reduced confidence beats refusing to run.
DIALECT_BY_ADAPTER: dict[str, str] = {
    "athena": "athena",
    "bigquery": "bigquery",
    "clickhouse": "clickhouse",
    "databricks": "databricks",
    "duckdb": "duckdb",
    "fabric": "tsql",
    "postgres": "postgres",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "spark": "spark",
    "sqlserver": "tsql",
    "starrocks": "starrocks",
    "synapse": "tsql",
    "trino": "trino",
}

_RELATION_PREFIX = "wb"
_UNSAFE = re.compile(r"[^0-9a-zA-Z_]")


def dialect_for(adapter_type: str) -> str | None:
    """sqlglot dialect for a dbt adapter, or None to use sqlglot's default."""
    return DIALECT_BY_ADAPTER.get((adapter_type or "").strip().lower())


def _slug(part: str) -> str:
    return _UNSAFE.sub("_", part)


def relation_key(*parts: str) -> str:
    """Stable, SQL-safe identifier for a node.

    Not a real warehouse relation name -- deliberately. Using the true
    database.schema.identifier would require warehouse knowledge we refuse to
    depend on, and would collide across environments. This is an internal
    surrogate whose only job is to be consistent between the stub and the
    schema map.
    """
    joined = "_".join(_slug(p) for p in parts if p)
    return f"{_RELATION_PREFIX}_{joined}"


def model_relation_key(name: str) -> str:
    """Key for anything reachable via `ref()` -- models, seeds, snapshots."""
    return relation_key("model", name)


def source_relation_key(source_name: str, table_name: str) -> str:
    """Key for anything reachable via `source()`."""
    return relation_key("source", source_name, table_name)
