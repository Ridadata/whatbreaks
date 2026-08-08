"""Harness for the false-positive corpus.

Each case is a YAML file describing the same project before and after a change
that MUST NOT alarm anyone. The runner builds two manifests from it and asserts
that whatbreaks reports nothing at or above POSSIBLY_BREAKING.

Kept data-driven on purpose: adding a case is adding a file, which makes "here
is SQL you get wrong" the cheapest possible contribution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CASES_DIR = Path(__file__).parent / "cases"


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    why: str
    models: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> Case:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            name=path.stem,
            description=str(payload.get("description") or path.stem),
            why=str(payload.get("why") or ""),
            models=payload.get("models") or {},
        )

    def manifest(self, side: str) -> dict[str, Any]:
        """Build a manifest for `base` or `head`.

        A model with no `head:` key is unchanged, which keeps cases focused on
        the one thing they are about.
        """
        nodes: dict[str, Any] = {}
        for name, spec in self.models.items():
            sql = spec.get(side, spec.get("base", ""))
            depends = [f"model.fp.{d}" for d in (spec.get("depends_on") or [])]
            nodes[f"model.fp.{name}"] = {
                "name": name,
                "resource_type": "model",
                "package_name": "fp",
                "original_file_path": f"models/{name}.sql",
                "raw_code": sql,
                "depends_on": {"nodes": depends},
                "columns": {c: {"name": c} for c in (spec.get("columns") or [])},
                "config": spec.get("config") or {},
            }
        return {
            "metadata": {
                "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                "dbt_version": "1.11.12",
                "adapter_type": "duckdb",
                "project_name": "fp",
            },
            "nodes": nodes,
            "sources": {},
            "exposures": {},
            "macros": {},
        }

    def write(self, root: Path, side: str) -> Path:
        target = root / side / "target"
        target.mkdir(parents=True, exist_ok=True)
        (root / side / "dbt_project.yml").write_text("name: fp\n", encoding="utf-8")
        path = target / "manifest.json"
        path.write_text(json.dumps(self.manifest(side)), encoding="utf-8")
        return path


def all_cases() -> list[Case]:
    return [Case.load(p) for p in sorted(CASES_DIR.glob("*.yaml"))]
