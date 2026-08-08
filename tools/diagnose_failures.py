"""Show the raw_code around a given recovery failure detail, to triage it."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from whatbreaks.errors import InputError
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery

root = Path(sys.argv[1])
wanted = sys.argv[2]
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 3

shown = 0
for path in sorted(root.rglob("target/manifest.json")):
    if "dbt_packages" in path.parts or shown >= limit:
        continue
    try:
        manifest = load_manifest(path)
    except InputError:
        continue
    recovery = SqlRecovery(manifest, project_root=path.parent.parent)
    for uid, result in recovery.recover_all().items():
        if shown >= limit or result.failure is None:
            continue
        if wanted not in str(result.failure):
            continue
        node = manifest.models[uid]
        print("=" * 72)
        print(f"{path.relative_to(root).parts[0]} :: {node.name}  -> {result.failure}")
        for line in node.raw_code.splitlines():
            if re.search(r"\.get\b|\bget\(", line):
                print(f"    | {line.strip()[:150]}")
        shown += 1
