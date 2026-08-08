from __future__ import annotations

from tests.conftest import manifest_payload, model_node
from whatbreaks.lineage import (
    ColumnRef,
    Confidence,
    EdgeKind,
    SchemaInference,
    build_column_graph,
)
from whatbreaks.manifest import load_manifest
from whatbreaks.sql import SqlRecovery


def graph_for(write_manifest, nodes: dict, sources: dict | None = None):
    path = write_manifest(manifest_payload(nodes=nodes, sources=sources or {}))
    manifest = load_manifest(path)
    root = path.parent
    recovery = SqlRecovery(manifest, project_root=root)
    inference = SchemaInference(manifest, recovery, project_root=root).infer()
    return build_column_graph(manifest, recovery, inference), manifest


def upstream(nodes: dict, name: str, columns: list[str]) -> dict:
    nodes[f"model.testproj.{name}"] = model_node(
        name, raw_code="select " + ", ".join(f"1 as {c}" for c in columns)
    )
    return nodes


UP = "model.testproj.up"
DOWN = "model.testproj.down"


def test_direct_selection_produces_a_direct_edge(write_manifest) -> None:
    nodes = upstream({}, "up", ["a", "b"])
    nodes[DOWN] = model_node(
        "down", raw_code="select a as renamed from {{ ref('up') }}", depends_on=[UP]
    )
    graph, _ = graph_for(write_manifest, nodes)

    edges = graph.upstream_of(ColumnRef(DOWN, "renamed"))
    assert len(edges) == 1
    assert edges[0].upstream == ColumnRef(UP, "a")
    assert edges[0].kind is EdgeKind.DIRECT


def test_untouched_columns_do_not_gain_edges(write_manifest) -> None:
    """Precision is the product. `b` is upstream but unused, so `renamed` must
    not depend on it -- that is the difference from model-level lineage."""
    nodes = upstream({}, "up", ["a", "b"])
    nodes[DOWN] = model_node(
        "down", raw_code="select a as renamed from {{ ref('up') }}", depends_on=[UP]
    )
    graph, _ = graph_for(write_manifest, nodes)
    assert graph.downstream_of(ColumnRef(UP, "b")) == ()
    assert graph.consumers(ColumnRef(UP, "a")) == (ColumnRef(DOWN, "renamed"),)


def test_expression_edges_capture_every_input(write_manifest) -> None:
    nodes = upstream({}, "up", ["a", "b"])
    nodes[DOWN] = model_node(
        "down", raw_code="select a + b as total from {{ ref('up') }}", depends_on=[UP]
    )
    graph, _ = graph_for(write_manifest, nodes)
    sources = {e.upstream for e in graph.upstream_of(ColumnRef(DOWN, "total"))}
    assert sources == {ColumnRef(UP, "a"), ColumnRef(UP, "b")}


def test_aggregate_edges_are_labelled(write_manifest) -> None:
    nodes = upstream({}, "up", ["amount", "grp"])
    nodes[DOWN] = model_node(
        "down",
        raw_code="select grp, sum(amount) as total from {{ ref('up') }} group by grp",
        depends_on=[UP],
    )
    graph, _ = graph_for(write_manifest, nodes)
    edges = graph.upstream_of(ColumnRef(DOWN, "total"))
    assert [e.upstream for e in edges] == [ColumnRef(UP, "amount")]
    assert edges[0].kind is EdgeKind.AGGREGATE


def test_lineage_traverses_ctes_without_emitting_them_as_nodes(write_manifest) -> None:
    """CTEs are internal to a model and are not addressable in dbt, so they
    must not appear as graph nodes -- only the real upstream model does."""
    nodes = upstream({}, "up", ["a"])
    nodes[DOWN] = model_node(
        "down",
        raw_code=(
            "with step1 as (select a from {{ ref('up') }}), "
            "step2 as (select a as b from step1) "
            "select b from step2"
        ),
        depends_on=[UP],
    )
    graph, _ = graph_for(write_manifest, nodes)
    edges = graph.upstream_of(ColumnRef(DOWN, "b"))
    assert [e.upstream for e in edges] == [ColumnRef(UP, "a")]
    assert all(e.upstream.node_id in (UP, DOWN) for e in graph.edges)


def test_join_across_two_parents_attributes_columns_correctly(write_manifest) -> None:
    nodes: dict = {}
    upstream(nodes, "left", ["id", "name"])
    upstream(nodes, "right", ["id", "amount"])
    nodes[DOWN] = model_node(
        "down",
        raw_code=(
            "select l.name as who, r.amount as much "
            "from {{ ref('left') }} l join {{ ref('right') }} r on l.id = r.id"
        ),
        depends_on=["model.testproj.left", "model.testproj.right"],
    )
    graph, _ = graph_for(write_manifest, nodes)
    assert [e.upstream for e in graph.upstream_of(ColumnRef(DOWN, "who"))] == [
        ColumnRef("model.testproj.left", "name")
    ]
    assert [e.upstream for e in graph.upstream_of(ColumnRef(DOWN, "much"))] == [
        ColumnRef("model.testproj.right", "amount")
    ]


def test_star_expansion_produces_one_edge_per_column(write_manifest) -> None:
    nodes = upstream({}, "up", ["a", "b"])
    nodes[DOWN] = model_node("down", raw_code="select * from {{ ref('up') }}", depends_on=[UP])
    graph, _ = graph_for(write_manifest, nodes)
    assert [e.upstream for e in graph.upstream_of(ColumnRef(DOWN, "a"))] == [ColumnRef(UP, "a")]
    assert [e.upstream for e in graph.upstream_of(ColumnRef(DOWN, "b"))] == [ColumnRef(UP, "b")]


