"""Build the column graph over every real manifest, for scale and sanity.

Unit tests prove the graph is right on SQL I chose. This proves it survives SQL
I did not, at realistic size, and reports how much of each project it can
actually say something about.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

from whatbreaks.errors import InputError
from whatbreaks.lineage import Confidence, SchemaInference, build_column_graph
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery

root = Path(sys.argv[1])
kinds: Counter[str] = Counter()
totals = Counter()
rows = []

for path in sorted(root.rglob("target/manifest.json")):
    if "dbt_packages" in path.parts:
        continue
    project = path.relative_to(root).parts[0]
    try:
        manifest = load_manifest(path)
    except InputError as exc:
        print(f"  SKIP {project}: {exc}")
        continue

    project_root = path.parent.parent
    started = time.perf_counter()
    recovery = SqlRecovery(manifest, project_root=project_root)
    inference = SchemaInference(manifest, recovery, project_root=project_root).infer()
    graph = build_column_graph(manifest, recovery, inference)
    elapsed = time.perf_counter() - started

    for edge in graph.edges:
        kinds[edge.kind.value] += 1
        totals[f"conf_{edge.confidence.label}"] += 1
    models_with_edges = len({e.downstream.node_id for e in graph.edges})
    stats = graph.stats()
    totals["models"] += len(manifest.models)
    totals["edges"] += stats["edges"]
    totals["required"] += stats["required"]
    totals["unresolved"] += stats["unresolved"]
    totals["covered"] += models_with_edges

    rows.append(
        (
            project,
            len(manifest.models),
            models_with_edges,
            stats["edges"],
            stats["required"],
            stats["unresolved"],
            elapsed,
        )
    )

hdr = (
    f"{'project':<22}{'models':>7}{'covered':>9}{'edges':>8}{'required':>10}{'unres':>7}{'secs':>8}"
)
print(hdr)
print("-" * len(hdr))
for project, models, covered, edges, required, unres, elapsed in rows:
    print(f"{project:<22}{models:>7}{covered:>9}{edges:>8}{required:>10}{unres:>7}{elapsed:>8.1f}")
print("-" * len(hdr))
print(
    f"{'TOTAL':<22}{totals['models']:>7}{totals['covered']:>9}"
    f"{totals['edges']:>8}{totals['required']:>10}{totals['unresolved']:>7}"
)

pct = 100.0 * totals["covered"] / totals["models"] if totals["models"] else 0.0
print(f"\nmodels with at least one column edge: {pct:.1f}%")
print("\nedge kinds:")
for kind, count in kinds.most_common():
    print(f"  {count:>7}  {kind}")

print("\nedge confidence:")
for level in Confidence:
    print(f"  {totals[f'conf_{level.label}']:>7}  {level.label}")

# None of these manifests carry compiled_code (dbt parse yields 0%, ADR 000 F1),
# so every edge is built from SQL we rendered ourselves. A CONFIRMED edge here
# would mean the confidence algebra is leaking certainty it has not earned.
confirmed = totals["conf_confirmed"]
status = "OK" if confirmed == 0 else "BUG"
print(f"\nsanity [{status}]: CONFIRMED edges from rendered SQL = {confirmed} (must be 0)")
