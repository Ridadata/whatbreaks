from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import manifest_payload, model_node
from whatbreaks.analysis import Analysis
from whatbreaks.diff import Severity, classify, diff_analyses
from whatbreaks.lineage.uncertainty import Confidence


def write_project(tmp_path: Path, name: str, nodes: dict, **kw) -> Path:
    root = tmp_path / name
    (root / "target").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: testproj\n", encoding="utf-8")
    path = root / "target" / "manifest.json"
    path.write_text(json.dumps(manifest_payload(nodes=nodes, **kw)), encoding="utf-8")
    return path


def run(tmp_path: Path, base_nodes: dict, head_nodes: dict, **kw):
    base = Analysis.run(write_project(tmp_path, "base", base_nodes, **kw))
    head = Analysis.run(write_project(tmp_path, "head", head_nodes, **kw))
    diff = diff_analyses(base, head)
    return diff, classify(diff, head, base)


UP = "model.testproj.up"
DOWN = "model.testproj.down"


def up(cols: list[str]) -> dict:
    return {UP: model_node("up", raw_code="select " + ", ".join(f"1 as {c}" for c in cols))}


# ------------------------------------------------------ WB001 breaking
def test_removed_column_with_a_consumer_is_breaking(tmp_path: Path) -> None:
    base = up(["a", "b"])
    base[DOWN] = model_node("down", raw_code="select a as x from {{ ref('up') }}", depends_on=[UP])
    head = up(["b"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    breaking = [f for f in findings.items if f.rule == "WB001"]
    assert len(breaking) == 1
    assert breaking[0].severity is Severity.BREAKING
    assert breaking[0].column == "a"
    assert "down" in breaking[0].detail


def test_breaking_finding_names_the_downstream_columns(tmp_path: Path) -> None:
    base = up(["a", "b"])
    base[DOWN] = model_node("down", raw_code="select a as x from {{ ref('up') }}", depends_on=[UP])
    head = up(["b"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    impact = findings.breaking[0].impact
    assert [str(c) for c in impact.columns] == [f"{DOWN}.x"]
    assert impact.models == (DOWN,)


def test_predicate_only_consumer_still_makes_it_breaking(tmp_path: Path) -> None:
    """`b` never reaches an output but the model filters on it."""
    base = up(["a", "b"])
    base[DOWN] = model_node(
        "down", raw_code="select a as x from {{ ref('up') }} where b > 0", depends_on=[UP]
    )
    head = up(["a"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    removed_b = [f for f in findings.items if f.rule == "WB001" and f.column == "b"]
    assert removed_b[0].severity is Severity.BREAKING
    assert DOWN in removed_b[0].impact.query_breaks


def test_cte_qualified_reference_is_still_seen(tmp_path: Path) -> None:
    """Regression: the canonical dbt model qualifies columns by CTE name.

        with orders as (select * from {{ ref('stg_orders') }})
        select orders.status from orders

    `orders` is a CTE, not a table. Resolving only real table aliases made
    this reference invisible, and whatbreaks reported a genuine breaking
    change on jaffle_shop as SAFE - a false negative, the worst outcome this
    tool can produce. Found by running the CLI, not by a unit test.
    """
    base = up(["order_id", "status"])
    base[DOWN] = model_node(
        "down",
        raw_code=(
            "with o as (select * from {{ ref('up') }}) "
            "select o.order_id as id, o.status as st from o"
        ),
        depends_on=[UP],
    )
    head = up(["order_id"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    removed = [f for f in findings.items if f.rule == "WB001" and f.column == "status"]
    assert removed, "the removal must be reported at all"
    assert removed[0].severity is Severity.BREAKING
    assert "down" in removed[0].detail


def test_chained_ctes_still_resolve_to_the_parent(tmp_path: Path) -> None:
    base = up(["a", "b"])
    base[DOWN] = model_node(
        "down",
        raw_code=(
            "with s1 as (select * from {{ ref('up') }}), "
            "s2 as (select * from s1) "
            "select s2.b as kept from s2"
        ),
        depends_on=[UP],
    )
    head = up(["a"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    removed = [f for f in findings.items if f.rule == "WB001" and f.column == "b"]
    assert removed[0].severity is Severity.BREAKING


# -------------------------------------------------- WB001 not breaking
def test_removed_column_with_no_consumer_is_safe_when_coverage_is_complete(
    tmp_path: Path,
) -> None:
    base = up(["a", "b"])
    base[DOWN] = model_node("down", raw_code="select a as x from {{ ref('up') }}", depends_on=[UP])
    head = up(["a"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    removed_b = [f for f in findings.items if f.rule == "WB001" and f.column == "b"]
    assert removed_b[0].severity is Severity.SAFE


def test_absence_of_evidence_is_not_evidence_of_absence(tmp_path: Path) -> None:
    """With partial coverage, "no consumer found" cannot mean "safe".

    The consumer may be one of the models we could not read. Calling it safe
    would be exactly the overclaim this tool exists to avoid.
    """
    base = up(["a", "b"])
    base["model.testproj.opaque"] = model_node("opaque", raw_code="select {{ nope.x() }}")
    head = up(["a"])
    head["model.testproj.opaque"] = base["model.testproj.opaque"]

    _, findings = run(tmp_path, base, head)
    removed_b = [f for f in findings.items if f.rule == "WB001" and f.column == "b"]
    assert removed_b[0].severity is Severity.POSSIBLY_BREAKING
    assert "partially analysed" in removed_b[0].detail or "may be among" in removed_b[0].detail


def test_star_only_consumers_are_possibly_breaking_not_breaking(tmp_path: Path) -> None:
    """`select *` consumers narrow silently rather than erroring."""
    base = up(["a", "b"])
    base[DOWN] = model_node("down", raw_code="select * from {{ ref('up') }}", depends_on=[UP])
    head = up(["a"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    removed_b = [f for f in findings.items if f.rule == "WB001" and f.column == "b"]
    assert removed_b[0].severity is Severity.POSSIBLY_BREAKING
    assert "narrower" in removed_b[0].detail


# ------------------------------------------------------------- WB002
def test_removed_model_still_referenced_is_breaking(tmp_path: Path) -> None:
    base = up(["a"])
    base[DOWN] = model_node("down", raw_code="select a from {{ ref('up') }}", depends_on=[UP])
    head = {DOWN: base[DOWN]}

    _, findings = run(tmp_path, base, head)
    wb002 = [f for f in findings.items if f.rule == "WB002"]
    assert wb002[0].severity is Severity.BREAKING
    assert "down" in wb002[0].detail


def test_removed_model_with_no_references_is_safe(tmp_path: Path) -> None:
    base = up(["a"])
    base["model.testproj.orphan"] = model_node("orphan", raw_code="select 1 as z")
    head = up(["a"])

    _, findings = run(tmp_path, base, head)
    wb002 = [f for f in findings.items if f.rule == "WB002"]
    assert wb002[0].severity is Severity.SAFE


# ------------------------------------------------------------- WB003
def test_added_column_is_safe(tmp_path: Path) -> None:
    _, findings = run(tmp_path, up(["a"]), up(["a", "b"]))
    wb003 = [f for f in findings.items if f.rule == "WB003"]
    assert len(wb003) == 1
    assert wb003[0].column == "b"
    assert wb003[0].severity is Severity.SAFE


# ------------------------------------------- no-op / false positives
def test_reformatting_produces_no_findings(tmp_path: Path) -> None:
    """A graph diff must be immune to formatting noise by construction."""
    base = {UP: model_node("up", raw_code="select 1 as a, 2 as b")}
    head = {
        UP: model_node(
            "up",
            raw_code="SELECT\n    1 AS a,   -- a comment\n    2 AS b\n",
        )
    }
    diff, findings = run(tmp_path, base, head)
    assert diff.is_empty
    assert findings.items == ()


def test_reordering_columns_produces_no_findings(tmp_path: Path) -> None:
    base = {UP: model_node("up", raw_code="select 1 as a, 2 as b")}
    head = {UP: model_node("up", raw_code="select 2 as b, 1 as a")}
    _, findings = run(tmp_path, base, head)
    assert findings.items == ()


def test_renaming_a_cte_produces_no_findings(tmp_path: Path) -> None:
    base = {UP: model_node("up", raw_code="with s as (select 1 as a) select * from s")}
    head = {UP: model_node("up", raw_code="with renamed as (select 1 as a) select * from renamed")}
    _, findings = run(tmp_path, base, head)
    assert findings.items == ()


def test_adding_an_unrelated_model_produces_no_breaking_findings(tmp_path: Path) -> None:
    base = up(["a"])
    head = up(["a"])
    head["model.testproj.brand_new"] = model_node("brand_new", raw_code="select 1 as z")
    _, findings = run(tmp_path, base, head)
    assert not findings.breaking


# --------------------------------------------------- honesty guarantees
def test_incomparable_models_are_reported_not_dropped(tmp_path: Path) -> None:
    """A model we could not compare is a gap in the answer, not a non-event."""
    base = {"model.testproj.x": model_node("x", raw_code="select {{ nope.a() }}")}
    head = {"model.testproj.x": model_node("x", raw_code="select 1 as a")}
    diff, findings = run(tmp_path, base, head)
    assert diff.incomparable
    wb900 = [f for f in findings.items if f.rule == "WB900"]
    assert wb900 and wb900[0].severity is Severity.INFO


def test_unknown_base_schema_never_yields_a_removal_finding(tmp_path: Path) -> None:
    """We cannot claim a column vanished if we never knew it was there."""
    base = {"model.testproj.x": model_node("x", raw_code="select {{ nope.a() }}")}
    head = {"model.testproj.x": model_node("x", raw_code="select 1 as only_col")}
    _, findings = run(tmp_path, base, head)
    assert not [f for f in findings.items if f.rule == "WB001"]


def test_findings_are_sorted_most_severe_first(tmp_path: Path) -> None:
    base = up(["a", "b", "c"])
    base[DOWN] = model_node("down", raw_code="select a as x from {{ ref('up') }}", depends_on=[UP])
    head = up(["c", "d"])
    head[DOWN] = base[DOWN]

    _, findings = run(tmp_path, base, head)
    ranks = [f.severity.rank for f in findings.items]
    assert ranks == sorted(ranks, reverse=True)


def test_coverage_accompanies_findings(tmp_path: Path) -> None:
    _, findings = run(tmp_path, up(["a"]), up(["a", "b"]))
    assert findings.coverage is not None
    assert findings.coverage.total_models == 1


@pytest.mark.parametrize("severity", list(Severity))
def test_every_severity_has_a_rank(severity: Severity) -> None:
    assert isinstance(severity.rank, int)


def test_confidence_never_exceeds_the_weakest_edge(tmp_path: Path) -> None:
    """Findings are built from rendered SQL, so none can be CONFIRMED."""
    base = up(["a", "b"])
    base[DOWN] = model_node("down", raw_code="select a as x from {{ ref('up') }}", depends_on=[UP])
    head = up(["b"])
    head[DOWN] = base[DOWN]
    _, findings = run(tmp_path, base, head)
    assert all(f.confidence <= Confidence.LIKELY for f in findings.items)