def test_column_named_inside_a_cte_is_not_called_star_expanded(write_manifest) -> None:
    """Regression: the dominant dbt idiom ends in `select * from final`.

    Judging star-expansion from the top-level projection labelled EVERY column
    star-expanded, including ones computed by name inside a CTE. Since
    STAR_EXPANDED is exempt from `breaks_query`, that silently under-reported
    breakage on the most common pattern in dbt. Found by running the CLI on
    jaffle_shop, not by reading the code.
    """
    nodes = upstream({}, "up", ["order_id", "customer_id"])
    nodes[DOWN] = model_node(
        "down",
        raw_code=(
            "with final as ("
            "  select customer_id, count(order_id) as number_of_orders "
            "  from {{ ref('up') }} group by customer_id"
            ") select * from final"
        ),
        depends_on=[UP],
    )
    graph, _ = graph_for(write_manifest, nodes)

    edges = graph.upstream_of(ColumnRef(DOWN, "number_of_orders"))
    assert [e.upstream for e in edges] == [ColumnRef(UP, "order_id")]
    assert edges[0].kind is not EdgeKind.STAR_EXPANDED
    assert edges[0].kind.breaks_query, "dropping order_id errors; this must say so"


def test_a_genuinely_passed_through_column_is_star_expanded(write_manifest) -> None:
    """The converse: a column never named anywhere really is star-expanded."""
    nodes = upstream({}, "up", ["a", "b"])
    nodes[DOWN] = model_node("down", raw_code="select * from {{ ref('up') }}", depends_on=[UP])
    graph, _ = graph_for(write_manifest, nodes)
    edges = graph.upstream_of(ColumnRef(DOWN, "a"))
    assert edges[0].kind is EdgeKind.STAR_EXPANDED
    assert not edges[0].kind.breaks_query


def test_literal_columns_have_no_upstream_and_are_not_unresolved(write_manifest) -> None:
    nodes = upstream({}, "up", ["a"])
    nodes[DOWN] = model_node(
        "down", raw_code="select 1 as constant from {{ ref('up') }}", depends_on=[UP]
    )
    graph, _ = graph_for(write_manifest, nodes)
    assert graph.upstream_of(ColumnRef(DOWN, "constant")) == ()


def test_edges_from_rendered_sql_are_never_confirmed(write_manifest) -> None:
    """Rendered SQL is our reconstruction, not dbt's compiled output."""
    nodes = upstream({}, "up", ["a"])
    nodes[DOWN] = model_node("down", raw_code="select a from {{ ref('up') }}", depends_on=[UP])
    graph, _ = graph_for(write_manifest, nodes)
    assert graph.edges
    assert all(e.confidence <= Confidence.LIKELY for e in graph.edges)


def test_edges_from_dbt_compiled_sql_can_be_confirmed(write_manifest) -> None:
    nodes = upstream({}, "up", ["a"])
    nodes[DOWN] = model_node("down", raw_code="select a from x", depends_on=[UP])
    nodes[DOWN]["compiled_code"] = "select a from wb_model_up"
    nodes[UP]["compiled_code"] = "select 1 as a"
    graph, _ = graph_for(write_manifest, nodes)
    edges = graph.upstream_of(ColumnRef(DOWN, "a"))
    assert edges
    assert edges[0].confidence is Confidence.CONFIRMED


def test_graph_is_deterministic(write_manifest) -> None:
    nodes = upstream({}, "up", ["a", "b", "c"])
    nodes[DOWN] = model_node(
        "down",
        raw_code="select c, a, b from {{ ref('up') }}",
        depends_on=[UP],
    )
    first, _ = graph_for(write_manifest, nodes)
    second, _ = graph_for(write_manifest, nodes)
    assert [e.sort_key for e in first.edges] == [e.sort_key for e in second.edges]


def test_duplicate_paths_are_deduplicated(write_manifest) -> None:
    """A column both projected and used as a join key must count once."""
    nodes: dict = {}
    upstream(nodes, "left", ["id"])
    upstream(nodes, "right", ["id"])
    nodes[DOWN] = model_node(
        "down",
        raw_code=(
            "select l.id as id from {{ ref('left') }} l join {{ ref('right') }} r on l.id = r.id"
        ),
        depends_on=["model.testproj.left", "model.testproj.right"],
    )
    graph, _ = graph_for(write_manifest, nodes)
    pairs = [(e.downstream, e.upstream) for e in graph.edges]
    assert len(pairs) == len(set(pairs))


def test_stats_are_reported(write_manifest) -> None:
    nodes = upstream({}, "up", ["a"])
    nodes[DOWN] = model_node("down", raw_code="select a from {{ ref('up') }}", depends_on=[UP])
    graph, _ = graph_for(write_manifest, nodes)
    stats = graph.stats()
    assert stats["edges"] >= 1
    assert set(stats) >= {"edges", "unresolved", "confirmed", "likely", "unknown"}


def test_unrenderable_model_contributes_no_edges(write_manifest) -> None:
    nodes = upstream({}, "up", ["a"])
    nodes[DOWN] = model_node(
        "down", raw_code="select {{ nope.x() }} from {{ ref('up') }}", depends_on=[UP]
    )
    graph, _ = graph_for(write_manifest, nodes)
    assert all(e.downstream.node_id != DOWN for e in graph.edges)
