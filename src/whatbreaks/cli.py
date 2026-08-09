"""Command line interface.

Only `debug` exists so far. The headline `check` command arrives in Phase 2,
and shipping a stub of it that always says "no breaking changes" would be worse
than shipping nothing.

`debug` is not a throwaway: you cannot reason about a lineage bug you cannot
see, and every wrong answer so far was diagnosed by dumping intermediate state.
It earns its place before the feature it supports.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from whatbreaks import OUTPUT_SCHEMA_VERSION, __version__
from whatbreaks.analysis import Analysis, CoverageReport
from whatbreaks.diff import Finding, Findings, Severity, classify, diff_analyses
from whatbreaks.errors import InputError
from whatbreaks.lineage.column_graph import ColumnRef
from whatbreaks.report import render_markdown

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_INPUT_ERROR = 2


def _fail(exc: InputError) -> None:
    """Report an input problem as a problem with the INPUT, not the analysis.

    "analysis failed" sends users looking for a bug in their SQL. dbt's own
    error plus a remedy sends them to the actual cause.
    """
    click.secho(f"error: {exc}", fg="red", err=True)
    if exc.remedy:
        click.secho(f"  {exc.remedy}", err=True)
    sys.exit(EXIT_INPUT_ERROR)


def _load(manifest: Path, project_root: Path | None) -> Analysis:
    try:
        return Analysis.run(manifest, project_root)
    except InputError as exc:
        _fail(exc)
        raise  # unreachable; keeps type checkers happy


def _echo_coverage(report: CoverageReport) -> None:
    click.echo(report.headline())
    if report.reasons:
        for reason, count in report.reasons.items():
            click.echo(f"  {count:>5}  {reason}")
    if report.catalog_would_help:
        click.echo(
            f"  {report.catalog_would_help} of these would resolve with catalog.json "
            f"(run `dbt docs generate`)"
        )


@click.group()
@click.version_option(__version__, prog_name="whatbreaks")
def main() -> None:
    """Static breaking-change analysis for dbt.

    No warehouse, no secrets, no backend.
    """


_manifest_arg = click.argument(
    "manifest", type=click.Path(exists=False, dir_okay=False, path_type=Path)
)
_root_opt = click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="dbt project root. Inferred from <root>/target/manifest.json when omitted. "
    "Unlocks seed CSV headers and project vars.",
)
_json_opt = click.option("--json", "as_json", is_flag=True, help="Emit JSON.")


_SEVERITY_STYLE = {
    Severity.BREAKING: ("BREAKING", "red"),
    Severity.POSSIBLY_BREAKING: ("MAYBE", "yellow"),
    Severity.SAFE: ("safe", "green"),
    Severity.INFO: ("info", None),
}

# What `--fail-on` means, from strictest to most permissive.
_FAIL_THRESHOLDS = {
    "breaking": Severity.BREAKING,
    "possibly-breaking": Severity.POSSIBLY_BREAKING,
    "never": None,
}


@main.command("check")
@click.option(
    "--base",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="manifest.json from the base commit (what main looks like today).",
)
@click.option(
    "--head",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="manifest.json from the change under review.",
)
@click.option("--base-root", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--head-root", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option(
    "--fail-on",
    type=click.Choice(list(_FAIL_THRESHOLDS)),
    default="breaking",
    show_default=True,
    help="Minimum severity that fails the run. Uncertainty is always REPORTED; "
    "failing CI on it is opt-in, because a linter that cries wolf gets removed.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "markdown"]),
    default="text",
    show_default=True,
    help="text for humans, json for tooling, markdown for a PR comment.",
)
def check(
    base: Path,
    head: Path,
    base_root: Path | None,
    head_root: Path | None,
    fail_on: str,
    output_format: str,
) -> None:
    """Report what a change breaks downstream.

    Compares the column contracts of two manifests. This is a graph diff, not a
    text diff: reformatting produces nothing, and a model whose columns changed
    because an upstream SELECT * changed is caught even though its own file was
    never touched.
    """
    base_analysis = _load(base, base_root)
    head_analysis = _load(head, head_root)

    diff = diff_analyses(base_analysis, head_analysis)
    findings = classify(diff, head_analysis, base_analysis)
    coverage = findings.coverage

    threshold = _FAIL_THRESHOLDS[fail_on]
    failing = (
        [f for f in findings.items if f.severity.rank >= threshold.rank]
        if threshold is not None
        else []
    )

    if output_format == "json":
        click.echo(
            json.dumps(_findings_payload(findings, fail_on, failing), indent=2, sort_keys=True)
        )
    elif output_format == "markdown":
        click.echo(render_markdown(findings), nl=False)
    else:
        _render_findings(findings, coverage)

    sys.exit(EXIT_FINDINGS if failing else EXIT_OK)


def _findings_payload(findings: Findings, fail_on: str, failing: list[Finding]) -> dict[str, Any]:
    coverage = findings.coverage
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "fail_on": fail_on,
        "failed": bool(failing),
        "findings": [
            {
                "rule": f.rule,
                "title": f.title,
                "severity": f.severity.value,
                "confidence": f.confidence.label,
                "model": f.node_name,
                "column": f.column,
                "summary": f.summary,
                "detail": f.detail,
                "impact": {
                    "columns": [str(c) for c in f.impact.columns],
                    "models": list(f.impact.models),
                    "tests": list(f.impact.tests),
                    "exposures": list(f.impact.exposures),
                    "query_breaks": list(f.impact.query_breaks),
                },
            }
            for f in findings.items
        ],
        "coverage": (
            {
                "total_models": coverage.total_models,
                "exact": coverage.exact,
                "partial": coverage.partial,
                "unknown": coverage.unknown,
                "analysed_pct": coverage.analysed_pct,
                "complete": coverage.is_complete,
                "reasons": coverage.reasons,
            }
            if coverage
            else None
        ),
    }


def _render_findings(findings: Findings, coverage: CoverageReport | None) -> None:
    for finding in findings.items:
        label, colour = _SEVERITY_STYLE[finding.severity]
        prefix = click.style(f"{label:>8}", fg=colour, bold=colour == "red")
        click.echo(f"{prefix}  {finding.rule}  {finding.summary}")
        if finding.detail:
            click.echo(f"          {finding.detail}")
        click.echo(f"          confidence: {finding.confidence.label}")

    if not findings.items:
        click.secho("no changes to model column contracts", fg="green")

    # Coverage is never omitted. "No breaking changes found" without saying how
    # much was analysed is a lie by omission.
    if coverage is not None:
        click.echo()
        _echo_coverage(coverage)
        if not coverage.is_complete:
            # Wording matters here. "absence of findings is not proof of safety"
            # reads as nonsense when findings are on screen; what incomplete
            # coverage actually means is that there may be MORE than we found.
            note = (
                "some models could not be fully analysed, so there may be further "
                "impact we cannot see"
                if findings.items
                else "some models could not be fully analysed, so the absence of "
                "findings is not proof that nothing breaks"
            )
            click.secho(f"note: {note}", fg="yellow")


@main.group()
def debug() -> None:
    """Inspect what whatbreaks sees. Output shape is not a stable API."""


@debug.command("coverage")
@_manifest_arg
@_root_opt
@_json_opt
def debug_coverage(manifest: Path, project_root: Path | None, as_json: bool) -> None:
    """How much of the project could be analysed, and why not the rest."""
    analysis = _load(manifest, project_root)
    report = analysis.coverage()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "schema_version": OUTPUT_SCHEMA_VERSION,
                    "total_models": report.total_models,
                    "exact": report.exact,
                    "partial": report.partial,
                    "unknown": report.unknown,
                    "analysed_pct": report.analysed_pct,
                    "reasons": report.reasons,
                    "macros": report.macro_stats,
                    "unanalysable": [
                        {"model": name, "why": why} for name, why in report.unanalysable
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    _echo_coverage(report)
    stats = report.macro_stats
    click.echo(
        f"macros: compiled {stats.get('compiled', 0)}/{stats.get('plain', 0)} "
        f"({stats.get('skipped_non_macro', 0)} non-macro blocks skipped)"
    )
    if report.unanalysable:
        click.echo("\nnot analysed:")
        for name, why in report.unanalysable[:40]:
            click.echo(f"  {name}: {why}")
        if len(report.unanalysable) > 40:
            click.echo(f"  ... and {len(report.unanalysable) - 40} more")


@debug.command("schema")
@_manifest_arg
@_root_opt
@click.option("--model", "model_name", default=None, help="Limit to one model.")
@_json_opt
def debug_schema(
    manifest: Path, project_root: Path | None, model_name: str | None, as_json: bool
) -> None:
    """Show inferred output columns per model, with resolution and reason."""
    analysis = _load(manifest, project_root)
    rows: list[dict[str, Any]] = []
    for uid, node in sorted(analysis.manifest.models.items()):
        if model_name and node.name != model_name:
            continue
        schema = analysis.inference.schemas.get(uid)
        if schema is None:
            continue
        rows.append(
            {
                "model": node.name,
                "unique_id": uid,
                "resolution": schema.resolution.value,
                "reason": schema.uncertainty.reason.value or None,
                "origin": schema.origin.value,
                "columns": list(schema.columns),
                "undocumented": list(schema.undocumented),
                "documented_but_absent": list(schema.documented_but_absent),
            }
        )

    if as_json:
        click.echo(
            json.dumps(
                {"schema_version": OUTPUT_SCHEMA_VERSION, "models": rows},
                indent=2,
                sort_keys=True,
            )
        )
        return

    for row in rows:
        marker = {"exact": "=", "partial": "~", "unknown": "?"}[row["resolution"]]
        suffix = f"  ({row['reason']})" if row["reason"] else ""
        click.echo(f"{marker} {row['model']} [{row['origin']}]{suffix}")
        click.echo(f"    {', '.join(row['columns']) or '(none)'}")
        if row["undocumented"] or row["documented_but_absent"]:
            click.echo(f"    doc drift: +{row['undocumented']} -{row['documented_but_absent']}")
    if not rows:
        click.echo("no models matched")


@debug.command("graph")
@_manifest_arg
@_root_opt
@click.option("--model", "model_name", default=None, help="Limit to one model.")
@click.option("--column", "column_name", default=None, help="Limit to one column.")
@click.option(
    "--consumers",
    is_flag=True,
    help="Invert: show what depends ON the selected column instead.",
)
@_json_opt
def debug_graph(
    manifest: Path,
    project_root: Path | None,
    model_name: str | None,
    column_name: str | None,
    consumers: bool,
    as_json: bool,
) -> None:
    """Dump the column graph, or the lineage of one column."""
    analysis = _load(manifest, project_root)
    graph = analysis.graph
    by_name = {n.name: uid for uid, n in analysis.manifest.models.items()}

    selected_id = by_name.get(model_name) if model_name else None
    if model_name and selected_id is None:
        click.secho(f"no model named {model_name!r}", fg="red", err=True)
        sys.exit(EXIT_INPUT_ERROR)

    if consumers and selected_id and column_name:
        ref = ColumnRef(selected_id, column_name)
        edges = graph.downstream_of(ref)
        required = [r for r in graph.required if r.upstream == ref]
    else:
        edges = tuple(
            e
            for e in graph.edges
            if (selected_id is None or e.downstream.node_id == selected_id)
            and (column_name is None or e.downstream.column == column_name)
        )
        required = [
            r for r in graph.required if selected_id is None or r.downstream_model == selected_id
        ]

    if as_json:
        click.echo(
            json.dumps(
                {
                    "schema_version": OUTPUT_SCHEMA_VERSION,
                    "edges": [
                        {
                            "downstream": {
                                "model": e.downstream.node_id,
                                "column": e.downstream.column,
                            },
                            "upstream": {
                                "model": e.upstream.node_id,
                                "column": e.upstream.column,
                            },
                            "kind": e.kind.value,
                            "confidence": e.confidence.label,
                        }
                        for e in edges
                    ],
                    "required": [
                        {
                            "model": r.downstream_model,
                            "upstream": {
                                "model": r.upstream.node_id,
                                "column": r.upstream.column,
                            },
                            "kind": r.kind.value,
                            "confidence": r.confidence.label,
                        }
                        for r in required
                    ],
                    "unresolved": [
                        {"model": u.node_id, "column": u.column} for u in graph.unresolved
                    ],
                    "stats": graph.stats(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    for edge in edges:
        click.echo(
            f"{edge.downstream}  <-  {edge.upstream}  [{edge.kind.value}, {edge.confidence.label}]"
        )
    if required:
        click.echo("\nrequired but not projected (breaks the query, not the schema):")
        for item in required:
            click.echo(f"  {item.downstream_model}  needs  {item.upstream}")
    if graph.unresolved and not model_name:
        click.echo(f"\n{len(graph.unresolved)} columns with unresolved lineage")
    if not edges and not required:
        click.echo("no edges matched")

    # Coverage always accompanies results, even in debug output.
    click.echo()
    _echo_coverage(analysis.coverage())


@debug.command("sql")
@_manifest_arg
@_root_opt
@click.option("--model", "model_name", required=True, help="Model to show.")
def debug_sql(manifest: Path, project_root: Path | None, model_name: str) -> None:
    """Show the SQL we recovered for a model, and where it came from."""
    analysis = _load(manifest, project_root)
    for node in analysis.manifest.models.values():
        if node.name != model_name:
            continue
        recovered = analysis.recovery.recover(node)
        if recovered.ok:
            click.echo(f"-- source: {recovered.source.value if recovered.source else '?'}")
            click.echo(f"-- high fidelity: {recovered.is_high_fidelity}")
            click.echo(recovered.sql)
        else:
            failure = recovered.failure
            click.secho(f"could not recover SQL for {model_name}", fg="yellow")
            if failure:
                click.echo(f"  {failure.kind.value}: {failure.explanation}")
                click.echo(f"  fixable with better inputs: {failure.kind.is_fixable}")
        return
    click.secho(f"no model named {model_name!r}", fg="red", err=True)
    sys.exit(EXIT_INPUT_ERROR)


if __name__ == "__main__":  # pragma: no cover
    main()
