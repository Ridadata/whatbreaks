"""Validate SchemaInference against the Phase 0 probe's EXACT rates.

ADR 000 section 5.3 measured 76.8% EXACT (weighted) across the sample. If the
production engine lands materially below that, the ADR's numbers no longer
describe the shipped tool and the gate decision needs revisiting.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from whatbreaks.errors import InputError
from whatbreaks.lineage import Resolution, SchemaInference
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery

PHASE0_EXACT_PCT = 76.8

# ADR 000 section 3: analytics projects are the target population; packages are
# harder by construction and reported separately.
KIND = {
    "dbt_bootcamp": "analytics",
    "jaffle_shop_duckdb": "analytics",
    "dbt_artifacts": "package",
    "dbt_expectations": "package",
    "elementary": "package",
    "gitlab_snowflake": "package",
    "snowflake_monitoring": "package",
}

root = Path(sys.argv[1])
reasons: Counter[str] = Counter()
totals: dict[str, list[int]] = {"analytics": [0, 0], "package": [0, 0]}
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
    recovery = SqlRecovery(manifest, project_root=project_root)
    result = SchemaInference(manifest, recovery, project_root=project_root).infer()

    models = {
        uid: s for uid, s in result.schemas.items() if uid in manifest.models
    }
    n = len(models)
    exact = sum(1 for s in models.values() if s.resolution is Resolution.EXACT)
    partial = sum(1 for s in models.values() if s.resolution is Resolution.PARTIAL)
    drift = sum(1 for s in models.values() if s.has_doc_drift)
    for s in models.values():
        if s.uncertainty.reason.value:
            reasons[s.uncertainty.reason.value] += 1

    kind = KIND.get(project, "package")
    totals[kind][0] += n
    totals[kind][1] += exact
    rows.append((project, kind, n, exact, partial, drift))

print(f"{'project':<22}{'kind':<11}{'models':>7}{'EXACT':>7}{'PART':>6}{'pct':>8}{'drift':>7}")
print("-" * 74)
for project, kind, n, exact, partial, drift in rows:
    pct = 100.0 * exact / n if n else 0.0
    print(f"{project:<22}{kind:<11}{n:>7}{exact:>7}{partial:>6}{pct:>7.1f}%{drift:>7}")

print("-" * 74)
grand_n = sum(v[0] for v in totals.values())
grand_e = sum(v[1] for v in totals.values())
for kind, (n, exact) in totals.items():
    if n:
        print(f"{kind:<22}{'':<11}{n:>7}{exact:>7}{'':>6}{100.0 * exact / n:>7.1f}%")
overall = 100.0 * grand_e / grand_n if grand_n else 0.0
print(f"{'COMBINED':<22}{'':<11}{grand_n:>7}{grand_e:>7}{'':>6}{overall:>7.1f}%")

delta = overall - PHASE0_EXACT_PCT
print(f"\nPhase 0 baseline: {PHASE0_EXACT_PCT}%   production: {overall:.1f}%  ({delta:+.1f} pp)")

# EXACT alone can fall while capability rises, if the bar for EXACT is raised.
# Comparing models with ANY usable schema separates "resolves less" from
# "claims less", which are very different things.
grand_p = sum(r[4] for r in rows)
usable = 100.0 * (grand_e + grand_p) / grand_n if grand_n else 0.0
print(f"any usable schema (EXACT+PARTIAL): {usable:.1f}%  vs Phase 0's 78.0%")
if delta < -1.0 and usable >= 78.0:
    print("-> NOT a capability regression: resolves more, claims less (stricter EXACT bar)")
elif delta >= -1.0:
    print("-> MATCHES/BEATS baseline")
else:
    print("-> REGRESSION vs baseline")

print("\nunknown/partial reasons:")
for reason, count in reasons.most_common():
    print(f"  {count:>5}  {reason}")
