"""Verify the performance and determinism NFRs on a generated project.

The plan states <20s cold for 500 models. Asserting that without measuring it
would be exactly the kind of unverified claim this project avoids elsewhere.

Generates a synthetic project of a given size, runs the full pipeline twice, and
reports timings plus whether the two runs produced identical output.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from whatbreaks.analysis import Analysis
from whatbreaks.diff import classify, diff_analyses


def build_project(root: Path, n_models: int, *, drop_column: bool = False) -> Path:
    """A wide, deep, realistically-shaped project.

    Chained models with several columns each, using the dominant dbt idiom
    (`with ... select * from final`) so the benchmark exercises the real path
    rather than a trivial one.
    """
    nodes: dict[str, dict] = {}
    nodes["seed.bench.raw"] = {
        "name": "raw",
        "resource_type": "seed",
        "package_name": "bench",
        "original_file_path": "seeds/raw.csv",
        "depends_on": {"nodes": []},
        "columns": {c: {"name": c} for c in ("id", "a", "b", "c", "d")},
        "config": {},
    }
    previous = "raw"
    previous_id = "seed.bench.raw"
    for i in range(n_models):
        name = f"model_{i:04d}"
        cols = ["id", "a", "b", "c"] + ([] if drop_column and i == 0 else ["d"])
        projection = ", ".join(f"{c} as {c}" for c in cols)
        nodes[f"model.bench.{name}"] = {
            "name": name,
            "resource_type": "model",
            "package_name": "bench",
            "original_file_path": f"models/{name}.sql",
            "raw_code": (
                f"with src as (select * from {{{{ ref('{previous}') }}}}), "
                f"final as (select {projection} from src where id is not null) "
                f"select * from final"
            ),
            "depends_on": {"nodes": [previous_id]},
            "columns": {},
            "config": {},
        }
        previous, previous_id = name, f"model.bench.{name}"

    target = root / "target"
    target.mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text("name: bench\n", encoding="utf-8")
    path = target / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                    "dbt_version": "1.11.12",
                    "adapter_type": "duckdb",
                    "project_name": "bench",
                },
                "nodes": nodes,
                "sources": {},
                "exposures": {},
                "macros": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", type=int, default=500)
    ap.add_argument("--target-seconds", type=float, default=20.0)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        base_manifest = build_project(root / "base", args.models)
        head_manifest = build_project(root / "head", args.models, drop_column=True)

        started = time.perf_counter()
        base = Analysis.run(base_manifest)
        base_elapsed = time.perf_counter() - started

        started = time.perf_counter()
        head = Analysis.run(head_manifest)
        head_elapsed = time.perf_counter() - started

        started = time.perf_counter()
        findings = classify(diff_analyses(base, head), head, base)
        diff_elapsed = time.perf_counter() - started

        total = base_elapsed + head_elapsed + diff_elapsed
        coverage = findings.coverage
        assert coverage is not None

        print(f"models per side      : {args.models}")
        print(f"base analysis        : {base_elapsed:6.2f}s")
        print(f"head analysis        : {head_elapsed:6.2f}s")
        print(f"diff + classify      : {diff_elapsed:6.2f}s")
        print(f"TOTAL                : {total:6.2f}s   (target < {args.target_seconds}s)")
        print(f"coverage             : {coverage.exact}/{coverage.total_models} exact")
        print(f"findings             : {len(findings.items)}")
        print(f"edges                : {base.graph.stats()['edges']}")

        # Determinism is a stated NFR and a precondition for caching later.
        again = classify(diff_analyses(base, head), head, base)
        identical = [f.sort_key for f in findings.items] == [f.sort_key for f in again.items]
        print(f"deterministic        : {identical}")

        ok = total < args.target_seconds and identical and coverage.exact > 0
        print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
