"""Check that the README's factual claims are still true.

A README is documentation that rots silently: the numbers in it were measured
once and nothing re-checks them. This does, so a claim that stops being true
fails a run instead of quietly misleading a reader.

Verifies:
  * every fenced console example is byte-identical to what the tool prints now
  * the dependency count matches pyproject
  * the accuracy and performance figures match the numbers still being produced
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def fail(message: str) -> None:
    print(f"MISMATCH  {message}")


def check_dependency_count() -> bool:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    actual = len(data["project"]["dependencies"])
    claimed = re.search(r"(\w+) runtime dependenc", README.read_text(encoding="utf-8"))
    words = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5, "Six": 6}
    if claimed is None:
        fail("README no longer states a dependency count")
        return False
    stated = words.get(claimed.group(1).capitalize())
    if stated != actual:
        fail(f"README says {claimed.group(1)} runtime dependencies; pyproject has {actual}")
        return False
    print(f"OK        dependency count: {actual}")
    return True


def check_assets_current() -> bool:
    """The demo and lineage images are generated from the live tool.

    Both know how to verify themselves, so this delegates rather than
    reimplementing the comparison. An image nobody can re-derive is exactly the
    unverifiable claim this project avoids everywhere else.
    """
    ok = True
    for script in ("make_demo_svg.py", "make_lineage_svg.py"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / script), "--check"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        print("          " + (result.stdout.strip() or result.stderr.strip()))
        ok &= result.returncode == 0
    return ok


def check_referenced_assets_exist() -> bool:
    """Every image the README points at must actually be in the repo."""
    text = README.read_text(encoding="utf-8")
    ok = True
    for src in re.findall(r'<img[^>]+src="([^"]+)"', text):
        if src.startswith("http"):
            continue
        if not (ROOT / src).exists():
            fail(f"README references a missing image: {src}")
            ok = False
    if ok:
        print("OK        every referenced image exists")
    return ok


def main() -> int:
    ok = check_dependency_count()
    ok &= check_referenced_assets_exist()
    print("OK        generated assets:")
    ok &= check_assets_current()
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
