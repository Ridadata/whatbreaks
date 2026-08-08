from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import manifest_payload, model_node
from whatbreaks.errors import (
    ManifestNotFoundError,
    ManifestParseError,
    UnsafePathError,
    UnsupportedManifestVersionError,
)
from whatbreaks.manifest import ResourceType, load_manifest
from whatbreaks.manifest.loader import check_within_root
from whatbreaks.sql.dialect import model_relation_key, source_relation_key


def test_loads_a_minimal_manifest(write_manifest) -> None:
    path = write_manifest(
        manifest_payload(nodes={"model.testproj.a": model_node("a", columns=["x"])})
    )
    m = load_manifest(path)
    assert m.schema_version == 12
    assert m.adapter_type == "duckdb"
    assert set(m.models) == {"model.testproj.a"}
    assert m.models["model.testproj.a"].declared_column_names == ("x",)


def test_missing_file_is_an_input_error_with_a_remedy(tmp_path: Path) -> None:
    with pytest.raises(ManifestNotFoundError) as excinfo:
        load_manifest(tmp_path / "nope.json")
    assert excinfo.value.remedy  # must tell the user what to actually do


def test_invalid_json_is_reported_as_input_error(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestParseError):
        load_manifest(path)


def test_non_manifest_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(ManifestParseError):
        load_manifest(path)


@pytest.mark.parametrize("version", [1, 7, 9, 13, 99])
def test_unsupported_schema_version_is_fatal(write_manifest, version: int) -> None:
    """We refuse to guess at unknown artifact formats -- see ADR 000."""
    path = write_manifest(manifest_payload(schema_version=version))
    with pytest.raises(UnsupportedManifestVersionError):
        load_manifest(path)


@pytest.mark.parametrize("version", [10, 11, 12])
def test_supported_schema_versions_load(write_manifest, version: int) -> None:
    path = write_manifest(manifest_payload(schema_version=version))
    assert load_manifest(path).schema_version == version


def test_dependency_edges_drop_references_to_absent_nodes(write_manifest) -> None:
    """A ref to a disabled/absent node must not invent a graph entry."""
    path = write_manifest(
        manifest_payload(
            nodes={
                "model.testproj.b": model_node(
                    "b", depends_on=["model.testproj.a", "model.testproj.ghost"]
                ),
                "model.testproj.a": model_node("a"),
            }
        )
    )
    edges = load_manifest(path).dependency_edges()
    assert edges["model.testproj.b"] == ("model.testproj.a",)


def test_sources_and_seeds_become_nodes_with_relation_keys(write_manifest) -> None:
    path = write_manifest(
        manifest_payload(
            nodes={
                "seed.testproj.raw": {
                    "name": "raw",
                    "resource_type": "seed",
                    "package_name": "testproj",
                    "original_file_path": "seeds/raw.csv",
                    "depends_on": {"nodes": []},
                    "columns": {},
                    "config": {},
                }
            },
            sources={
                "source.testproj.jaffle.orders": {
                    "name": "orders",
                    "source_name": "jaffle",
                    "resource_type": "source",
                    "package_name": "testproj",
                    "original_file_path": "models/sources.yml",
                    "columns": {"id": {"name": "id"}},
                }
            },
        )
    )
    m = load_manifest(path)
    seed = m.nodes["seed.testproj.raw"]
    src = m.nodes["source.testproj.jaffle.orders"]
    assert seed.resource_type is ResourceType.SEED
    assert seed.relation_key == model_relation_key("raw")
    assert src.resource_type is ResourceType.SOURCE
    assert src.relation_key == source_relation_key("jaffle", "orders")
    assert src.declared_column_names == ("id",)


def test_tests_and_exposures_are_captured_as_consumers(write_manifest) -> None:
    path = write_manifest(
        manifest_payload(
            nodes={
                "test.testproj.not_null_a_x": {
                    "name": "not_null_a_x",
                    "resource_type": "test",
                    "package_name": "testproj",
                    "column_name": "x",
                    "attached_node": "model.testproj.a",
                    "depends_on": {"nodes": ["model.testproj.a"]},
                    "test_metadata": {"name": "not_null"},
                }
            },
            exposures={
                "exposure.testproj.dash": {
                    "name": "dash",
                    "type": "dashboard",
                    "url": "https://bi.example/dash",
                    "owner": {"email": "data@example.com"},
                    "depends_on": {"nodes": ["model.testproj.a"]},
                }
            },
        )
    )
    m = load_manifest(path)
    test = m.tests["test.testproj.not_null_a_x"]
    assert test.column_name == "x"
    assert test.test_type == "not_null"
    exposure = m.exposures["exposure.testproj.dash"]
    assert exposure.owner == "data@example.com"
    assert exposure.depends_on == ("model.testproj.a",)


def test_contract_and_materialization_config_is_read(write_manifest) -> None:
    path = write_manifest(
        manifest_payload(
            nodes={
                "model.testproj.a": model_node(
                    "a",
                    materialized="incremental",
                    unique_key="id",
                    contract={"enforced": True},
                )
            }
        )
    )
    node = load_manifest(path).models["model.testproj.a"]
    assert node.materialized == "incremental"
    assert node.unique_key == "id"
    assert node.contract_enforced is True


def test_plain_macros_exclude_materializations_and_tests(write_manifest) -> None:
    """ADR 000 F6: one materialization block poisons a bulk Jinja compile."""
    path = write_manifest(
        manifest_payload(
            macros={
                "macro.p.good": {
                    "name": "good",
                    "package_name": "p",
                    "macro_sql": "{% macro good() %}1{% endmacro %}",
                },
                "macro.p.lead": {
                    "name": "lead",
                    "package_name": "p",
                    "macro_sql": "{%- macro lead() %}2{% endmacro %}",
                },
                "macro.p.mat": {
                    "name": "mat",
                    "package_name": "p",
                    "macro_sql": "{% materialization mat, default %}x{% endmaterialization %}",
                },
                "macro.p.tst": {
                    "name": "tst",
                    "package_name": "p",
                    "macro_sql": "{% test tst(model) %}select 1{% endtest %}",
                },
            }
        )
    )
    m = load_manifest(path)
    assert len(m.macros) == 4
    assert {mac.name for mac in m.plain_macros()} == {"good", "lead"}


def test_path_escape_is_refused(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    assert check_within_root("models/a.sql", tmp_path).is_relative_to(tmp_path.resolve())
    with pytest.raises(UnsafePathError):
        check_within_root("../../../etc/passwd", tmp_path)


def test_malformed_entries_are_skipped_not_fatal(write_manifest) -> None:
    """A junk node must not abort analysis of every good node around it."""
    path = write_manifest(
        manifest_payload(
            nodes={
                "model.testproj.a": model_node("a"),
                "model.testproj.junk": "not-an-object",  # type: ignore[dict-item]
            }
        )
    )
    assert set(load_manifest(path).models) == {"model.testproj.a"}
