"""Work out every model's output columns, without a warehouse.

The algorithm is a single topological pass. Leaves (sources and seeds) are
seeded from what is knowable offline; each model is then qualified against the
schemas of its own parents, and its output columns fall out of the qualified
projection.

**The central rule, and the one that makes this viable** (ADR 000 F2):

    Raw `SELECT *` presence is NOT the uncertainty signal.
    A star that SURVIVES qualification is.

The dominant dbt idiom ends every model in `select * from final`. Measured over
public projects, 50% of models contain a star - but almost all of those stars
are over a CTE whose projection is explicit, and sqlglot expands them correctly
against an empty schema. Treating raw star presence as uncertainty scored 0%
EXACT on jaffle_shop; asking whether a star survived scores 100%. Same code,
same data, opposite conclusion.

Unknown-ness propagates, but only where it actually bites: a model selecting
explicit columns from an unknown parent still knows its own output names.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from whatbreaks.graph import topological_sort
from whatbreaks.lineage.uncertainty import Resolution, Uncertainty, UnknownReason
from whatbreaks.manifest.models import Manifest, Node, ResourceType
from whatbreaks.sql.dialect import dialect_for
from whatbreaks.sql.recovery import FailureKind, SqlRecovery

MAX_SEED_HEADER_BYTES = 1024 * 1024


class SchemaOrigin(str, Enum):
    """Where a schema came from. Reported so users can see what we relied on."""

    DECLARED = "declared"
    SEED_CSV = "seed_csv"
    INFERRED = "inferred"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ModelSchema:
    node_id: str
    columns: tuple[str, ...] = ()
    uncertainty: Uncertainty = field(
        default_factory=lambda: Uncertainty.unknown(UnknownReason.NO_SQL)
    )
    origin: SchemaOrigin = SchemaOrigin.NONE
    undocumented: tuple[str, ...] = ()
    documented_but_absent: tuple[str, ...] = ()

    @property
    def resolution(self) -> Resolution:
        return self.uncertainty.resolution

    @property
    def is_usable(self) -> bool:
        return self.resolution.is_usable and bool(self.columns)

    @property
    def has_doc_drift(self) -> bool:
        """The YAML says something the SQL does not, or vice versa.

        A useful side-effect rather than the point: it means the project's
        documentation is stale.
        """
        return bool(self.undocumented or self.documented_but_absent)


@dataclass(frozen=True, slots=True)
class InferenceResult:
    schemas: dict[str, ModelSchema]

    def coverage(self) -> dict[str, int]:
        counts = {r.value: 0 for r in Resolution}
        for schema in self.schemas.values():
            counts[schema.resolution.value] += 1
        counts["total"] = len(self.schemas)
        return counts

    def reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for schema in self.schemas.values():
            if schema.uncertainty.reason is not UnknownReason.NONE:
                key = schema.uncertainty.reason.value
                out[key] = out.get(key, 0) + 1
        return out


_FAILURE_TO_REASON = {
    FailureKind.NO_SQL: UnknownReason.NO_SQL,
    FailureKind.INTROSPECTIVE: UnknownReason.NEEDS_WAREHOUSE,
    FailureKind.UNDEFINED_MACRO: UnknownReason.UNPARSEABLE_JINJA,
    FailureKind.JINJA_SYNTAX: UnknownReason.UNPARSEABLE_JINJA,
    FailureKind.JINJA_RUNTIME: UnknownReason.UNPARSEABLE_JINJA,
    FailureKind.TOO_LARGE: UnknownReason.UNPARSEABLE_JINJA,
    FailureKind.RECURSION: UnknownReason.UNPARSEABLE_JINJA,
}


class SchemaInference:
    """Infers output columns for every model in a manifest."""

    def __init__(
        self,
        manifest: Manifest,
        recovery: SqlRecovery,
        project_root: Path | None = None,
    ) -> None:
        self._manifest = manifest
        self._recovery = recovery
        self._project_root = project_root
        self._dialect = dialect_for(manifest.adapter_type)

    def infer(self) -> InferenceResult:
        schemas: dict[str, ModelSchema] = {}

        # ---- seed the leaves ------------------------------------------
        for uid, node in self._manifest.nodes.items():
            if node.resource_type is ResourceType.SOURCE:
                schemas[uid] = self._from_declared(node)
            elif node.resource_type is ResourceType.SEED:
                schemas[uid] = self._from_seed(node)

        # ---- walk models in dependency order --------------------------
        edges = self._manifest.dependency_edges()
        for uid in topological_sort(edges):
            target = self._manifest.nodes.get(uid)
            if target is None or target.resource_type not in (
                ResourceType.MODEL,
                ResourceType.SNAPSHOT,
            ):
                continue
            if target.is_python:
                # Out of scope, and said plainly. Its raw_code is Python, so
                # any SQL-shaped diagnostic here would send the user hunting
                # for a problem in SQL they never wrote.
                schemas[uid] = self._fallback_to_declared(target, UnknownReason.PYTHON_MODEL)
                continue
            schemas[uid] = self._infer_one(target, schemas)

        return InferenceResult(schemas)

    # ------------------------------------------------------------------
    def _from_declared(self, node: Node) -> ModelSchema:
        cols = node.declared_column_names
        if cols:
            return ModelSchema(node.unique_id, cols, Uncertainty.exact(), SchemaOrigin.DECLARED)
        return ModelSchema(
            node.unique_id,
            (),
            Uncertainty.unknown(UnknownReason.NO_OUTPUT_COLUMNS),
            SchemaOrigin.NONE,
        )

    def _from_seed(self, node: Node) -> ModelSchema:
        """A seed's columns are its CSV header - free, offline, authoritative.

        Missing this makes every downstream `select *` degrade for no reason
        (ADR 000 F3).
        """
        declared = node.declared_column_names
        if declared:
            return ModelSchema(node.unique_id, declared, Uncertainty.exact(), SchemaOrigin.DECLARED)
        header = self._read_seed_header(node)
        if header:
            return ModelSchema(node.unique_id, header, Uncertainty.exact(), SchemaOrigin.SEED_CSV)
        return ModelSchema(
            node.unique_id,
            (),
            Uncertainty.unknown(UnknownReason.NO_OUTPUT_COLUMNS),
            SchemaOrigin.NONE,
        )

    def _read_seed_header(self, node: Node) -> tuple[str, ...]:
        if self._project_root is None or not node.original_file_path:
            return ()
        path = self._project_root / node.original_file_path
        try:
            resolved = path.resolve()
            root = self._project_root.resolve()
            if root not in resolved.parents:
                return ()  # refuse to read outside the project
            if not resolved.is_file():
                return ()
            # Only the first line is read, so seed *size* is irrelevant. What
            # does matter is a pathological single line, hence the cap on how
            # much of it we are willing to consume.
            with resolved.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                header = fh.readline(MAX_SEED_HEADER_BYTES)
            row = next(csv.reader([header]), [])
        except (OSError, csv.Error):
            return ()
        return tuple(c.strip() for c in row if c.strip())

    # ------------------------------------------------------------------
    def _infer_one(self, node: Node, schemas: dict[str, ModelSchema]) -> ModelSchema:
        recovered = self._recovery.recover(node)
        if not recovered.ok or recovered.sql is None:
            reason = (
                _FAILURE_TO_REASON.get(recovered.failure.kind, UnknownReason.UNPARSEABLE_JINJA)
                if recovered.failure
                else UnknownReason.NO_SQL
            )
            return self._fallback_to_declared(node, reason)

        try:
            tree = sqlglot.parse_one(recovered.sql, dialect=self._dialect)
        except Exception:
            return self._fallback_to_declared(node, UnknownReason.SQL_PARSE_ERROR)

        if tree is None or not isinstance(tree, exp.Query):
            return self._fallback_to_declared(node, UnknownReason.SQL_PARSE_ERROR)

        parent_schema, upstream_unknown = self._parent_schema(node, schemas)

        qualified: exp.Query
        try:
            qualified = qualify(
                tree,
                schema=parent_schema,
                dialect=self._dialect,
                infer_schema=True,
                validate_qualify_columns=False,
            )
        except Exception:
            return self._fallback_to_declared(node, UnknownReason.QUALIFY_ERROR)

        columns, surviving_star = self._outputs(qualified)

        if not columns:
            return self._fallback_to_declared(node, UnknownReason.NO_OUTPUT_COLUMNS)

        if surviving_star:
            # THE rule: a star only signals uncertainty if qualification could
            # not expand it. Stars over CTEs resolve and are not a problem.
            reason = (
                UnknownReason.UPSTREAM_UNKNOWN if upstream_unknown else UnknownReason.SURVIVING_STAR
            )
            uncertainty = Uncertainty.partial(reason)
        else:
            uncertainty = Uncertainty.exact()

        return self._with_doc_drift(node, columns, uncertainty, SchemaOrigin.INFERRED)

    def _parent_schema(
        self, node: Node, schemas: dict[str, ModelSchema]
    ) -> tuple[dict[str, Any], bool]:
        """Build a sqlglot schema from this node's own parents.

        Scoped per node rather than global on purpose: `relation_key` is derived
        from a node's name, and two packages may legitimately each define a
        model called `orders`. Restricting to actual `depends_on` parents means
        a collision only matters if one model depends on both, which is rare and
        detectable, instead of silently corrupting the whole project's schema.
        """
        out: dict[str, dict[str, str]] = {}
        upstream_unknown = False
        for parent_id in node.depends_on:
            parent = self._manifest.nodes.get(parent_id)
            if parent is None:
                continue
            schema = schemas.get(parent_id)
            if schema is None or not schema.is_usable:
                upstream_unknown = True
                continue
            out[parent.relation_key] = dict.fromkeys(schema.columns, "UNKNOWN")
            if schema.resolution is Resolution.PARTIAL:
                upstream_unknown = True
        return out, upstream_unknown

    @staticmethod
    def _outputs(query: exp.Query) -> tuple[tuple[str, ...], bool]:
        """Output column names, plus whether an unexpanded star remains."""
        selects: list[exp.Expression] = []
        if isinstance(query, exp.Query):
            try:
                selects = list(query.selects)  # type: ignore[arg-type]
            except Exception:
                selects = []

        surviving_star = any(
            isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
            for e in selects
        )
        names = tuple(
            name
            for e in selects
            if not (
                isinstance(e, exp.Star)
                or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
            )
            and (name := e.alias_or_name)
        )
        return names, surviving_star

    # ------------------------------------------------------------------
    def _fallback_to_declared(self, node: Node, reason: UnknownReason) -> ModelSchema:
        """When inference fails, YAML-declared columns are better than nothing.

        Reported as PARTIAL, never EXACT: the declaration is what a human said,
        not what the SQL does, and the gap between the two is exactly the kind
        of thing this tool exists to find.
        """
        declared = node.declared_column_names
        if declared:
            return ModelSchema(
                node.unique_id, declared, Uncertainty.partial(reason), SchemaOrigin.DECLARED
            )
        return ModelSchema(node.unique_id, (), Uncertainty.unknown(reason), SchemaOrigin.NONE)

    @staticmethod
    def _with_doc_drift(
        node: Node,
        columns: tuple[str, ...],
        uncertainty: Uncertainty,
        origin: SchemaOrigin,
    ) -> ModelSchema:
        declared = set(node.declared_column_names)
        inferred = set(columns)
        undocumented: tuple[str, ...] = ()
        absent: tuple[str, ...] = ()
        if declared:
            undocumented = tuple(c for c in columns if c not in declared)
            absent = tuple(c for c in node.declared_column_names if c not in inferred)
        return ModelSchema(
            node.unique_id,
            columns,
            uncertainty,
            origin,
            undocumented=undocumented,
            documented_but_absent=absent,
        )
