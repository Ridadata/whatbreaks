"""Minimal graph primitives.

Deliberately hand-rolled rather than pulling in networkx. The only operations
this project needs are a topological sort and reverse reachability, both of
which are short; adding a graph dependency to a CI tool to avoid writing them
is a bad trade against the <=4-dependency budget.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import TypeVar

T = TypeVar("T", bound=Hashable)


class CycleError(Exception):
    """The graph contains a cycle. dbt forbids these, so this means bad input."""

    def __init__(self, cycle: Sequence[object]) -> None:
        self.cycle = list(cycle)
        rendered = " -> ".join(str(c) for c in self.cycle)
        super().__init__(f"dependency cycle: {rendered}")


def topological_sort(edges: Mapping[T, Iterable[T]]) -> list[T]:
    """Return nodes ordered so every node follows all of its dependencies.

    `edges[n]` lists what `n` depends on. Nodes referenced only as dependencies
    are included. Iteration order of the input is preserved for ties, so the
    output is deterministic -- required by the byte-identical-output NFR.

    Raises `CycleError` naming the actual cycle, not just its existence.
    """
    order: list[T] = []
    state: dict[T, int] = {}  # 0/absent = unvisited, 1 = in progress, 2 = done
    stack: list[T] = []

    def visit(node: T) -> None:
        current = state.get(node, 0)
        if current == 2:
            return
        if current == 1:
            start = stack.index(node)
            raise CycleError([*stack[start:], node])
        state[node] = 1
        stack.append(node)
        for dep in edges.get(node, ()):
            visit(dep)
        stack.pop()
        state[node] = 2
        order.append(node)

    for node in edges:
        visit(node)
    return order


def reverse_edges(edges: Mapping[T, Iterable[T]]) -> dict[T, list[T]]:
    """Invert a dependency map: `out[x]` lists nodes that depend on `x`."""
    out: dict[T, list[T]] = {node: [] for node in edges}
    for node, deps in edges.items():
        for dep in deps:
            out.setdefault(dep, []).append(node)
    return out


def descendants(edges: Mapping[T, Iterable[T]], seeds: Iterable[T]) -> set[T]:
    """Everything transitively downstream of `seeds`, excluding the seeds.

    This is the blast radius at node granularity. Column granularity is
    computed separately in `impact.blast_radius`, because a node being
    downstream does not mean the *changed column* reaches it -- that
    distinction is the entire point of the tool.
    """
    downstream = reverse_edges(edges)
    seen: set[T] = set()
    queue = list(seeds)
    while queue:
        node = queue.pop()
        for child in downstream.get(node, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    seen.difference_update(seeds)
    return seen
