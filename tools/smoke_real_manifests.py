"""Smoke-check the real loader against every manifest Phase 0 produced.

Synthetic fixtures prove the loader does what I expected. This proves it
survives what dbt actually emits.
"""

from __future__ import annotations

import sys
from pathlib import Path

from whatbreaks.errors import InputError
from whatbreaks.graph import CycleError, topological_sort
from whatbreaks.manifest import load_manifest

root = Path(sys.argv[1])
manifests = sorted(p for p in root.rglob("target/manifest.json") if "dbt_packages" not in p.parts)

print(f"{len(manifests)} manifests\n")
ok = fail = 0
for path in manifests:
    project = path.relative_to(root).parts[0]
    try:
        m = load_manifest(path)
        edges = m.dependency_edges()
        order = topological_sort(edges)
        plain = len(m.plain_macros())
        seeded = sum(1 for n in m.nodes.values() if n.declared_column_names)
        print(
            f"  OK   {project:<24} v{m.schema_version} {m.adapter_type:<9} "
            f"models={len(m.models):<5} nodes={len(m.nodes):<5} "
            f"tests={len(m.tests):<4} exp={len(m.exposures):<3} "
            f"macros={len(m.macros):<5} plain={plain:<5} "
            f"declared_cols={seeded:<4} toposorted={len(order)}"
        )
        ok += 1
    except InputError as exc:
        print(f"  SKIP {project:<24} {type(exc).__name__}: {exc}")
        fail += 1
    except CycleError as exc:
        print(f"  FAIL {project:<24} {exc}")
        fail += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  CRASH {project:<24} {type(exc).__name__}: {exc}")
        fail += 1

print(f"\nloaded {ok}, refused/failed {fail}")
