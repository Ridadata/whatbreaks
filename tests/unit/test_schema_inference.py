from __future__ import annotations

from pathlib import Path

from tests.conftest import manifest_payload, model_node
from whatbreaks.lineage import Resolution, SchemaInference, SchemaOrigin, UnknownReason
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery


def infer(write_manifest, nodes: dict, sources: dict | None = None, root: Path | None = None):
    path = write_manifest(manifest_payload(nodes=nodes, sources=sources or {}))
    manifest = load_manifest(path)
    project_root = root or path.parent
    recovery = SqlRecovery(manifest, project_root=project_root)
    return SchemaInference(manifest, recovery, project_root=project_root).infer()


def source_node(source_name: str, name: str, columns: list[str]) -> dict:
    return {
        "name": name,
        "source_name": source_name,
        "resource_type": "source",
        "package_name": "testproj",
        "original_file_path": "models/sources.yml",
        "columns": {c: {"name": c} for c in columns},
    }


def seed_node(name: str, columns: list[str] | None = None) -> dict:
    return {
        "name": name,
        "resource_type": "seed",
        "package_name": "testproj",
        "original_file_path": f"seeds/{name}.csv",
        "depends_on": {"nodes": []},
        "columns": {c: {"name": c} for c in (columns or [])},
        "config": {},
    }


# ------------------------------------------------------------- basics
def test_simple_projection_is_exact(write_manifest) -> None:
    result = infer(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select 1 as x, 2 as y")},
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.columns == ("x", "y")
    assert schema.resolution is Resolution.EXACT
    assert schema.origin is SchemaOrigin.INFERRED


def test_aliases_are_used_as_output_names(write_manifest) -> None:
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a", raw_code="select id as customer_id, upper(nm) as name from t"
            )
        },
    )
    assert result.schemas["model.testproj.a"].columns == ("customer_id", "name")


# --------------------------------------------------- THE central rule
def test_star_over_a_cte_resolves_and_stays_exact(write_manifest) -> None:
    """ADR 000 F2 - the finding the whole approach rests on.

    Nearly every dbt model ends in `select * from final`. That star is over a
    CTE with an explicit projection, so it expands even with no schema at all.
    Treating raw star presence as uncertainty scored 0% EXACT on jaffle_shop;
    asking whether a star *survived* scores 100%.
    """
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a",
                raw_code=(
                    "with source as (select * from {{ ref('raw') }}), "
                    "renamed as (select id as customer_id, first_name from source) "
                    "select * from renamed"
                ),
                depends_on=["seed.testproj.raw"],
            ),
            "seed.testproj.raw": seed_node("raw", ["id", "first_name"]),
        },
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.columns == ("customer_id", "first_name")
    assert schema.resolution is Resolution.EXACT


def test_surviving_star_over_unknown_parent_is_partial_not_exact(write_manifest) -> None:
    """A star we could not expand is the real uncertainty signal."""
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a",
                raw_code="select *, 1 as extra from {{ source('s','t') }}",
                depends_on=["source.testproj.s.t"],
            )
        },
        sources={"source.testproj.s.t": source_node("s", "t", [])},
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.resolution is Resolution.PARTIAL
    assert schema.uncertainty.reason in (
        UnknownReason.SURVIVING_STAR,
        UnknownReason.UPSTREAM_UNKNOWN,
    )
    assert schema.uncertainty.reason.is_fixable_with_catalog


def test_star_over_a_known_source_expands(write_manifest) -> None:
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a",
                raw_code="select * from {{ source('s','t') }}",
                depends_on=["source.testproj.s.t"],
            )
        },
        sources={"source.testproj.s.t": source_node("s", "t", ["id", "amount"])},
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.columns == ("id", "amount")
    assert schema.resolution is Resolution.EXACT


# ------------------------------------------------------------- seeds
def test_seed_columns_come_from_the_csv_header(write_manifest, tmp_path: Path) -> None:
    """ADR 000 F3 - a seed's header is free schema and must not be ignored."""
    path = write_manifest(
        manifest_payload(
            nodes={
                "seed.testproj.raw": seed_node("raw"),
                "model.testproj.a": model_node(
                    "a",
                    raw_code="select * from {{ ref('raw') }}",
                    depends_on=["seed.testproj.raw"],
                ),
            }
        )
    )
    seeds = path.parent / "seeds"
    seeds.mkdir()
    (seeds / "raw.csv").write_text("id,first_name,email\n1,a,b\n", encoding="utf-8")

    manifest = load_manifest(path)
    recovery = SqlRecovery(manifest, project_root=path.parent)
    result = SchemaInference(manifest, recovery, project_root=path.parent).infer()

    seed_schema = result.schemas["seed.testproj.raw"]
    assert seed_schema.columns == ("id", "first_name", "email")
    assert seed_schema.origin is SchemaOrigin.SEED_CSV
    # and it propagates
    assert result.schemas["model.testproj.a"].columns == ("id", "first_name", "email")
    assert result.schemas["model.testproj.a"].resolution is Resolution.EXACT


