"""One pass over a dbt project: recover SQL, infer schemas, build the graph.

Everything downstream - the debug commands now, the diff and PR comment later -
needs the same three stages wired together against the same manifest. Doing it
once here keeps the wiring in a single place and makes the coverage report a
first-class output rather than something each caller reassembles.

The coverage report is not optional. Printing "no breaking changes found"
without saying how much of the project was actually analysed is a lie by
omission, and it is the specific failure this project exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from whatbreaks.lineage.column_graph import ColumnGraph, build_column_graph
from whatbreaks.lineage.schema_inference import InferenceResult, SchemaInference
from whatbreaks.lineage.uncertainty import Resolution, UnknownReason
from whatbreaks.manifest.loader import load_manifest
from whatbreaks.manifest.models import Manifest
from whatbreaks.sql.recovery import SqlRecovery


def infer_project_root(manifest_path: Path) -> Path | None:
    """dbt writes `<project>/target/manifest.json`, so the root is two up.

    Worth having because the root unlocks seed CSV headers and project vars,
    both of which materially improve resolution and neither of which the
    manifest carries.
    """
    manifest_path = Path(manifest_path)
    parent = manifest_path.parent
    if parent.name == "target" and (parent.parent / "dbt_project.yml").is_file():
        return parent.parent
    return None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """What we could and could not analyse. Always reported, never suppressed."""

    total_models: int
    exact: int
    partial: int
    unknown: int
    reasons: dict[str, int]
    macro_stats: dict[str, int]
    unanalysable: tuple[tuple[str, str], ...]

    @property
    def analysed(self) -> int:
        return self.exact + self.partial

    @property
    def analysed_pct(self) -> float:
        if not self.total_models:
            return 0.0
        return round(100.0 * self.analysed / self.total_models, 1)

    @property
    def is_complete(self) -> bool:
        return self.unknown == 0 and self.partial == 0

    @property
    def catalog_would_help(self) -> int:
        """How many gaps a `catalog.json` would close.

        Reported so the claim that catalog.json is an *optional* upgrade stays
        honest and quantified rather than asserted.
        """
        return sum(
            count
            for name, count in self.reasons.items()
            if UnknownReason(name).is_fixable_with_catalog
        )

    def headline(self) -> str:
        return (
            f"analysed {self.analysed}/{self.total_models} models "
            f"({self.analysed_pct}%) - {self.exact} exact, "
            f"{self.partial} partial, {self.unknown} not analysed"
        )


@dataclass(frozen=True, slots=True)
class Analysis:
    manifest: Manifest
    recovery: SqlRecovery
    inference: InferenceResult
    graph: ColumnGraph
    project_root: Path | None

    @classmethod
    def run(cls, manifest_path: Path | str, project_root: Path | None = None) -> Analysis:
        manifest_path = Path(manifest_path)
        root = project_root or infer_project_root(manifest_path)
        manifest = load_manifest(manifest_path)
        recovery = SqlRecovery(manifest, project_root=root)
        inference = SchemaInference(manifest, recovery, project_root=root).infer()
        graph = build_column_graph(manifest, recovery, inference)
        return cls(manifest, recovery, inference, graph, root)

    def coverage(self) -> CoverageReport:
        models = self.manifest.models
        counts = {Resolution.EXACT: 0, Resolution.PARTIAL: 0, Resolution.UNKNOWN: 0}
        reasons: dict[str, int] = {}
        unanalysable: list[tuple[str, str]] = []

        for uid, node in models.items():
            schema = self.inference.schemas.get(uid)
            if schema is None:
                counts[Resolution.UNKNOWN] += 1
                continue
            counts[schema.resolution] += 1
            reason = schema.uncertainty.reason
            if reason is not UnknownReason.NONE:
                reasons[reason.value] = reasons.get(reason.value, 0) + 1
                if schema.resolution is Resolution.UNKNOWN:
                    unanalysable.append((node.name, reason.explanation))

        return CoverageReport(
            total_models=len(models),
            exact=counts[Resolution.EXACT],
            partial=counts[Resolution.PARTIAL],
            unknown=counts[Resolution.UNKNOWN],
            reasons=dict(sorted(reasons.items())),
            macro_stats=self.recovery.macro_stats,
            unanalysable=tuple(sorted(unanalysable)),
        )
