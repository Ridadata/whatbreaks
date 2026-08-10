"""Execute the action's analysis script exactly as GitHub would.

This exists because of a bug that only appeared on a real runner: GitHub invokes
composite-action bash with `-e`, so `whatbreaks` exiting 1 - which is the normal
"found something" result - killed the step before its status could be read. Unit
tests could not see it, and the no-op case passed because it exits 0.

Rather than trusting a code comment not to regress, this extracts the shipped
script out of action.yml and runs it under the same shell flags with the same
environment, then asserts the outputs.

    python tools/check_action_script.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / "action" / "action.yml"

# The flags GitHub uses for `shell: bash` in a composite action.
SHELL_FLAGS = ["--noprofile", "--norc", "-e", "-o", "pipefail"]


def find_bash() -> str:
    """Prefer Git Bash on Windows.

    `bash` on PATH there is usually WSL's, which cannot see `C:/...` paths and
    fails in a way that looks like a script bug rather than a shell mismatch.
    """
    explicit = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    for candidate in explicit:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("bash")
    if found:
        return found
    raise SystemExit("no bash available to run the check")


def analysis_script() -> str:
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    for step in steps:
        if step.get("id") == "run":
            return str(step["run"])
    raise SystemExit("could not find the step with id 'run' in action.yml")


def run_case(bash: str, script: str, base: Path, head: Path, label: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        script_path = tmpdir / "step.sh"
        script_path.write_text(script.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        outputs = tmpdir / "outputs"
        summary = tmpdir / "summary"
        outputs.touch()
        summary.touch()

        # On a runner, `pip install whatbreaks` puts the entry point on PATH.
        # Locally it lives in the venv, so put it there to match.
        scripts_dir = Path(sys.executable).parent
        env = {
            **os.environ,
            "PATH": f"{scripts_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "RUNNER_TEMP": str(tmpdir).replace("\\", "/"),
            "GITHUB_OUTPUT": str(outputs).replace("\\", "/"),
            "GITHUB_STEP_SUMMARY": str(summary).replace("\\", "/"),
            "WB_BASE": str(base).replace("\\", "/"),
            "WB_HEAD": str(head).replace("\\", "/"),
            "WB_BASE_ROOT": "",
            "WB_HEAD_ROOT": "",
            "WB_FAIL_ON": "breaking",
        }
        result = subprocess.run(
            [bash, *SHELL_FLAGS, str(script_path).replace("\\", "/")],
            env=env, capture_output=True, text=True, cwd=ROOT,
        )
        if result.returncode != 0:
            print(f"FAIL  [{label}] the step exited {result.returncode}")
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            raise SystemExit(1)

        parsed = dict(
            line.split("=", 1)
            for line in outputs.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        if not summary.read_text(encoding="utf-8").strip():
            print(f"FAIL  [{label}] nothing written to the job summary")
            raise SystemExit(1)
        return parsed


def main() -> int:
    bash = find_bash()
    script = analysis_script()
    example = ROOT / "examples" / "quickstart"
    base = example / "base" / "target" / "manifest.json"
    head = example / "head" / "target" / "manifest.json"

    breaking = run_case(bash, script, base, head, "breaking change")
    if breaking.get("failed") != "true":
        print(f"FAIL  a breaking change reported failed={breaking.get('failed')!r}")
        return 1
    print("OK    breaking change   -> failed=true, step still exits 0")

    clean = run_case(bash, script, base, base, "no-op")
    if clean.get("failed") != "false":
        print(f"FAIL  a no-op reported failed={clean.get('failed')!r}")
        return 1
    print("OK    no-op             -> failed=false")

    for key in ("markdown-file", "json-file"):
        if key not in breaking:
            print(f"FAIL  output {key} was not set")
            return 1
    print("OK    outputs set       -> markdown-file, json-file")
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
