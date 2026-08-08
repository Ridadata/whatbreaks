# whatbreaks

**Static breaking-change analysis for dbt. Column-level blast radius in CI — no warehouse, no
secrets, no backend.**

> ## ⚠ Status: pre-alpha. Not usable yet.
>
> There is no working CLI. The feasibility study (Phase 0) is complete and the lineage core is
> under construction. Nothing here analyses your project yet. Do not install it expecting a tool.
>
> What *does* exist and may be worth your time: a measured study of how much of a real dbt project
> can be analysed without a warehouse connection — see
> [ADR 000](docs/adr/000-feasibility.md).

---

## The problem

You rename a column. `dbt build` passes. Three hops downstream a dashboard silently breaks, and you
find out a week later from an angry Slack message.

`dbt ls --select model+` tells you which *models* are downstream. It does not tell you which
*columns* are, so it flags 40 models when 2 actually touch the column you changed. The noise is why
nobody runs it.

whatbreaks answers the narrower question precisely: **change or drop `orders.customer_email` — what
specifically breaks?**

## Why another one of these

Column-level lineage for dbt is not new, and this project does not pretend to invent it. `sqlglot`
provides the parsing; several good tools build on it.

The gap is elsewhere. **Every existing CI impact tool needs infrastructure:**

| Tool | Needs |
| --- | --- |
| [Recce](https://github.com/DataRecce/recce) (Apache-2.0) | live warehouse + two dbt environments |
| [dbt-column-lineage](https://github.com/Fszta/dbt-column-lineage) (MIT) | `catalog.json`, i.e. a warehouse |
| [SQLMesh](https://github.com/TobikoData/sqlmesh) (Apache-2.0) | adopting SQLMesh as your framework |
| dbt Cloud Explorer | dbt Cloud, Enterprise tier |
| Datafold / Atlan / Sifflet / Metaplane | a hosted catalog (SaaS) |

whatbreaks needs **`dbt parse` output and nothing else**. That is the whole differentiation, and it
has one consequence worth stating plainly: it is the only tool of its class that can run on a pull
request **from a fork**, where `GITHUB_TOKEN` is read-only and secrets are unavailable.

If you already run Recce or Datafold happily, they answer a *different and often better* question —
"did the data actually change?" — by querying your warehouse. whatbreaks answers "what could break?"
statically, for free, in seconds.

## Design commitments

- **Never overclaim.** Findings carry a *severity* and an independent *confidence*. Coverage is
  always reported. A clean result is never printed when analysis was partial.
- **A linter, not a catalog.** No server, no database, no web UI, no account.
- **Evidence, not graphs.** Every finding cites a file, a line and an expression. Output should read
  like a compiler error.
- **Deterministic.** No network calls, ever. No LLMs. Same inputs, byte-identical output.

## What Phase 0 measured

Before writing the tool, the central assumption was tested: *can a dbt project be analysed at column
level with no warehouse?* Across 14 public dbt projects (7 produced a usable manifest, 164 models):

| Population | Models | Schema resolved EXACT |
| --- | ---: | ---: |
| Analytics projects (target) | 15 | **93.3%** |
| Libraries / packages (harder) | 149 | **75.2%** |

Two findings shaped the design:

- **`dbt parse` yields compiled SQL for 0% of models**, so offline Jinja rendering is mandatory.
  Compiling the macros that `manifest.json` already contains raises renderability from **34% to
  80%** — that is the difference between viable and not.
- **`SELECT *` prevalence is a misleading metric.** The dominant dbt idiom ends every model in
  `select * from final`, but that star is over a CTE and resolves fine with an empty schema. The
  real uncertainty signal is whether an unresolved star *survives* qualification.

Full method, per-project numbers, and threats to validity: [ADR 000](docs/adr/000-feasibility.md).
The measurement scripts are in [`tools/`](tools/) and are reproducible.

## Limitations

Being explicit about these is a design goal, not an apology. See
[`docs/limitations.md`](docs/limitations.md) once it exists; in the meantime ADR 000 §7 is the
honest account. Known hard limits today:

- Without `catalog.json`, `SELECT *` over a source with no declared columns cannot be expanded.
  Those models are reported `PARTIAL`/`UNKNOWN`, never guessed.
- Introspective macros (`run_query`, `adapter.get_relation`, `load_result`) are unresolvable
  offline and are reported as such rather than rendered to empty string.
- Python models are out of scope.

## Licence

MIT
