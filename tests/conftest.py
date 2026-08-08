from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest


class NetworkAccessError(AssertionError):
    """whatbreaks opened a socket. That is always a bug."""


@pytest.fixture(autouse=True)
def _forbid_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any network call fail, for every test.

    "No network calls, ever" is both a determinism guarantee and a security
    property -- a tool with nothing to exfiltrate with is much easier to trust
    in CI. Enforced here rather than documented, because a documented promise
    is not a promise.

    `oracle`-marked tests are exempt: they shell out to real dbt.
    """
    if request.node.get_closest_marker("oracle"):
        return

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise NetworkAccessError(
            "whatbreaks attempted to open a socket; it must never touch the network"
        )

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def manifest_payload(
    *,
    schema_version: int = 12,
    adapter_type: str = "duckdb",
    nodes: dict[str, Any] | None = None,
    sources: dict[str, Any] | None = None,
    exposures: dict[str, Any] | None = None,
    macros: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal but structurally faithful dbt manifest."""
    return {
        "metadata": {
            "dbt_schema_version": (
                f"https://schemas.getdbt.com/dbt/manifest/v{schema_version}.json"
            ),
            "dbt_version": "1.11.12",
            "adapter_type": adapter_type,
            "project_name": "testproj",
        },
        "nodes": nodes or {},
        "sources": sources or {},
        "exposures": exposures or {},
        "macros": macros or {},
    }


def model_node(
    name: str,
    *,
    raw_code: str = "select 1 as x",
    depends_on: list[str] | None = None,
    columns: list[str] | None = None,
    **config: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "resource_type": "model",
        "package_name": "testproj",
        "original_file_path": f"models/{name}.sql",
        "raw_code": raw_code,
        "depends_on": {"nodes": depends_on or []},
        "columns": {c: {"name": c} for c in (columns or [])},
        "config": config,
    }


@pytest.fixture
def write_manifest(tmp_path: Path):
    def _write(payload: dict[str, Any], filename: str = "manifest.json") -> Path:
        path = tmp_path / filename
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    return _write
