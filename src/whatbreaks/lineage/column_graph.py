"""Column-level lineage: which upstream columns feed each output column.

Built on `sqlglot.lineage`, which does the hard AST work. What this module adds
is the part that matters for a breaking-change linter:

* mapping sqlglot's leaf tables back to **manifest node ids**, so an edge points
  at a dbt model rather than a bare identifier;
* attaching a **confidence** to every edge, derived from the same algebra as
  everything else - an edge from a PARTIAL schema, or from SQL we rendered
  ourselves, is not a CONFIRMED fact;
* recording **why** an edge exists (`EdgeKind`), because "you dropped a column
  this model selects" and "you dropped a column this model joins on" are
  different messages even though both break.

Edges are always emitted with the weakest defensible confidence. An edge we are
not sure about is still worth reporting - silence would be worse - but it must
never be reported as certain.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.lineage import Node as LineageNode
from sqlglot.lineage import lineage as sqlglot_lineage

from whatbreaks.lineage.schema_inference import InferenceResult
from whatbreaks.lineage.uncertainty import Confidence, Resolution, confidence_for
from whatbreaks.manifest.models import Manifest, Node
from whatbreaks.sql.dialect import dialect_for
from whatbreaks.sql.recovery import SqlRecovery

# A pathological model should not be able to hang the run.
MAX_COLUMNS_PER_MODEL = 2_000


class EdgeKind(str, Enum):
    """Why an upstream column reaches a downstream one."""

    DIRECT = "direct"
    EXPRESSION = "expression"
    AGGREGATE = "aggregate"
    STAR_EXPANDED = "star_expanded"
    PREDICATE = "predicate"
    UNKNOWN = "unknown"

    @property
    def description(self) -> str:
        return {
            EdgeKind.DIRECT: "selected directly",
            EdgeKind.EXPRESSION: "used in an expression",
            EdgeKind.AGGREGATE: "used in an aggregate",
            EdgeKind.STAR_EXPANDED: "included via SELECT *",
            EdgeKind.PREDICATE: "used in a filter, join or grouping",
            EdgeKind.UNKNOWN: "referenced",
        }[self]

    @property
    def breaks_query(self) -> bool:
        """Does removing the upstream column make the query fail outright?

        `STAR_EXPANDED` is the exception and the reason this distinction exists:
        `select *` does not error when a column disappears, it silently produces
        a narrower result. Different breakage, different message.
        """
        return self is not EdgeKind.STAR_EXPANDED


@dataclass(frozen=True, slots=True, order=True)
class ColumnRef:
    """A column belonging to a specific manifest node."""

    node_id: str
    column: str

    def __str__(self) -> str:
        return f"{self.node_id}.{self.column}"


@dataclass(frozen=True, slots=True)
class ColumnEdge:
    downstream: ColumnRef
    upstream: ColumnRef
    kind: EdgeKind = EdgeKind.UNKNOWN
    confidence: Confidence = Confidence.UNKNOWN

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.downstream.node_id,
            self.downstream.column,
            self.upstream.node_id,
            self.upstream.column,
        )


@dataclass(frozen=True, slots=True)
class RequiredColumn:
    """An upstream column a model needs, but which feeds no output column.

    Filters, join keys and GROUP BY terms. Removing one breaks the query
    without changing the downstream schema, so projection lineage alone misses
    them entirely - found by the invariant oracle in
    `tests/unit/test_lineage_invariant.py`, not by inspection.
    """

    downstream_model: str
    upstream: ColumnRef
    kind: EdgeKind = EdgeKind.PREDICATE
    confidence: Confidence = Confidence.UNKNOWN


@dataclass(frozen=True, slots=True)
class ColumnGraph:
    edges: tuple[ColumnEdge, ...] = ()
    unresolved: tuple[ColumnRef, ...] = ()
    required: tuple[RequiredColumn, ...] = ()
    # Every parent column each model MENTIONS, whether or not it is projected
    # and whether or not the parent still has it. Derived from the SQL text, so
    # it survives the parent's schema changing underneath - which is exactly
    # what happens on the head side of a column removal.
    references: tuple[RequiredColumn, ...] = ()

    def mentions(self, ref: ColumnRef) -> tuple[str, ...]:
        """Models whose SQL still names `ref`, regardless of resolution.

        The question "did this pull request also update the consumers?" cannot
        be answered from the head graph's edges: once the column is gone from
        the parent, nothing resolves against it and every edge disappears.
        Textual references survive that, so this is what makes the difference
        between a real break and a change the author already handled.
        """
        return tuple(sorted({r.downstream_model for r in self.references if r.upstream == ref}))

    def upstream_of(self, ref: ColumnRef) -> tuple[ColumnEdge, ...]:
        return tuple(e for e in self.edges if e.downstream == ref)

    def downstream_of(self, ref: ColumnRef) -> tuple[ColumnEdge, ...]:
        """Direct consumers of `ref`. One hop; see impact.blast_radius for all."""
        return tuple(e for e in self.edges if e.upstream == ref)

    def consumers(self, ref: ColumnRef) -> tuple[ColumnRef, ...]:
        return tuple(sorted({e.downstream for e in self.downstream_of(ref)}))

    def dependents_of(self, ref: ColumnRef) -> tuple[str, ...]:
        """Every model that breaks if `ref` disappears.

        Union of models consuming it into an output column and models needing
        it only for a filter, join or grouping. The second group is invisible
        to projection lineage but breaks just as hard.
        """
        models = {e.downstream.node_id for e in self.downstream_of(ref)}
        models |= {r.downstream_model for r in self.required if r.upstream == ref}
        return tuple(sorted(models))

    def stats(self) -> dict[str, int]:
        by_confidence = {c.label: 0 for c in Confidence}
        for edge in self.edges:
            by_confidence[edge.confidence.label] += 1
        return {
            "edges": len(self.edges),
            "required": len(self.required),
            "unresolved": len(self.unresolved),
            **by_confidence,
        }


class ColumnGraphBuilder:
    """Builds a column graph for a whole manifest."""

    def __init__(
        self,
        manifest: Manifest,
        recovery: SqlRecovery,
        inference: InferenceResult,
    ) -> None:
        self._manifest = manifest
        self._recovery = recovery
        self._inference = inference
        self._dialect = dialect_for(manifest.adapter_type)

    def build(self) -> ColumnGraph:
        edges: list[ColumnEdge] = []
        unresolved: list[ColumnRef] = []
        required: list[RequiredColumn] = []
        references: list[RequiredColumn] = []
        for uid, node in self._manifest.nodes.items():
            if not node.is_executable_sql:
                continue
            schema = self._inference.schemas.get(uid)
            # Edges need a resolved schema; textual references do not - and
            # they are needed MOST when resolution failed, because a model
            # broken *by* the change under review is exactly the one whose
            # schema stops resolving. Skipping it here is what let a real
            # breaking change be reported as safe.
            resolution = schema.resolution if schema else Resolution.UNKNOWN
            node_edges, node_unresolved, node_required, node_refs = self._build_for(
                node, resolution, with_edges=bool(schema and schema.columns)
            )
            edges.extend(node_edges)
            unresolved.extend(node_unresolved)
            required.extend(node_required)
            references.extend(node_refs)

        # Deterministic output is a stated NFR, and sqlglot's traversal order
        # is not guaranteed stable across versions.
        edges.sort(key=lambda e: (*e.sort_key, e.kind.value))
        key = lambda r: (r.downstream_model, r.upstream.node_id, r.upstream.column)  # noqa: E731
        required.sort(key=key)
        references.sort(key=key)
        return ColumnGraph(
            tuple(_dedupe(edges)),
            tuple(sorted(set(unresolved))),
            tuple(dict.fromkeys(required)),
            tuple(dict.fromkeys(references)),
        )

    # ------------------------------------------------------------------
    def _build_for(
        self, node: Node, resolution: Resolution, with_edges: bool = True
    ) -> tuple[list[ColumnEdge], list[ColumnRef], list[RequiredColumn], list[RequiredColumn]]:
        recovered = self._recovery.recover(node)
        if not recovered.ok or recovered.sql is None:
            return [], [], [], []

        schema, relation_to_node = self._parent_context(node)
        model_schema = self._inference.schemas.get(node.unique_id)
        columns = (
            model_schema.columns[:MAX_COLUMNS_PER_MODEL] if with_edges and model_schema else ()
        )

        base_confidence = confidence_for(
            resolution=resolution,
            sql_was_compiled_by_dbt=recovered.is_high_fidelity,
        )

        # Column names the model mentions anywhere. An UPSTREAM column absent
        # from this set reached the output only via a star, which is a
        # materially different relationship: "you dropped a column this model
        # passes through" reads very differently from - and breaks differently
        # to - "you dropped a column this model names".
        named = _named_columns(recovered.sql, self._dialect)

        edges: list[ColumnEdge] = []
        unresolved: list[ColumnRef] = []
        for column in columns:
            ref = ColumnRef(node.unique_id, column)
            try:
                root = sqlglot_lineage(
                    column,
                    recovered.sql,
                    schema=schema,
                    dialect=self._dialect,
                )
            except Exception:
                unresolved.append(ref)
                continue
            # The kind describes how the DOWNSTREAM column uses its inputs, so
            # it comes from the root projection - not from the leaf, which only
            # says where the value originated.
            found = self._walk(root, ref, relation_to_node, base_confidence, _kind_of(root), named)
            if found:
                edges.extend(found)
            else:
                # A literal (`select 1 as x`) legitimately has no upstream. Only
                # record genuinely unexplained columns.
                if self._has_table_leaf(root):
                    unresolved.append(ref)

        projected = {e.upstream for e in edges}
        mentioned = self._referenced_columns(recovered.sql, relation_to_node)
        references = [
            RequiredColumn(node.unique_id, ref, EdgeKind.UNKNOWN, base_confidence)
            for ref in mentioned
        ]
        required = [
            RequiredColumn(node.unique_id, ref, EdgeKind.PREDICATE, base_confidence)
            for ref in mentioned
            if ref not in projected
        ]
        return edges, unresolved, required, references

    def _parent_context(self, node: Node) -> tuple[dict[str, Any], dict[str, str]]:
        schema: dict[str, Any] = {}
        relation_to_node: dict[str, str] = {}
        for parent_id in node.depends_on:
            parent = self._manifest.nodes.get(parent_id)
            if parent is None:
                continue
            relation_to_node[parent.relation_key.lower()] = parent_id
            parent_schema = self._inference.schemas.get(parent_id)
            if parent_schema and parent_schema.columns:
                schema[parent.relation_key] = dict.fromkeys(parent_schema.columns, "UNKNOWN")
        return schema, relation_to_node

    def _walk(
        self,
        root: LineageNode,
        downstream: ColumnRef,
        relation_to_node: dict[str, str],
        base_confidence: Confidence,
        kind: EdgeKind,
        named: set[str] | None = None,
    ) -> list[ColumnEdge]:
        """Collect edges from a sqlglot lineage tree.

        Only leaves whose source is a real table matter: intermediate nodes are
        CTEs and subqueries inside this model, which are not separately
        addressable in dbt and would be noise in a PR comment.
        """
        edges: list[ColumnEdge] = []
        seen: set[int] = set()
        stack: list[LineageNode] = [root]
        while stack:
            current = stack.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))

            table = self._leaf_table(current)
            if table is not None:
                parent_id = relation_to_node.get(table.lower())
                column = _column_of(current.name)
                if parent_id and column:
                    # Star-expansion is a property of THIS upstream column: it
                    # reached the output without ever being named.
                    edge_kind = (
                        EdgeKind.STAR_EXPANDED
                        if named is not None and column.lower() not in named
                        else kind
                    )
                    edges.append(
                        ColumnEdge(
                            downstream=downstream,
                            upstream=ColumnRef(parent_id, column),
                            kind=edge_kind,
                            confidence=base_confidence,
                        )
                    )
            stack.extend(current.downstream)
        return edges

    def _referenced_columns(self, sql: str, relation_to_node: dict[str, str]) -> list[ColumnRef]:
        """Every parent column the query touches, anywhere.

        Projection lineage answers "where does this output value come from".
        It cannot answer "what does this query need in order to run", which is
        a superset: WHERE, JOIN ... ON, GROUP BY, HAVING, QUALIFY and ORDER BY
        all reference columns that never reach the output.
        """
        try:
            tree = sqlglot.parse_one(sql, dialect=self._dialect)
        except Exception:
            return []

        # Map every name a column can be qualified by back to real relations.
        #
        # Table aliases are the easy half. The half that matters is CTE names:
        # the canonical dbt model reads `with orders as (select * from
        # {{ ref('stg_orders') }}) ... select orders.status`, so the qualifier
        # is a CTE, not a table. Missing that made the tool report a genuine
        # breaking change on jaffle_shop as SAFE - a false negative, which is
        # the worst outcome this tool can produce.
        alias_to_relations: dict[str, set[str]] = {}

        def note(alias: str, relation: str) -> None:
            if alias and relation:
                alias_to_relations.setdefault(alias.lower(), set()).add(relation.lower())

        for table in tree.find_all(exp.Table):
            real = (table.name or "").lower()
            note(real, real)
            note(table.alias_or_name, real)

        # A CTE resolves to whatever relations it reads from. Chains resolve by
        # repeating until stable, so `a -> b -> real_table` works.
        for _ in range(4):
            changed = False
            for cte in tree.find_all(exp.CTE):
                name = (cte.alias_or_name or "").lower()
                if not name:
                    continue
                targets: set[str] = set()
                for inner in cte.this.find_all(exp.Table) if cte.this else []:
                    targets |= alias_to_relations.get((inner.name or "").lower(), set())
                if targets - alias_to_relations.get(name, set()):
                    alias_to_relations.setdefault(name, set()).update(targets)
                    changed = True
            if not changed:
                break

        # A single parent means unqualified columns can only be its.
        parents = {
            relation
            for relations in alias_to_relations.values()
            for relation in relations
            if relation in relation_to_node
        }
        sole = next(iter(parents)) if len(parents) == 1 else None

        found: list[ColumnRef] = []
        for column in tree.find_all(exp.Column):
            name = column.name
            if not name or name == "*":
                continue
            qualifier = (column.table or "").lower()
            relations = alias_to_relations.get(qualifier, set()) if qualifier else set()
            if not relations and sole is not None:
                relations = {sole}
            for relation in relations:
                parent_id = relation_to_node.get(relation)
                if parent_id:
                    found.append(ColumnRef(parent_id, name))
        return list(dict.fromkeys(found))

    @staticmethod
    def _leaf_table(node: LineageNode) -> str | None:
        source = getattr(node, "source", None)
        if isinstance(source, exp.Table):
            return source.name
        return None

    def _has_table_leaf(self, root: LineageNode) -> bool:
        stack = [root]
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if self._leaf_table(current) is not None:
                return True
            stack.extend(current.downstream)
        return False


def _dedupe(edges: list[ColumnEdge]) -> list[ColumnEdge]:
    """Keep the strongest edge per (downstream, upstream) pair.

    sqlglot can reach the same source column by several paths (a join key that
    is also projected, say). Reporting it twice would double-count blast radius.
    """
    best: dict[tuple[ColumnRef, ColumnRef], ColumnEdge] = {}
    for edge in edges:
        key = (edge.downstream, edge.upstream)
        current = best.get(key)
        if current is None or edge.confidence > current.confidence:
            best[key] = edge
    return [best[k] for k in sorted(best, key=lambda k: (str(k[0]), str(k[1])))]


def _column_of(name: str) -> str:
    """sqlglot names leaf nodes `table.column`; we want the column."""
    if not name:
        return ""
    return name.rsplit(".", 1)[-1].strip('"')


def _named_columns(sql: str, dialect: str | None) -> set[str] | None:
    """Every column name the query mentions explicitly, in ANY scope.

    Deliberately not limited to the top-level projection. The dominant dbt
    idiom ends in `select * from final`, so a top-level check labels every
    column star-expanded - including ones computed by name inside a CTE, such
    as `count(order_id) as number_of_orders`. Dropping `order_id` there
    *errors*; calling it star-expanded understates breakage on the single most
    common pattern in dbt. Found by running the CLI on jaffle_shop, not by
    reading the code.

    `None` means we could not parse, in which case nothing is claimed to be
    star-expanded rather than guessed at.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    names: set[str] = set()
    for column in tree.find_all(exp.Column):
        name = column.name
        if name and name != "*":
            names.add(name.lower())
    return names


def _kind_of(node: LineageNode) -> EdgeKind:
    """Classify the downstream projection: a plain reference, an aggregate, or
    some other expression."""
    expression = getattr(node, "expression", None)
    if expression is None:
        return EdgeKind.UNKNOWN
    inner = expression.this if isinstance(expression, exp.Alias) else expression
    if inner is None:
        return EdgeKind.UNKNOWN
    if isinstance(inner, exp.Star):
        return EdgeKind.STAR_EXPANDED
    if isinstance(inner, exp.AggFunc) or (
        isinstance(inner, exp.Expression) and inner.find(exp.AggFunc) is not None
    ):
        return EdgeKind.AGGREGATE
    if isinstance(inner, exp.Column):
        return EdgeKind.DIRECT
    return EdgeKind.EXPRESSION


def build_column_graph(
    manifest: Manifest,
    recovery: SqlRecovery,
    inference: InferenceResult,
) -> ColumnGraph:
    return ColumnGraphBuilder(manifest, recovery, inference).build()
