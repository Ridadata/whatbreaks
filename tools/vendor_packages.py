"""
Vendor dbt packages by git clone instead of `dbt deps`.

Why this exists: this machine sits behind an Avast Web/Mail Shield TLS
interception proxy whose CA certificate does not mark Basic Constraints as
critical. OpenSSL 3.x rejects that outright, so any Python HTTPS request to
hub.getdbt.com fails and `dbt deps` can never succeed. Git uses Windows
schannel and is unaffected, so we resolve packages over git and drop them into
`dbt_packages/` -- which is exactly what `dbt deps` would have produced.

RESEARCH SCRIPT. Phase 0 only. Not part of the product.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# hub package name -> github repo
HUB_TO_GITHUB = {
    "dbt-labs/dbt_utils": "https://github.com/dbt-labs/dbt-utils",
    "dbt-labs/spark_utils": "https://github.com/dbt-labs/spark-utils",
    "dbt-labs/audit_helper": "https://github.com/dbt-labs/dbt-audit-helper",
    "dbt-labs/codegen": "https://github.com/dbt-labs/dbt-codegen",
    "dbt-labs/dbt_external_tables": "https://github.com/dbt-labs/dbt-external-tables",
    "calogica/dbt_date": "https://github.com/calogica/dbt-date",
    "calogica/dbt_expectations": "https://github.com/calogica/dbt-expectations",
    "metaplane/dbt_expectations": "https://github.com/metaplane/dbt-expectations",
    "fivetran/fivetran_utils": "https://github.com/fivetran/dbt_fivetran_utils",
    "get-select/dbt_query_tags": "https://github.com/get-select/dbt-query-tags",
    "elementary-data/elementary": "https://github.com/elementary-data/dbt-data-reliability",
    "brooklyn-data/dbt_artifacts": "https://github.com/brooklyn-data/dbt_artifacts",
    "Datavault-UK/automate_dv": "https://github.com/Datavault-UK/automate-dv",
}


def parse_packages_yml(path: Path) -> list[tuple[str, str | None]]:
    """-> [(git_url, revision_or_None)]. Crude but sufficient for Phase 0."""
    out: list[tuple[str, str | None]] = []
    if not path.exists():
        return out
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        s = line.strip().lstrip("-").strip()
        m = re.match(r"^package\s*:\s*(.+)$", s)
        if m:
            name = re.sub(r"\s+#.*$", "", m.group(1)).strip().strip("'\"")
            url = HUB_TO_GITHUB.get(name)
            if url:
                out.append((url, None))
            else:
                owner_repo = name.replace("_", "-")
                out.append((f"https://github.com/{owner_repo}", None))
            continue
        m = re.match(r"^git\s*:\s*(.+)$", s)
        if m:
            url = re.sub(r"\s+#.*$", "", m.group(1)).strip().strip("'\"")
            rev = None
            for nxt in lines[i + 1 : i + 4]:
                r = re.match(r"^\s*revision\s*:\s*(.+)$", nxt)
                if r:
                    rev = re.sub(r"\s+#.*$", "", r.group(1)).strip().strip("'\"")
                    break
            out.append((url, rev))
    return out


def package_name_of(pkg_dir: Path) -> str | None:
    pj = pkg_dir / "dbt_project.yml"
    if not pj.exists():
        return None
    m = re.search(r"^\s*name\s*:\s*(.+)$", pj.read_text(encoding="utf-8", errors="replace"), re.M)
    if not m:
        return None
    return re.sub(r"\s+#.*$", "", m.group(1)).strip().strip("'\"")


def parse_local_packages(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*-?\s*local\s*:\s*(.+)$", line)
        if m:
            out.append(re.sub(r"\s+#.*$", "", m.group(1)).strip().strip("'\""))
    return out


def vendor(project_dir: Path, depth: int = 0, seen: set[str] | None = None) -> None:
    if depth > 3:
        return
    seen = seen if seen is not None else set()
    pkgs_dir = project_dir / "dbt_packages"

    # `local:` deps (common in a package's own integration_tests) are copied,
    # since dbt still expects them materialised under dbt_packages/.
    import shutil

    for rel in parse_local_packages(project_dir / "packages.yml"):
        src = (project_dir / rel).resolve()
        nm = package_name_of(src)
        if not nm:
            print(f"    local SKIP (no dbt_project.yml) {rel}")
            continue
        dst = pkgs_dir / nm
        if dst.exists():
            continue
        pkgs_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns(
                ".git", "dbt_packages", "target", "integration_tests", "logs"
            ),
        )
        print(f"    vendored {nm}  <- local:{rel}")

    for url, rev in parse_packages_yml(project_dir / "packages.yml"):
        if url in seen:
            continue
        seen.add(url)
        pkgs_dir.mkdir(parents=True, exist_ok=True)
        tmp = pkgs_dir / ("_tmp_" + re.sub(r"[^0-9a-zA-Z]", "_", url)[-40:])
        if tmp.exists():
            continue
        cmd = ["git", "clone", "--depth", "1", "--quiet"]
        if rev:
            cmd += ["--branch", rev]
        cmd += [url, str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0 and rev:
            # tag/branch may not exist shallow -- retry default branch
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", url, str(tmp)],
                capture_output=True,
                text=True,
                errors="replace",
            )
        if not tmp.exists():
            print(f"    vendor FAIL {url}")
            continue
        name = package_name_of(tmp)
        if not name:
            print(f"    vendor SKIP (no dbt_project.yml) {url}")
            continue
        final = pkgs_dir / name
        if final.exists():
            continue
        tmp.rename(final)
        print(f"    vendored {name}  <- {url}")
        vendor(final, depth + 1, seen)

    # clean leftovers
    for p in pkgs_dir.glob("_tmp_*"):
        try:
            import shutil

            shutil.rmtree(p, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    root = Path(sys.argv[1])
    for repo in sorted(root.iterdir()):
        if not repo.is_dir():
            continue
        # must match tools/phase0_probe.py target discovery exactly, or we
        # vendor packages into a directory the probe never looks at
        best: tuple[tuple[int, int], Path] | None = None
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
                best = (score, pj.parent)
        if best:
            print(f"  {repo.name}")
            vendor(best[1])