def test_missing_seed_file_degrades_without_crashing(write_manifest) -> None:
    result = infer(write_manifest, {"seed.testproj.raw": seed_node("raw")})
    assert result.schemas["seed.testproj.raw"].resolution is Resolution.UNKNOWN


# ------------------------------------------------- unknown propagation
def test_explicit_columns_stay_exact_despite_unknown_parent(write_manifest) -> None:
    """Unknown-ness propagates only where it actually bites.

    We know our own output names even when we cannot verify the parent has
    them. Poisoning the whole descendant chain would make the tool useless.
    """
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a",
                raw_code="select id, name from {{ source('s','t') }}",
                depends_on=["source.testproj.s.t"],
            )
        },
        sources={"source.testproj.s.t": source_node("s", "t", [])},
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.columns == ("id", "name")
    assert schema.resolution is Resolution.EXACT


def test_partial_parent_degrades_a_downstream_star(write_manifest) -> None:
    result = infer(
        write_manifest,
        {
            "source.testproj.s.t": source_node("s", "t", []),
            "model.testproj.a": model_node(
                "a",
                raw_code="select * from {{ source('s','t') }}",
                depends_on=["source.testproj.s.t"],
            ),
            "model.testproj.b": model_node(
                "b",
                raw_code="select * from {{ ref('a') }}",
                depends_on=["model.testproj.a"],
            ),
        },
        sources={"source.testproj.s.t": source_node("s", "t", [])},
    )
    assert result.schemas["model.testproj.b"].resolution is not Resolution.EXACT


def test_unrenderable_model_is_unknown_with_a_reason(write_manifest) -> None:
    result = infer(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ nope.thing() }} as x")},
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.resolution is Resolution.UNKNOWN
    assert schema.uncertainty.reason is UnknownReason.UNPARSEABLE_JINJA


def test_warehouse_dependent_model_is_marked_unfixable(write_manifest) -> None:
    result = infer(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="{% set r = run_query('x') %}select 1 as v")},
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.uncertainty.reason is UnknownReason.NEEDS_WAREHOUSE
    assert not schema.uncertainty.reason.is_fixable_with_catalog


def test_unparseable_sql_falls_back_to_declared_columns_as_partial(write_manifest) -> None:
    """Declared columns beat nothing, but are what a human said - never EXACT."""
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a",
                raw_code="select {{ nope.thing() }}",
                columns=["documented_a", "documented_b"],
            )
        },
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.columns == ("documented_a", "documented_b")
    assert schema.resolution is Resolution.PARTIAL
    assert schema.origin is SchemaOrigin.DECLARED


def test_python_models_are_named_as_such_not_reported_as_sql_errors(write_manifest) -> None:
    """Found in the real sample: a Python model reported as `sql_parse_error`
    sends the user hunting for a bug in SQL they never wrote."""
    nodes = {
        "model.testproj.py": model_node(
            "py", raw_code="def model(dbt, session):\n    return dbt.ref('a')"
        )
    }
    nodes["model.testproj.py"]["language"] = "python"
    result = infer(write_manifest, nodes)
    schema = result.schemas["model.testproj.py"]
    assert schema.uncertainty.reason is UnknownReason.PYTHON_MODEL
    assert "out of scope" in schema.uncertainty.reason.explanation


# ------------------------------------------------------------- extras
def test_doc_drift_is_surfaced(write_manifest) -> None:
    result = infer(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select 1 as actual", columns=["stale"])},
    )
    schema = result.schemas["model.testproj.a"]
    assert schema.has_doc_drift
    assert schema.undocumented == ("actual",)
    assert schema.documented_but_absent == ("stale",)


def test_union_takes_output_names_from_the_first_branch(write_manifest) -> None:
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a", raw_code="select 1 as x, 2 as y union all select 3, 4"
            )
        },
    )
    assert result.schemas["model.testproj.a"].columns == ("x", "y")


def test_coverage_and_reasons_are_reported(write_manifest) -> None:
    result = infer(
        write_manifest,
        {
            "model.testproj.a": model_node("a", raw_code="select 1 as x"),
            "model.testproj.b": model_node("b", raw_code="select {{ nope.x() }}"),
        },
    )
    coverage = result.coverage()
    assert coverage["total"] == 2
    assert coverage["exact"] == 1
    assert coverage["unknown"] == 1
    assert result.reasons()["unparseable_jinja"] == 1


def test_inference_is_deterministic(write_manifest) -> None:
    nodes = {
        "model.testproj.b": model_node(
            "b", raw_code="select * from {{ ref('a') }}", depends_on=["model.testproj.a"]
        ),
        "model.testproj.a": model_node("a", raw_code="select 1 as x, 2 as y"),
    }
    first = infer(write_manifest, nodes)
    second = infer(write_manifest, nodes)
    assert {k: v.columns for k, v in first.schemas.items()} == {
        k: v.columns for k, v in second.schemas.items()
    }
