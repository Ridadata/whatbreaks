"""Load and normalise a dbt `manifest.json`.

Treats the manifest as untrusted input. In CI it comes from a pull request that
anyone may have authored, so it gets size caps and a path-escape guard even
though the common case is entirely benign.

Version policy is deliberately strict: an unrecognised schema version is fatal.
Best-effort parsing of an unknown artifact format is precisely how a tool starts
producing confidently wrong answers, which is the one thing whatbreaks must not
do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from whatbreaks.errors import (
    ManifestNotFoundError,
    ManifestParseError,
    ManifestTooLargeError,
    UnsafePathError,
    UnsupportedManifestVersionError,
)
from whatbreaks.manifest.models import (
    Column,
    Exposure,
    Macro,
    Manifest,
    Node,
    ResourceType,
    Test,
)
from whatbreaks.sql.dialect import model_relation_key, source_relation_key

# dbt-core 1.6 -> 1.11 emit v10..v12. Older manifests exist in the wild (a v7
# from dbt 1.3 turned up in the Phase 0 sample) and are rejected rather than
# guessed at.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({10, 11, 12})

MAX_MANIFEST_BYTES = 512 * 1024 * 1024
MAX_NODES = 200_000
MAX_SQL_CHARS = 4 * 1024 * 1024

_SCHEMA_VERSION_RE = re.compile(r"/manifest/v(\d+)\.json")


def _as_dict(value: object) -> dict[str, Any]:
    """Coerce an untrusted manifest field to a dict, never raising.

    Manifest shape drifts between dbt versions and hostile input is possible, so
    every nested lookup goes through this rather than assuming a type.
    """
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return {}


def _schema_version(metadata: dict[str, Any]) -> int:
    raw = metadata.get("dbt_schema_version")
    if not isinstance(raw, str):
        raise ManifestParseError(
            "manifest metadata has no dbt_schema_version",
            remedy="Regenerate it with `dbt parse`.",
        )
    match = _SCHEMA_VERSION_RE.search(raw)
    if not match:
        raise ManifestParseError(f"unrecognised dbt_schema_version: {raw!r}")
    return int(match.group(1))


def _columns(raw: Any) -> dict[str, Column]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Column] = {}
    for name, spec in raw.items():
        if not isinstance(name, str):
            continue
        data_type = spec.get("data_type") if isinstance(spec, dict) else None
        out[name] = Column(name=name, data_type=data_type if isinstance(data_type, str) else None)
    return out


def _depends_on(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        return ()
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return ()
    return tuple(n for n in nodes if isinstance(n, str))


def _truncate_sql(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) > MAX_SQL_CHARS:
        raise ManifestTooLargeError(
            f"a node's SQL exceeds {MAX_SQL_CHARS} characters",
            remedy="This looks malformed or generated. Exclude the model or raise the limit.",
        )
    return value


def check_within_root(path: str, project_root: Path) -> Path:
    """Resolve a manifest-supplied relative path, refusing escapes.

    Manifest paths are attacker-controlled in a fork PR. `../../.ssh/id_rsa` in
    `original_file_path` must not become a file read.
    """
    root = project_root.resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafePathError(
            f"path escapes the project root: {path!r}",
            remedy="The manifest may be malformed or hostile. Rebuild it with `dbt parse`.",
        )
    return candidate


def _build_node(uid: str, raw: dict[str, Any], resource_type: ResourceType) -> Node:
    config = _as_dict(raw.get("config"))
    contract = _as_dict(config.get("contract"))
    name = str(raw.get("name") or "")

    if resource_type is ResourceType.SOURCE:
        source_name = str(raw.get("source_name") or "")
        key = source_relation_key(source_name, name)
    else:
        source_name = None
        key = model_relation_key(name)

    return Node(
        unique_id=uid,
        name=name,
        resource_type=resource_type,
        package_name=str(raw.get("package_name") or ""),
        relation_key=key,
        original_file_path=str(raw.get("original_file_path") or ""),
        depends_on=_depends_on(raw.get("depends_on")),
        columns=_columns(raw.get("columns")),
        raw_code=_truncate_sql(raw.get("raw_code")),
        compiled_code=(
            _truncate_sql(raw.get("compiled_code")) if raw.get("compiled_code") else None
        ),
        materialized=(str(config.get("materialized")) if config.get("materialized") else None),
        contract_enforced=bool(contract.get("enforced")),
        unique_key=(str(config.get("unique_key")) if config.get("unique_key") else None),
        language=str(raw.get("language") or "sql"),
        source_name=source_name,
    )


def load_manifest(path: Path | str) -> Manifest:
    """Read, validate and normalise a manifest. Raises `InputError` subclasses."""
    path = Path(path)
    if not path.is_file():
        raise ManifestNotFoundError(
            f"no manifest at {path}",
            remedy="Run `dbt parse` in the project, then point at target/manifest.json.",
        )

    size = path.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise ManifestTooLargeError(f"manifest is {size} bytes (limit {MAX_MANIFEST_BYTES})")

    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ManifestParseError(
            f"{path} is not valid JSON: {exc}",
            remedy="Regenerate it with `dbt parse`.",
        ) from exc

    if not isinstance(payload, dict) or "metadata" not in payload:
        raise ManifestParseError(f"{path} does not look like a dbt manifest")

    if not isinstance(payload.get("metadata"), dict):
        raise ManifestParseError("manifest metadata is not an object")
    metadata = _as_dict(payload.get("metadata"))

    version = _schema_version(metadata)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(f"v{v}" for v in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise UnsupportedManifestVersionError(
            f"manifest schema v{version} is not supported (supported: {supported})",
            remedy=(
                "whatbreaks refuses to guess at an unknown manifest format. "
                "Regenerate the manifest with a supported dbt-core version (1.6-1.11)."
            ),
        )

    raw_nodes = _as_dict(payload.get("nodes"))
    raw_sources = _as_dict(payload.get("sources"))
    raw_exposures = _as_dict(payload.get("exposures"))
    raw_macros = _as_dict(payload.get("macros"))

    if len(raw_nodes) + len(raw_sources) > MAX_NODES:
        raise ManifestTooLargeError(f"manifest declares more than {MAX_NODES} nodes")

    nodes: dict[str, Node] = {}
    tests: dict[str, Test] = {}

    for uid, raw in raw_nodes.items():
        if not isinstance(raw, dict):
            continue
        rtype = raw.get("resource_type")
        if rtype in ("model", "seed", "snapshot"):
            nodes[uid] = _build_node(uid, raw, ResourceType(rtype))
        elif rtype in ("test", "unit_test"):
            tests[uid] = Test(
                unique_id=uid,
                name=str(raw.get("name") or ""),
                depends_on=_depends_on(raw.get("depends_on")),
                column_name=(str(raw.get("column_name")) if raw.get("column_name") else None),
                attached_node=(str(raw.get("attached_node")) if raw.get("attached_node") else None),
                test_type=(
                    str(_as_dict(raw.get("test_metadata")).get("name"))
                    if _as_dict(raw.get("test_metadata")).get("name")
                    else None
                ),
            )

    for uid, raw in raw_sources.items():
        if isinstance(raw, dict):
            nodes[uid] = _build_node(uid, raw, ResourceType.SOURCE)

    exposures: dict[str, Exposure] = {}
    for uid, raw in raw_exposures.items():
        if not isinstance(raw, dict):
            continue
        owner = raw.get("owner")
        exposures[uid] = Exposure(
            unique_id=uid,
            name=str(raw.get("name") or ""),
            depends_on=_depends_on(raw.get("depends_on")),
            exposure_type=str(raw.get("type")) if raw.get("type") else None,
            url=str(raw.get("url")) if raw.get("url") else None,
            owner=(
                str(owner.get("email") or owner.get("name") or "")
                if isinstance(owner, dict)
                else None
            ),
        )

    macros: dict[str, Macro] = {}
    for uid, raw in raw_macros.items():
        if not isinstance(raw, dict):
            continue
        sql = raw.get("macro_sql")
        if not isinstance(sql, str) or not sql:
            continue
        macros[uid] = Macro(
            unique_id=uid,
            name=str(raw.get("name") or ""),
            package_name=str(raw.get("package_name") or ""),
            macro_sql=sql,
        )

    return Manifest(
        schema_version=version,
        dbt_version=str(metadata.get("dbt_version") or ""),
        adapter_type=str(metadata.get("adapter_type") or ""),
        project_name=str(metadata.get("project_name") or ""),
        nodes=nodes,
        tests=tests,
        exposures=exposures,
        macros=macros,
    )
