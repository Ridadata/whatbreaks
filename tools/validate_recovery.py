"""Validate the real SqlRecovery against the Phase 0 probe baseline.

The probe (tools/phase0_probe.py) measured 80.5% renderability across the
sample. The production implementation should match or beat that. If it is
materially worse, something was lost in the rewrite and the ADR's numbers no
longer describe the shipped tool.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from whatbreaks.errors import InputError
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery, SqlSource

# from docs/adr/000-feasibility.md section 5.3
PHASE0_RENDERED_PCT = 80.5

root = Path(sys.argv[1])
manifests = sorted(p for p in root.rglob("target/manifest.json") if "dbt_packages" not in p.parts)

total = ok = 0
kinds: Counter[str] = Counter()
details: Counter[str] = Counter()
rows = []

for path in manifests:
    project = path.relative_to(root).parts[0]
    try:
        manifest = load_manifest(path)
    except InputError as exc:
        print(f"  SKIP {project}: {exc}")
        continue

    recovery = SqlRecovery(manifest, project_root=path.parent.parent)
    results = recovery.recover_all()
    n = len(results)
    good = sum(1 for r in results.values() if r.ok)
    for r in results.values():
        if r.failure is not None:
            kinds[r.failure.kind.value] += 1
            details[str(r.failure)[:60]] += 1

    total += n
    ok += good
    stats = recovery.macro_stats
    pct = 100.0 * good / n if n else 0.0
    rows.append((project, n, good, pct, stats))

print(f"{'project':<24}{'models':>7}{'rendered':>10}{'pct':>8}   macros(compiled/plain)")
print("-" * 78)
for project, n, good, pct, stats in rows:
    print(f"{project:<24}{n:>7}{good:>10}{pct:>7.1f}%   {stats['compiled']}/{stats['plain']}")

overall = 100.0 * ok / total if total else 0.0
print("-" * 78)
print(f"{'TOTAL':<24}{total:>7}{ok:>10}{overall:>7.1f}%")
print(f"\nPhase 0 probe baseline: {PHASE0_RENDERED_PCT}%")
delta = overall - PHASE0_RENDERED_PCT
verdict = "MATCHES/BEATS baseline" if delta >= -1.0 else "REGRESSION vs baseline"
print(f"production implementation: {overall:.1f}%  ({delta:+.1f} pp)  -> {verdict}")

print("\nfailure kinds:")
for kind, count in kinds.most_common():
    print(f"  {count:>5}  {kind}")

print("\ntop failure details:")
for detail, count in details.most_common(15):
    print(f"  {count:>5}  {detail}")
