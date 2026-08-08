"""Phase 0 helper: show which dbt project directory the probe will analyse per repo."""

import sys
from pathlib import Path

root = Path(sys.argv[1])
for repo in sorted(root.iterdir()):
    if not repo.is_dir():
        continue
    best = None
    for pj in repo.rglob("dbt_project.yml"):
        if "dbt_packages" in pj.parts:
            continue
        md = pj.parent / "models"
        if not md.exists():
            continue
        cnt = len(list(md.rglob("*.sql")))
        depth = len(pj.parent.relative_to(repo).parts)
        score = (0 if depth <= 1 else 1, -cnt)
        if cnt >= 5 and (best is None or score < best[0]):
            best = (score, pj.parent, cnt)
    if best:
        print(f"{repo.name:22} models={best[2]:5}  {best[1].relative_to(repo)}")
    else:
        print(f"{repo.name:22} NO USABLE PROJECT")
