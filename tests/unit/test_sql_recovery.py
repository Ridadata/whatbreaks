from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import manifest_payload, model_node
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import FailureKind, SqlRecovery, SqlSource
from whatbreaks.sql.dialect import model_relation_key, source_relation_key


def build(write_manifest, nodes: dict, macros: dict | None = None, **kw) -> SqlRecovery:
    path = write_manifest(manifest_payload(nodes=nodes, macros=macros or {}, **kw))
    return SqlRecovery(load_manifest(path))


def macro(name: str, package: str, body: str) -> dict:
    return {"name": name, "package_name": package, "macro_sql": body}


# ---------------------------------------------------------------- sources
def test_manifest_compiled_code_wins(write_manifest) -> None:
    payload = manifest_payload(nodes={"model.testproj.a": model_node("a")})
    payload["nodes"]["model.testproj.a"]["compiled_code"] = "select 1 as precompiled"
    rec = SqlRecovery(load_manifest(write_manifest(payload)))
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.source is SqlSource.MANIFEST_COMPILED
    assert out.is_high_fidelity is True
    assert "precompiled" in (out.sql or "")


def test_compiled_sql_on_disk_is_used(write_manifest, tmp_path: Path) -> None:
    path = write_manifest(manifest_payload(nodes={"model.testproj.a": model_node("a")}))
    root = path.parent
    target = root / "target" / "compiled" / "testproj" / "models"
    target.mkdir(parents=True)
    (target / "a.sql").write_text("select 1 as from_disk", encoding="utf-8")

    rec = SqlRecovery(load_manifest(path), project_root=root)
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.source is SqlSource.DISK_COMPILED
    assert "from_disk" in (out.sql or "")


def test_rendered_sql_is_not_high_fidelity(write_manifest) -> None:
    rec = build(write_manifest, {"model.testproj.a": model_node("a")})
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.source is SqlSource.RENDERED
    assert out.is_high_fidelity is False


# ---------------------------------------------------------------- ref/source
def test_ref_and_source_emit_the_shared_relation_key(write_manifest) -> None:
    """The stub and the schema map must agree, or inference finds nothing."""
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a",
                raw_code=(
                    "select * from {{ ref('up') }} join {{ source('jaffle','orders') }} on 1=1"
                ),
            )
        },
    )
    sql = rec.recover(rec.manifest.models["model.testproj.a"]).sql or ""
    assert model_relation_key("up") in sql
    assert source_relation_key("jaffle", "orders") in sql


@pytest.mark.parametrize(
    "expr",
    [
        "{{ ref('m') }}",
        "{{ ref('pkg', 'm') }}",
        "{{ ref('m', version=2) }}",
    ],
)
def test_ref_forms_all_resolve_to_the_model_name(write_manifest, expr: str) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code=f"select * from {expr}")},
    )
    assert model_relation_key("m") in (
        rec.recover(rec.manifest.models["model.testproj.a"]).sql or ""
    )


# ------------------------------------------------- the core safety property
def test_unresolvable_macro_never_renders_to_empty_string(write_manifest) -> None:
    """The single most important behaviour in this module.

    Rendering an unknown macro to "" yields SQL that parses cleanly and means
    something else entirely. Failing loudly is mandatory.
    """
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a", raw_code="select {{ dbt_utils.star(ref('up')) }} from {{ ref('up') }}"
            )
        },
    )
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.sql is None
    assert out.failure is not None
    assert out.failure.kind is FailureKind.UNDEFINED_MACRO
    assert "dbt_utils" in out.failure.detail


def test_introspective_macros_are_distinguished_from_unknown_ones(write_manifest) -> None:
    """`run_query` is unfixable offline; an unknown macro might be fixable."""
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a", raw_code="{% set r = run_query('select 1') %}select 1 as x"
            )
        },
    )
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.failure is not None
    assert out.failure.kind is FailureKind.INTROSPECTIVE
    assert out.failure.kind.is_fixable is False
    assert "warehouse" in out.failure.explanation


def test_adapter_attribute_access_is_introspective(write_manifest) -> None:
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a", raw_code="{% set r = adapter.get_relation(1,2,3) %}select 1 as x"
            )
        },
    )
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.failure is not None
    assert out.failure.kind is FailureKind.INTROSPECTIVE


def test_var_without_default_fails_rather_than_injecting_empty(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select * from {{ var('tbl') }}")},
    )
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.sql is None
    assert out.failure is not None


def test_var_with_default_renders(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ var('n', 5) }} as n")},
    )
    assert "5" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_template_rendering_to_whitespace_is_a_failure(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="{{ config(materialized='view') }}")},
    )
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.sql is None
    assert out.failure is not None
    assert out.failure.kind is FailureKind.NO_SQL


def test_jinja_syntax_error_is_classified(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {% if %} 1")},
    )
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.failure is not None
    assert out.failure.kind is FailureKind.JINJA_SYNTAX


def test_unbounded_range_is_refused(write_manifest) -> None:
    """A generated or hostile template must not hang a CI job."""
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a", raw_code="select {% for i in range(99999999) %}1{% endfor %}"
            )
        },
    )
    out = rec.recover(rec.manifest.models["model.testproj.a"])
    assert out.sql is None
    assert out.failure is not None


