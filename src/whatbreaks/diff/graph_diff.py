"""Diff two analyses: what changed in the project's column contracts.

**This is a graph diff, not a text diff**, and the distinction is the reason
the tool works.

A text diff tells you which files changed. It reports every reformatting and
CTE rename as a change, and it misses the case that actually hurts: a model
whose output columns changed because an *upstream* `SELECT *` changed, with no
edit to its own file at all.

Diffing the computed contracts is immune to formatting noise by construction
and catches indirect change for free.

One honesty rule governs everything here:

    A column can only be reported as removed if we KNEW the base schema.

If base resolution was UNKNOWN, its absence in head is not evidence of removal -
we never knew it was there. Reporting that would be inventing a finding out of
our own ignorance, which is exactly the failure mode this project exists to
avoid.
"""

from __future__ import annotations

from dataclasses import dataclass

from whatbreaks.analysis import Analysis
from whatbreaks.lineage.column_graph import ColumnRef
from whatbreaks.lineage.uncertainty import Resolution


@dataclass(frozen=True, slots=True)
class ModelChange:
    node_id: str
    name: str
    removed_columns: tuple[str, ...] = ()
    added_columns: tuple[str, ...] = ()
    base_resolution: Resolution = Resolution.UNKNOWN
    head_resolution: Resolution = Resolution.UNKNOWN

    @property
    def comparable(self) -> bool:
        """Did we know enough on BOTH sides to trust a column-level diff?"""
        return self.base_resolution is Resolution.EXACT and (
            self.head_resolution is Resolution.EXACT
        )


@dataclass(frozen=True, slots=True)
class GraphDiff:
    removed_models: tuple[str, ...] = ()
    added_models: tuple[str, ...] = ()
    changed: tuple[ModelChange, ...] = ()
    # models present in both but not comparable, so silently skipped otherwise
    incomparable: tuple[tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.removed_models or self.added_models or self.changed)

    def removed_column_refs(self) -> tuple[ColumnRef, ...]:
        return tuple(
            ColumnRef(change.node_id, column)
            for change in self.changed
            for column in change.removed_columns
        )


def diff_analyses(base: Analysis, head: Analysis) -> GraphDiff:
    base_models = base.manifest.models
    head_models = head.manifest.models

    removed = tuple(
        sorted(node.name for uid, node in base_models.items() if uid not in head_models)
    )
    added = tuple(sorted(node.name for uid, node in head_models.items() if uid not in base_models))

    changes: list[ModelChange] = []
    incomparable: list[tuple[str, str]] = []

    for uid, head_node in head_models.items():
        if uid not in base_models:
            continue
        base_schema = base.inference.schemas.get(uid)
        head_schema = head.inference.schemas.get(uid)
        if base_schema is None or head_schema is None:
            continue

        change = ModelChange(
            node_id=uid,
            name=head_node.name,
            base_resolution=base_schema.resolution,
            head_resolution=head_schema.resolution,
        )
        if not change.comparable:
            # Recorded, not dropped. A model we could not compare is a gap in
            # the answer and must appear in the report rather than vanishing.
            incomparable.append((head_node.name, _why_incomparable(change)))
            continue

        base_cols = list(base_schema.columns)
        head_cols = set(head_schema.columns)
        base_set = set(base_cols)

        removed_cols = tuple(c for c in base_cols if c not in head_cols)
        added_cols = tuple(c for c in head_schema.columns if c not in base_set)
        if removed_cols or added_cols:
            changes.append(
                ModelChange(
                    node_id=uid,
                    name=head_node.name,
                    removed_columns=removed_cols,
                    added_columns=added_cols,
                    base_resolution=base_schema.resolution,
                    head_resolution=head_schema.resolution,
                )
            )

    changes.sort(key=lambda c: c.name)
    return GraphDiff(
        removed_models=removed,
        added_models=added,
        changed=tuple(changes),
        incomparable=tuple(sorted(incomparable)),
    )


def _why_incomparable(change: ModelChange) -> str:
    if change.base_resolution is not Resolution.EXACT:
        return f"base schema was {change.base_resolution.value}"
    return f"head schema is {change.head_resolution.value}"
