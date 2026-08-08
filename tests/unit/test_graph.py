from __future__ import annotations

import pytest

from whatbreaks.graph import CycleError, descendants, reverse_edges, topological_sort


def test_topological_sort_orders_dependencies_first() -> None:
    edges = {"c": ["b"], "b": ["a"], "a": []}
    order = topological_sort(edges)
    assert order.index("a") < order.index("b") < order.index("c")


def test_topological_sort_includes_nodes_only_named_as_dependencies() -> None:
    order = topological_sort({"b": ["a"]})
    assert set(order) == {"a", "b"}
    assert order.index("a") < order.index("b")


def test_topological_sort_is_deterministic() -> None:
    edges = {"d": ["a", "b"], "c": ["a"], "b": [], "a": []}
    assert topological_sort(edges) == topological_sort(edges)


def test_topological_sort_handles_diamond() -> None:
    edges = {"top": ["left", "right"], "left": ["base"], "right": ["base"], "base": []}
    order = topological_sort(edges)
    assert order.index("base") < order.index("left")
    assert order.index("base") < order.index("right")
    assert order.index("left") < order.index("top")


def test_cycle_raises_and_names_the_cycle() -> None:
    with pytest.raises(CycleError) as excinfo:
        topological_sort({"a": ["b"], "b": ["c"], "c": ["a"]})
    # the error must be actionable: it names the members, not just the fact
    assert {"a", "b", "c"} <= set(excinfo.value.cycle)


def test_self_cycle_is_detected() -> None:
    with pytest.raises(CycleError):
        topological_sort({"a": ["a"]})


def test_reverse_edges_inverts() -> None:
    assert reverse_edges({"b": ["a"], "c": ["a"]}) == {"a": ["b", "c"], "b": [], "c": []}


def test_descendants_excludes_the_seeds() -> None:
    edges = {"c": ["b"], "b": ["a"], "a": []}
    assert descendants(edges, ["a"]) == {"b", "c"}
    assert descendants(edges, ["b"]) == {"c"}
    assert descendants(edges, ["c"]) == set()


def test_descendants_of_unknown_node_is_empty() -> None:
    assert descendants({"b": ["a"], "a": []}, ["nope"]) == set()


def test_descendants_does_not_hang_on_shared_subgraphs() -> None:
    edges = {"top": ["l", "r"], "l": ["base"], "r": ["base"], "base": []}
    assert descendants(edges, ["base"]) == {"l", "r", "top"}
