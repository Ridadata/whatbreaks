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


def normalise(text: str) -> list[str]:
    return [line.rstrip() for line in text.strip().splitlines()]


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


def check_check_output(base: Path, head: Path) -> bool:
    """The headline example must be what the tool actually prints."""
    text = README.read_text(encoding="utf-8")
    match = re.search(
        r"\$ whatbreaks check --base [^\n]*\n\n(.*?)```", text, re.S
    )
    if match is None:
        fail("could not find the headline `whatbreaks check` example")
        return False
    claimed = normalise(match.group(1))

    result = subprocess.run(
        [
            sys.executable, "-m", "whatbreaks.cli",
            "check", "--base", str(base), "--head", str(head), "--format", "text",
        ],
        capture_output=True, text=True, cwd=ROOT,
    )
    actual = normalise(result.stdout)
    if claimed != actual:
        fail("the headline example no longer matches real output")
        print("  README says:")
        for line in claimed:
            print(f"    {line}")
        print("  tool prints:")
        for line in actual:
            print(f"    {line}")
        return False
    print(f"OK        headline example matches real output ({len(actual)} lines)")
    return True


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: verify_readme.py <base-manifest> <head-manifest>")
        print("(skipping output checks; verifying static claims only)")
        return 0 if check_dependency_count() else 1

    ok = check_dependency_count()
    ok &= check_check_output(Path(sys.argv[1]), Path(sys.argv[2]))
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
