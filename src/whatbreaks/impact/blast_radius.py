"""What actually breaks if a set of columns disappears.

The whole point of the tool is that this is computed at **column** granularity.
`dbt ls --select model+` answers the model-level question and flags 40 models
when 2 touch the column you changed; the noise is why nobody runs it.

Two things are traversed together, because both break and only one is visible
to projection lineage:

* column edges - the changed column feeds a downstream output column;
* required columns - the downstream model only filters, joins or groups on it,
  so it never reaches an output but its removal still errors.

Traversal is over the **base** graph, i.e. the world before the change, and
this is not interchangeable with head. Once a column is gone from its parent
nothing resolves against it, so the head graph shows no consumers at all and
would answer "nothing breaks" for every removal. Base says what depended on the
column; `ColumnGraph.mentions` on the head side answers the separate question of
whether the author already updated those consumers.

Getting this backwards produced a false negative on jaffle_shop - a real
breaking change reported as safe - which is why it is spelled out here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from whatbreaks.lineage.column_graph import ColumnGraph, ColumnRef, EdgeKind
from whatbreaks.lineage.uncertainty import Confidence
from whatbreaks.manifest.models import Manifest


@dataclass(frozen=True, slots=True)
class BlastRadius:
    """Everything downstream of a change, with the evidence for each part."""

    columns: tuple[ColumnRef, ...] = ()
    models: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    exposures: tuple[str, ...] = ()
    # models that break without any of their output columns changing
    query_breaks: tuple[str, ...] = ()
    confidence: Confidence = Confidence.UNKNOWN

    @property
    def is_empty(self) -> bool:
        return not (self.columns or self.models or self.tests or self.exposures)

    @property
    def total(self) -> int:
        return len(self.columns) + len(self.tests) + len(self.exposures)

    def summary(self) -> str:
        if self.is_empty:
            return "no downstream consumers found"
        parts = []
        if self.columns:
            parts.append(f"{len(self.columns)} column{'s' if len(self.columns) != 1 else ''}")
        if self.models:
            parts.append(f"{len(self.models)} model{'s' if len(self.models) != 1 else ''}")
        if self.tests:
            parts.append(f"{len(self.tests)} test{'s' if len(self.tests) != 1 else ''}")
        if self.exposures:
            parts.append(f"{len(self.exposures)} exposure{'s' if len(self.exposures) != 1 else ''}")
        return ", ".join(parts)


def compute_blast_radius(
    graph: ColumnGraph,
    manifest: Manifest,
    seeds: Iterable[ColumnRef],
) -> BlastRadius:
    """Transitively reachable consumers of `seeds`.

    Confidence is the weakest of every edge traversed to get there - a chain is
    only as trustworthy as its least certain link, and a five-hop path through
    one PARTIAL schema is not a confident claim.
    """
    seed_set = set(seeds)
    if not seed_set:
        return BlastRadius(confidence=Confidence.UNKNOWN)

    reached_columns: set[ColumnRef] = set()
    query_breaks: set[str] = set()
    confidences: list[Confidence] = []

    frontier = list(seed_set)
    seen: set[ColumnRef] = set(seed_set)
    while frontier:
        current = frontier.pop()

        for edge in graph.downstream_of(current):
            confidences.append(edge.confidence)
            if edge.downstream not in seen:
                seen.add(edge.downstream)
                reached_columns.add(edge.downstream)
                frontier.append(edge.downstream)
            else:
                reached_columns.add(edge.downstream)

        # A model that only filters on the column breaks without any of its
        # own columns changing, so it terminates the walk rather than
        # propagating - nothing downstream of it sees a schema change.
        for requirement in graph.required:
            if requirement.upstream == current:
                query_breaks.add(requirement.downstream_model)
                confidences.append(requirement.confidence)

    affected_models = {ref.node_id for ref in reached_columns} | query_breaks
    all_affected = affected_models | {ref.node_id for ref in seed_set}

    tests = _affected_tests(manifest, seed_set, all_affected)
    exposures = tuple(
        sorted(
            exposure.name
            for exposure in manifest.exposures.values()
            if set(exposure.depends_on) & all_affected
        )
    )

    return BlastRadius(
        columns=tuple(sorted(reached_columns)),
        models=tuple(sorted(affected_models)),
        tests=tests,
        exposures=exposures,
        query_breaks=tuple(sorted(query_breaks)),
        confidence=Confidence.weakest(confidences) if confidences else Confidence.UNKNOWN,
    )


def _affected_tests(
    manifest: Manifest,
    seeds: set[ColumnRef],
    affected_models: set[str],
) -> tuple[str, ...]:
    """Tests that break.

    A column test on a removed column is a certain break. A model-level test on
    an affected model is weaker evidence, but still worth surfacing: it is the
    thing that will actually go red in CI.
    """
    seed_columns_by_model: dict[str, set[str]] = {}
    for ref in seeds:
        seed_columns_by_model.setdefault(ref.node_id, set()).add(ref.column)

    hits: set[str] = set()
    for test in manifest.tests.values():
        attached = test.attached_node
        touched = set(test.depends_on) & affected_models
        if not touched and attached not in affected_models:
            continue
        if test.column_name:
            # a column-scoped test only breaks if it names an affected column
            targets = seed_columns_by_model.get(attached or "", set())
            for node_id in test.depends_on:
                targets |= seed_columns_by_model.get(node_id, set())
            if test.column_name in targets:
                hits.add(test.name)
            continue
        hits.add(test.name)
    return tuple(sorted(hits))


def uses_only_star(graph: ColumnGraph, ref: ColumnRef) -> bool:
    """True when every consumer reaches this column via `SELECT *`.

    Matters for severity: those consumers do not error when the column vanishes,
    they silently produce a narrower result. Different breakage, and saying so
    is the difference between a useful warning and a wrong one.
    """
    edges = graph.downstream_of(ref)
    if not edges:
        return False
    return all(e.kind is EdgeKind.STAR_EXPANDED for e in edges)
