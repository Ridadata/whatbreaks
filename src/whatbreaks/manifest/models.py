"""Typed view over the subset of dbt's manifest that whatbreaks actually reads.

dbt's manifest is large and its shape drifts between versions. Rather than
passing raw dicts around, everything is normalised once at load time into these
frozen dataclasses. Two benefits: the rest of the codebase never sees a version
difference, and the blast radius of a dbt schema change is one module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class ResourceType(str, Enum):
    MODEL = "model"
    SEED = "seed"
    SNAPSHOT = "snapshot"
    SOURCE = "source"
    TEST = "test"
    EXPOSURE = "exposure"


@dataclass(frozen=True, slots=True)
class Column:
    """A column as *declared* in YAML. Not necessarily what the SQL produces.

    The gap between declared and inferred is itself useful signal -- it means
    the project's documentation is stale -- so both are tracked rather than one
    silently overwriting the other.
    """

    name: str
    data_type: str | None = None


@dataclass(frozen=True, slots=True)
class Node:
    """A model, seed, snapshot or source.

    `relation_key` is the identifier this node is referenced by in rendered SQL.
    It is the join between the manifest graph and the parsed SQL, and must be
    produced by exactly one function (`sql.dialect.relation_key`) so the two
    sides cannot drift.
    """

    unique_id: str
    name: str
    resource_type: ResourceType
    package_name: str
    relation_key: str
    original_file_path: str
    depends_on: tuple[str, ...] = ()
    columns: Mapping[str, Column] = field(default_factory=dict)
    raw_code: str = ""
    compiled_code: str | None = None
    materialized: str | None = None
    contract_enforced: bool = False
    unique_key: str | None = None
    # sources only
    source_name: str | None = None

    @property
    def is_executable_sql(self) -> bool:
        """Does this node have SQL we are expected to analyse?"""
        return self.resource_type in (ResourceType.MODEL, ResourceType.SNAPSHOT)

    @property
    def declared_column_names(self) -> tuple[str, ...]:
        return tuple(self.columns)


@dataclass(frozen=True, slots=True)
class Test:
    """A dbt test. A consumer that can be broken by an upstream column change."""

    unique_id: str
    name: str
    depends_on: tuple[str, ...] = ()
    column_name: str | None = None
    attached_node: str | None = None
    test_type: str | None = None


@dataclass(frozen=True, slots=True)
class Exposure:
    """A declared downstream consumer (dashboard, application, notebook).

    Exposures are the only place a stock dbt project records that anything
    exists beyond the DAG's edge, which makes them the cheapest possible
    approximation of real-world blast radius.
    """

    unique_id: str
    name: str
    depends_on: tuple[str, ...] = ()
    exposure_type: str | None = None
    url: str | None = None
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class Macro:
    unique_id: str
    name: str
    package_name: str
    macro_sql: str

    @property
    def is_plain_macro(self) -> bool:
        """True for `{% macro %}` blocks only.

        dbt also stores `{% materialization %}`, `{% test %}` and
        `{% snapshot %}` under `macros`. Those are dbt Jinja extensions that
        plain Jinja2 cannot parse, and a single one poisons a bulk compile --
        ADR 000 F6.
        """
        head = self.macro_sql.lstrip()
        return head.startswith("{% macro") or head.startswith("{%- macro")


@dataclass(frozen=True, slots=True)
class Manifest:
    """Everything whatbreaks needs from one dbt project at one commit."""

    schema_version: int
    dbt_version: str
    adapter_type: str
    project_name: str
    nodes: Mapping[str, Node]
    tests: Mapping[str, Test]
    exposures: Mapping[str, Exposure]
    macros: Mapping[str, Macro]

    @property
    def models(self) -> dict[str, Node]:
        return {uid: n for uid, n in self.nodes.items() if n.resource_type is ResourceType.MODEL}

    def dependency_edges(self) -> dict[str, tuple[str, ...]]:
        """`node -> nodes it depends on`, restricted to nodes we actually have.

        dbt resolves `ref()` and `source()` for us, so this graph is
        authoritative and must never be re-derived by parsing `ref()` out of
        SQL ourselves.
        """
        return {
            uid: tuple(d for d in n.depends_on if d in self.nodes) for uid, n in self.nodes.items()
        }

    def plain_macros(self) -> list[Macro]:
        return [m for m in self.macros.values() if m.is_plain_macro]
