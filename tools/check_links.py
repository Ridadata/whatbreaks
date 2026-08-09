"""Verify every relative markdown link in the repo resolves.

A README that promises `docs/limitations.md` and 404s is the exact kind of
unpolish that makes a reader distrust everything else on the page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
docs = [
    p
    for p in root.rglob("*.md")
    if not any(part in {".phase0", ".venv", "dbt_packages", "node_modules"} for part in p.parts)
]

broken: list[tuple[Path, str]] = []
checked = 0
for doc in docs:
    for match in LINK.finditer(doc.read_text(encoding="utf-8", errors="replace")):
        target = match.group(1).split("#", 1)[0].strip()
        if not target or target.startswith(SKIP_PREFIXES):
            continue
        checked += 1
        if not (doc.parent / target).resolve().exists():
            broken.append((doc.relative_to(root), target))

for doc, target in broken:
    print(f"BROKEN  {doc}  ->  {target}")
print(f"\nchecked {checked} relative links across {len(docs)} files; {len(broken)} broken")
sys.exit(1 if broken else 0)
