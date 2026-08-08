"""A mechanical oracle for the lineage claims themselves.

Every lineage tool in this space asserts correctness against its own
expectations - a closed loop. This breaks the loop with a property that can be
checked by something other than the code under test:

    If we claim column D depends on upstream column U, then removing U from the
    upstream schema MUST make the downstream query fail to qualify.

sqlglot's `validate_qualify_columns=True` is the independent judge. It knows
nothing about our graph; it just refuses to resolve a column that is not there.

The converse matters just as much and is the real false-positive guard:

    If we DO NOT claim a dependency on U, removing U must NOT break the query.

That is what stops the tool inventing blast radius.

`STAR_EXPANDED` edges are deliberately exempt. `select * from up` does not
*fail* when a column disappears - it silently produces a narrower result. That
is a real difference in breakage semantics, not a gap in the test, and the
report says so.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot.optimizer.qualify import qualify

from tests.conftest import manifest_payload, model_node
from whatbreaks.lineage import ColumnRef, EdgeKind, SchemaInference, build_column_graph
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery
from whatbreaks.sql.dialect import model_relation_key

DIALECT = "duckdb"
UP = "model.testproj.up"
DOWN = "model.testproj.down"
UP_REL = model_relation_key("up")

# (label, upstream columns, downstream SQL over `up`)
CASES: list[tuple[str, list[str], str]] = [
    ("direct", ["a", "b"], "select a from {rel}"),
    ("aliased", ["a", "b"], "select a as renamed from {rel}"),
    ("expression", ["a", "b"], "select a + b as total from {rel}"),
    ("function", ["a", "b"], "select coalesce(a, b) as c from {rel}"),
    ("aggregate", ["amt", "grp"], "select grp, sum(amt) as total from {rel} group by grp"),
    ("cte_chain", ["a", "b"], "with s as (select a from {rel}) select a from s"),
    (
        "nested_cte",
        ["a", "b"],
        "with s as (select a from {rel}), t as (select a as z from s) select z from t",
    ),
    ("case_when", ["a", "b"], "select case when a > 1 then b else null end as c from {rel}"),
    ("where_only", ["a", "b"], "select b from {rel} where a > 1"),
    ("subquery", ["a", "b"], "select x from (select a as x from {rel}) q"),
    ("distinct", ["a", "b"], "select distinct a from {rel}"),
    (
        "window",
        ["a", "b"],
        "select row_number() over (partition by a order by b) as rn from {rel}",
    ),
]


def _qualifies_without(sql: str, columns: list[str], removed: str | None) -> bool:
    """Does the query still resolve when `removed` is gone from upstream?"""
    schema = {UP_REL: {c: "INT" for c in columns if c != removed}}
    try:
        qualify(
            sqlglot.parse_one(sql, dialect=DIALECT),
            schema=schema,
            dialect=DIALECT,
            validate_qualify_columns=True,
        )
    except Exception:
        return False
    return True


def _graph_for(write_manifest, columns: list[str], sql: str):
    nodes = {
        UP: model_node("up", raw_code="select " + ", ".join(f"1 as {c}" for c in columns)),
        DOWN: model_node("down", raw_code=sql.format(rel="{{ ref('up') }}"), depends_on=[UP]),
    }
    path = write_manifest(manifest_payload(nodes=nodes))
    manifest = load_manifest(path)
    recovery = SqlRecovery(manifest, project_root=path.parent)
    inference = SchemaInference(manifest, recovery, project_root=path.parent).infer()
    return build_column_graph(manifest, recovery, inference)


@pytest.mark.parametrize(("label", "columns", "sql"), CASES, ids=[c[0] for c in CASES])
def test_claimed_dependencies_really_break_when_removed(
    write_manifest, label: str, columns: list[str], sql: str
) -> None:
    """Soundness: every dependency we claim is a real one."""
    graph = _graph_for(write_manifest, columns, sql)
    rendered = sql.format(rel=UP_REL)

    claimed = {
        e.upstream.column
        for e in graph.edges
        if e.upstream.node_id == UP and e.kind is not EdgeKind.STAR_EXPANDED
    }
    assert claimed, f"{label}: expected at least one upstream dependency"

    for column in claimed:
        assert not _qualifies_without(rendered, columns, column), (
            f"{label}: we claim `{column}` is needed, but the query still "
            f"qualifies without it - the claim is unsound"
        )


@pytest.mark.parametrize(("label", "columns", "sql"), CASES, ids=[c[0] for c in CASES])
def test_unclaimed_columns_are_genuinely_unused(
    write_manifest, label: str, columns: list[str], sql: str
) -> None:
    """Completeness, and the false-positive guard.

    A column we did NOT report must be one whose removal does not break the
    query. Failing this means we would be under-reporting blast radius.
    """
    graph = _graph_for(write_manifest, columns, sql)
    rendered = sql.format(rel=UP_REL)

    # "Claimed" must include columns needed only for filters, joins and
    # grouping. Projection lineage alone misses them, and this assertion is
    # what caught that.
    claimed = {e.upstream.column for e in graph.edges if e.upstream.node_id == UP}
    claimed |= {r.upstream.column for r in graph.required if r.upstream.node_id == UP}
    for column in columns:
        if column in claimed:
            continue
        assert _qualifies_without(rendered, columns, column), (
            f"{label}: removing `{column}` breaks the query, but we never "
            f"reported it as a dependency - blast radius is under-reported"
        )


def test_the_oracle_itself_detects_a_real_break() -> None:
    """Guard against a vacuous oracle.

    If `validate_qualify_columns` silently accepted anything, both properties
    above would pass trivially. This asserts the judge actually judges.
    """
    sql = f"select a from {UP_REL}"
    assert _qualifies_without(sql, ["a", "b"], None) is True
    assert _qualifies_without(sql, ["a", "b"], "b") is True
    assert _qualifies_without(sql, ["a", "b"], "a") is False


def test_star_expansion_is_reported_but_exempt_from_the_invariant(write_manifest) -> None:
    """`select *` does not error when a column vanishes - it quietly narrows.

    Still worth reporting, but it is a different kind of breakage, and the
    distinction is deliberate rather than an untested gap.
    """
    graph = _graph_for(write_manifest, ["a", "b"], "select * from {rel}")
    star_edges = [e for e in graph.edges if e.kind is EdgeKind.STAR_EXPANDED]
    assert star_edges, "star expansion should still produce edges"
    # the query keeps qualifying without either column
    assert _qualifies_without(f"select * from {UP_REL}", ["a", "b"], "a") is True


def test_predicate_only_columns_are_reported_as_required(write_manifest) -> None:
    """Regression guard for the gap the oracle found.

    `select v from up where id = 1` never projects `id`, so projection lineage
    ignores it - yet dropping `id` breaks the model outright.
    """
    graph = _graph_for(write_manifest, ["id", "v"], "select v from {rel} where id = 1")
    projected = {e.upstream.column for e in graph.upstream_of(ColumnRef(DOWN, "v"))}
    assert projected == {"v"}

    required = {r.upstream.column for r in graph.required if r.downstream_model == DOWN}
    assert "id" in required
    assert _qualifies_without(f"select v from {UP_REL} where id = 1", ["id", "v"], "id") is False

    # and the model shows up as a dependent of the predicate column
    assert DOWN in graph.dependents_of(ColumnRef(UP, "id"))
