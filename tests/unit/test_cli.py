from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.conftest import manifest_payload, model_node
from whatbreaks.analysis import Analysis, infer_project_root
from whatbreaks.cli import EXIT_INPUT_ERROR, main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(write_manifest, tmp_path: Path) -> Path:
    """A small project laid out the way dbt lays one out."""
    nodes = {
        "model.testproj.up": model_node("up", raw_code="select 1 as a, 2 as b"),
        "model.testproj.down": model_node(
            "down",
            raw_code="select a as renamed from {{ ref('up') }} where b > 0",
            depends_on=["model.testproj.up"],
            columns=["renamed", "stale_doc"],
        ),
        "model.testproj.broken": model_node("broken", raw_code="select {{ nope.x() }}"),
    }
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "dbt_project.yml").write_text("name: testproj\n", encoding="utf-8")
    path = target / "manifest.json"
    path.write_text(json.dumps(manifest_payload(nodes=nodes)), encoding="utf-8")
    return path


# ------------------------------------------------------------- plumbing
def test_project_root_is_inferred_from_the_dbt_layout(project: Path) -> None:
    assert infer_project_root(project) == project.parent.parent


def test_project_root_is_not_invented_when_layout_differs(tmp_path: Path) -> None:
    assert infer_project_root(tmp_path / "manifest.json") is None


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "whatbreaks" in result.output


def test_missing_manifest_is_an_input_error_with_a_remedy(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Exit 2, not 1: nothing was analysed, so this is not a finding."""
    result = runner.invoke(main, ["debug", "coverage", str(tmp_path / "nope.json")])
    assert result.exit_code == EXIT_INPUT_ERROR
    assert "error:" in result.output
    assert "dbt parse" in result.output  # the remedy, not just the complaint


def test_unsupported_manifest_version_is_reported_clearly(
    runner: CliRunner, write_manifest
) -> None:
    path = write_manifest(manifest_payload(schema_version=7))
    result = runner.invoke(main, ["debug", "coverage", str(path)])
    assert result.exit_code == EXIT_INPUT_ERROR
    assert "v7" in result.output


# ------------------------------------------------------------- coverage
def test_coverage_reports_what_was_not_analysed(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "coverage", str(project)])
    assert result.exit_code == 0
    assert "analysed" in result.output
    assert "broken" in result.output  # the unanalysable model is named
    assert "unparseable_jinja" in result.output


def test_coverage_json_is_machine_readable(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "coverage", str(project), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["total_models"] == 3
    assert payload["unknown"] == 1
    assert any(u["model"] == "broken" for u in payload["unanalysable"])


def test_coverage_counts_are_internally_consistent(project: Path) -> None:
    report = Analysis.run(project).coverage()
    assert report.exact + report.partial + report.unknown == report.total_models
    assert report.analysed == report.exact + report.partial
    assert not report.is_complete  # one model failed, so this must not claim complete


# --------------------------------------------------------------- schema
def test_schema_shows_columns_and_doc_drift(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "schema", str(project), "--model", "down"])
    assert result.exit_code == 0
    assert "renamed" in result.output
    assert "doc drift" in result.output  # `stale_doc` is documented but absent


def test_schema_json_shape(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "schema", str(project), "--json"])
    payload = json.loads(result.output)
    names = {m["model"] for m in payload["models"]}
    assert names == {"up", "down", "broken"}
    down = next(m for m in payload["models"] if m["model"] == "down")
    assert down["columns"] == ["renamed"]
    assert down["documented_but_absent"] == ["stale_doc"]


def test_schema_unknown_model_reports_no_match(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "schema", str(project), "--model", "ghost"])
    assert result.exit_code == 0
    assert "no models matched" in result.output


# ---------------------------------------------------------------- graph
def test_graph_lists_edges(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "graph", str(project), "--model", "down"])
    assert result.exit_code == 0
    assert "model.testproj.up.a" in result.output


def test_graph_shows_predicate_only_dependencies_separately(
    runner: CliRunner, project: Path
) -> None:
    """`b` is only used in a WHERE clause; it must still be visible."""
    result = runner.invoke(main, ["debug", "graph", str(project), "--model", "down"])
    assert "required but not projected" in result.output
    assert "model.testproj.up.b" in result.output


def test_graph_always_prints_coverage(runner: CliRunner, project: Path) -> None:
    """Results without coverage are a lie by omission."""
    result = runner.invoke(main, ["debug", "graph", str(project)])
    assert "analysed" in result.output


def test_graph_consumers_inverts_the_question(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(
        main,
        [
            "debug",
            "graph",
            str(project),
            "--model",
            "up",
            "--column",
            "a",
            "--consumers",
        ],
    )
    assert result.exit_code == 0
    assert "model.testproj.down.renamed" in result.output


def test_graph_json_shape(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "graph", str(project), "--json"])
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["stats"]["edges"] >= 1
    assert all(e["confidence"] in ("confirmed", "likely", "unknown") for e in payload["edges"])
    assert any(r["upstream"]["column"] == "b" for r in payload["required"])


def test_graph_unknown_model_exits_two(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "graph", str(project), "--model", "ghost"])
    assert result.exit_code == EXIT_INPUT_ERROR


# ------------------------------------------------------------------ sql
def test_sql_shows_recovered_sql_and_its_provenance(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "sql", str(project), "--model", "down"])
    assert result.exit_code == 0
    assert "source: rendered" in result.output
    assert "high fidelity: False" in result.output  # rendered != dbt-compiled
    assert "wb_model_up" in result.output


def test_sql_explains_a_recovery_failure(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "sql", str(project), "--model", "broken"])
    assert result.exit_code == 0
    assert "could not recover" in result.output
    assert "undefined_macro" in result.output
    assert "fixable with better inputs: True" in result.output


def test_sql_unknown_model_exits_two(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(main, ["debug", "sql", str(project), "--model", "ghost"])
    assert result.exit_code == EXIT_INPUT_ERROR


# ----------------------------------------------------------- determinism
def test_json_output_is_byte_identical_across_runs(runner: CliRunner, project: Path) -> None:
    """A stated NFR, and a precondition for caching and diffing later."""
    first = runner.invoke(main, ["debug", "graph", str(project), "--json"]).output
    second = runner.invoke(main, ["debug", "graph", str(project), "--json"]).output
    assert first == second
