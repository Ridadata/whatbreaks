"""Read the parts of `dbt_project.yml` the manifest does not carry.

Project-level `vars:` are the notable gap. dbt resolves `var()` at compile
time, not parse time, so a `dbt parse` manifest does not record them -- yet
models reference them constantly. Without the real values the only honest
options are to fail the model or to invent a value, and inventing one silently
changes what the SQL means.

Reading the file directly gives the *actual* values, offline, with no warehouse.
That makes the tool more correct, not merely more permissive, which is the only
reason this module (and its YAML dependency) is justified.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

MAX_PROJECT_FILE_BYTES = 8 * 1024 * 1024


def _flatten_vars(raw: dict[str, Any]) -> dict[str, Any]:
    """dbt allows both flat vars and per-package scoping.

        vars:
          my_var: 1              # global
          some_package:          # scoped to a package
            other_var: 2

    Both are exposed by name. Global values win on collision, matching dbt's
    precedence for a model in the root project.
    """
    scoped: dict[str, Any] = {}
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            # ambiguous: could be a package scope or a genuine dict-valued var
            flat[key] = value
            for inner_key, inner_value in value.items():
                scoped.setdefault(inner_key, inner_value)
        else:
            flat[key] = value
    return {**scoped, **flat}


def read_project_vars(project_root: Path | None) -> dict[str, Any]:
    """Return `vars:` from `dbt_project.yml`, or `{}` if unavailable.

    Never raises. A missing or malformed project file degrades the analysis but
    must not abort it -- the manifest alone is still worth something.
    """
    if project_root is None:
        return {}
    path = project_root / "dbt_project.yml"
    try:
        if not path.is_file() or path.stat().st_size > MAX_PROJECT_FILE_BYTES:
            return {}
        payload = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("vars")
    if not isinstance(raw, dict):
        return {}
    return _flatten_vars(raw)