def test_is_incremental_takes_the_false_branch(write_manifest) -> None:
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node(
                "a",
                raw_code=(
                    "select 1 as x {% if is_incremental() %} where updated_at > '2020' {% endif %}"
                ),
            )
        },
    )
    sql = rec.recover(rec.manifest.models["model.testproj.a"]).sql or ""
    assert "where" not in sql.lower()


# ---------------------------------------------------------------- macros
def test_manifest_macros_are_compiled_and_callable(write_manifest) -> None:
    """ADR 000 F6: this is what took renderability from 34% to 80%."""
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ my_pkg.answer() }} as v")},
        macros={
            "macro.my_pkg.answer": macro("answer", "my_pkg", "{% macro answer() %}42{% endmacro %}")
        },
    )
    assert "42" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_macros_are_callable_by_bare_name_too(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ answer() }} as v")},
        macros={
            "macro.my_pkg.answer": macro("answer", "my_pkg", "{% macro answer() %}42{% endmacro %}")
        },
    )
    assert "42" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_macros_can_call_across_packages(write_manifest) -> None:
    """The two-pass compile exists for exactly this case.

    With a single pass, `helpers` is undefined *inside* caller's body even
    though a model could see it -- the bug that made the Phase 0 registry
    look useless.
    """
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ pkg_a.caller() }} as v")},
        macros={
            "macro.pkg_a.caller": macro(
                "caller", "pkg_a", "{% macro caller() %}{{ helpers.inner() }}{% endmacro %}"
            ),
            "macro.helpers.inner": macro("inner", "helpers", "{% macro inner() %}99{% endmacro %}"),
        },
    )
    assert "99" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_materialization_blocks_do_not_poison_the_package(write_manifest) -> None:
    """One unparsable block must not cost every macro in its package."""
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ good() }} as v")},
        macros={
            "macro.p.good": macro("good", "p", "{% macro good() %}7{% endmacro %}"),
            "macro.p.mat": macro(
                "mat", "p", "{% materialization mat, default %}x{% endmaterialization %}"
            ),
        },
    )
    assert "7" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_a_broken_macro_does_not_lose_its_siblings(write_manifest) -> None:
    """Bulk compile fails -> per-macro fallback keeps the usable ones."""
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ fine() }} as v")},
        macros={
            "macro.p.fine": macro("fine", "p", "{% macro fine() %}8{% endmacro %}"),
            "macro.p.broken": macro("broken", "p", "{% macro broken() %}{% endif %}"),
        },
    )
    assert "8" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_adapter_dispatch_resolves_default_implementation(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ pick() }} as v")},
        macros={
            "macro.p.pick": macro(
                "pick", "p", "{% macro pick() %}{{ adapter.dispatch('impl')() }}{% endmacro %}"
            ),
            "macro.p.default__impl": macro(
                "default__impl", "p", "{% macro default__impl() %}D{% endmacro %}"
            ),
        },
    )
    assert "D" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_adapter_dispatch_prefers_the_adapter_specific_implementation(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a", raw_code="select {{ pick() }} as v")},
        macros={
            "macro.p.pick": macro(
                "pick", "p", "{% macro pick() %}{{ adapter.dispatch('impl')() }}{% endmacro %}"
            ),
            "macro.p.default__impl": macro(
                "default__impl", "p", "{% macro default__impl() %}D{% endmacro %}"
            ),
            "macro.p.duckdb__impl": macro(
                "duckdb__impl", "p", "{% macro duckdb__impl() %}DUCK{% endmacro %}"
            ),
        },
        adapter_type="duckdb",
    )
    assert "DUCK" in (rec.recover(rec.manifest.models["model.testproj.a"]).sql or "")


def test_macro_stats_are_reported_for_coverage(write_manifest) -> None:
    rec = build(
        write_manifest,
        {"model.testproj.a": model_node("a")},
        macros={
            "macro.p.good": macro("good", "p", "{% macro good() %}1{% endmacro %}"),
            "macro.p.mat": macro(
                "mat", "p", "{% materialization mat, default %}x{% endmaterialization %}"
            ),
        },
    )
    stats = rec.macro_stats
    assert stats["total"] == 2
    assert stats["plain"] == 1
    assert stats["skipped_non_macro"] == 1
    assert stats["compiled"] == 1


def test_recover_all_covers_every_model(write_manifest) -> None:
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node("a"),
            "model.testproj.b": model_node("b", raw_code="select {{ nope.x() }}"),
        },
    )
    out = rec.recover_all()
    assert set(out) == {"model.testproj.a", "model.testproj.b"}
    assert out["model.testproj.a"].ok is True
    assert out["model.testproj.b"].ok is False


def test_node_context_is_not_leaked_between_models(write_manifest) -> None:
    """`this` must reflect each model, not the registry's placeholder node."""
    rec = build(
        write_manifest,
        {
            "model.testproj.a": model_node("a", raw_code="select '{{ this }}' as t"),
            "model.testproj.b": model_node("b", raw_code="select '{{ this }}' as t"),
        },
    )
    out = rec.recover_all()
    assert model_relation_key("a") in (out["model.testproj.a"].sql or "")
    assert model_relation_key("b") in (out["model.testproj.b"].sql or "")
