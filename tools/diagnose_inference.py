"""Show models whose schema failed for a given reason, with the offending SQL."""

from __future__ import annotations

import sys
from pathlib import Path

from whatbreaks.errors import InputError
from whatbreaks.lineage import SchemaInference
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery

root = Path(sys.argv[1])
wanted = sys.argv[2]
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 4

shown = 0
for path in sorted(root.rglob("target/manifest.json")):
    if "dbt_packages" in path.parts or shown >= limit:
        continue
    try:
        manifest = load_manifest(path)
    except InputError:
        continue
    project_root = path.parent.parent
    recovery = SqlRecovery(manifest, project_root=project_root)
    result = SchemaInference(manifest, recovery, project_root=project_root).infer()

    for uid, schema in result.schemas.items():
        if shown >= limit or uid not in manifest.models:
            continue
        if schema.uncertainty.reason.value != wanted:
            continue
        node = manifest.models[uid]
        recovered = recovery.recover(node)
        print("=" * 74)
        print(f"{path.relative_to(root).parts[0]} :: {node.name}  [{wanted}]")
        body = (recovered.sql or node.raw_code or "").strip()
        print("\n".join(body.splitlines()[:14])[:900])
        shown += 1
