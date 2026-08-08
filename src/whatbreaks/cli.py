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
from whatbreaks.errors import InputError
from whatbreaks.lineage.column_graph import ColumnRef

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


@main.group()
def debug() -> None:
    """Inspect what whatbreaks sees. Output shape is not a stable API."""


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
