<div align="center">

# whatbreaks

**Know what a dbt change breaks — before you merge it.**

Column-level blast radius in CI. No warehouse, no secrets, no backend.

[![CI](https://github.com/Ridadata/whatbreaks/actions/workflows/ci.yml/badge.svg)](https://github.com/Ridadata/whatbreaks/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10–3.13-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status: v0.1.0](https://img.shields.io/badge/status-v0.1.0%20·%20early-orange.svg)](#project-status)

</div>

---

You rename a column. `dbt build` passes. Three hops downstream a dashboard breaks, and you find out
a week later from an angry Slack message.

`dbt ls --select model+` knows which **models** are downstream. It does not know which **columns**
are — so a change to one column flags every model beneath it, whether or not that column reaches
them. The answer is technically correct and too coarse to act on.

whatbreaks answers the narrower question precisely:

```console
$ whatbreaks check --base base/target/manifest.json --head target/manifest.json

BREAKING  WB001  column stg_orders.status was removed
          still referenced by: orders; breaks 1 column, 1 model, 1 test
          confidence: likely
    info  WB900  orders could not be compared
          head schema is partial
          confidence: unknown

analysed 5/5 models (100.0%) - 4 exact, 1 partial, 0 not analysed
      1  qualify_error
note: analysis was incomplete, so absence of findings is not proof of safety
```

Exit code `1`. That is dbt's own `jaffle_shop` with one column deleted — a real break, found without
touching a warehouse.

Note the last three lines. The tool reports what it *could not* work out, so a clean result is never
mistaken for a complete one.

## Why not the existing tools

Column-level lineage for dbt is not new. `sqlglot` does the parsing, and several good tools build on
it. The gap is that **every existing CI impact tool needs infrastructure**:

| Tool | Requires |
| --- | --- |
| [Recce](https://github.com/DataRecce/recce) | live warehouse + two dbt environments |
| [dbt-column-lineage](https://github.com/Fszta/dbt-column-lineage) | `catalog.json`, i.e. a warehouse |
| [SQLMesh](https://github.com/TobikoData/sqlmesh) | adopting SQLMesh as your framework |
| dbt Cloud Explorer | dbt Cloud, Enterprise tier |
| Datafold · Atlan · Sifflet · Metaplane | a hosted catalog (SaaS) |
| **whatbreaks** | **`dbt parse` output. Nothing else.** |

That has one practical consequence: because it needs no credentials, it can run on pull requests
**from forks**, where `GITHUB_TOKEN` is read-only and secrets are withheld.

If you already run Recce or Datafold, they answer a *different and often better* question — "did the
data actually change?" — by querying your warehouse. whatbreaks answers "what could break?"
statically, in seconds, for free.

## Install

> **Not on PyPI yet.** The release is prepared but unpublished; until then, install from source.
> This note disappears the moment `pip install whatbreaks` works.

```console
pip install git+https://github.com/Ridadata/whatbreaks
```

Four runtime dependencies. No warehouse driver, no service, no account.

## Use

You need two manifests: one from the base commit, one from your change. `dbt parse` produces them
offline — no warehouse connection required.

```bash
git worktree add ../base origin/main
(cd ../base && dbt parse)     # base
dbt parse                     # your change

whatbreaks check --base ../base/target/manifest.json --head target/manifest.json
```

| Flag | |
| --- | --- |
| `--fail-on breaking` | default — only confirmed breakage fails the run |
| `--fail-on possibly-breaking` | stricter |
| `--fail-on never` | report only, always exit `0` |
| `--format text \| json \| markdown` | humans · tooling · PR comments |

**Exit codes:** `0` clean · `1` findings at or above the threshold · `2` bad input (nothing analysed).

<details>
<summary>Inspecting what the tool sees</summary>

```console
$ whatbreaks debug graph target/manifest.json --model stg_orders --column order_id --consumers

model.jaffle_shop.customers.number_of_orders  <-  model.jaffle_shop.stg_orders.order_id  [direct, likely]
model.jaffle_shop.orders.order_id  <-  model.jaffle_shop.stg_orders.order_id  [direct, likely]
```

Also `debug schema`, `debug sql` and `debug coverage`. Output shape is not a stable API.

</details>

## How it works

1. Reads two `manifest.json` files — no warehouse, no network.
2. Recovers each model's SQL: dbt-compiled if available, otherwise Jinja rendered against the macros
   the manifest already contains.
3. Infers every model's output columns in one topological pass, tracking what it could *not* work out.
4. Builds column-level lineage with `sqlglot`, including columns needed only for filters and joins —
   those break the query without changing any downstream schema.
5. **Diffs the two graphs, not the two files.** Reformatting and CTE renames produce nothing; a model
   whose columns changed because an *upstream* `SELECT *` changed is caught even though its own file
   was never touched.

## What it reports

| Rule | Change | Severity |
| --- | --- | --- |
| [WB001](docs/rules/WB001.md) | column removed | breaking · possibly · safe, by blast radius |
| [WB002](docs/rules/WB002.md) | model removed | breaking if still referenced |
| [WB003](docs/rules/WB003.md) | column added | safe |
| [WB900](docs/rules/WB900.md) | model could not be analysed | info |

Renames, type changes and expression changes are inference rather than fact. They wait for later
releases instead of shipping as guesses.

## Design commitments

- **Never overclaim.** Findings carry a severity *and* an independent confidence. Coverage is always
  reported. A clean result is never printed without saying how much was analysed.
- **Absence of evidence is not evidence of absence.** A removed column with no consumer is `safe`
  only when coverage is complete — otherwise `possibly_breaking`, because the consumer may be a model
  we could not read.
- **A `SELECT *` consumer is not the same as a named one.** It does not error when a column vanishes;
  it silently produces a narrower result. Different breakage, reported differently.
- **Deterministic.** No network calls, ever — enforced by a test that blocks socket creation. No LLMs.
  Same inputs, byte-identical output.

## How well it works

Measured, not asserted. Across 7 public dbt projects (164 models) with **no warehouse**:

| | |
| --- | ---: |
| Output columns resolved exactly | **75.6%** |
| Models with any usable schema | **83.5%** |
| Analytics-style projects (the target population) | **93.3%** |
| False positives on the [no-op corpus](tests/false_positives/) | **0** |
| 500-model project, cold | **11.5s** |

Method, per-project numbers and threats to validity are in [ADR 000](docs/adr/000-feasibility.md).
The measurement scripts are in [`tools/`](tools/) and reproduce the numbers.

The analytics figure rests on 2 projects and 15 models. It is directionally right and
under-evidenced; the package subset (73.8%, a harder population) is the conservative floor.

## Limitations

Being explicit here is a design goal, not an apology. Full list:
**[docs/limitations.md](docs/limitations.md)**.

- Without `catalog.json`, `SELECT *` over a source with no declared columns cannot be expanded.
  Those models are reported `partial`, never guessed. *(A star over a CTE resolves fine — most do.)*
- Macros needing a live warehouse (`run_query`, `adapter.get_relation`) are unresolvable offline and
  say so by name.
- Python models are out of scope, and are reported as such rather than as a SQL parse error.
- whatbreaks reasons about **schema**, not values. Whether your numbers changed is a different
  question, and Recce and Datafold answer it well.

## Project status

**v0.1.0 — early, but real.** It works, it is tested against real dbt projects, and it is honest
about what it could not analyse.

What is not here yet: the GitHub Action, rename and type-change detection, and suppressions. The JSON
output carries its own `schema_version` so tooling can pin to it while the shape settles.

## Contributing

Found SQL whatbreaks gets wrong? **[A corpus case](tests/false_positives/README.md) is the most
useful possible bug report** — one YAML file, no code. It reproduces the problem, documents the
expectation, and becomes the regression test the moment it is fixed.

A break we *failed* to report is the more valuable half: false negatives mean someone merges a
breaking change believing it is safe. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT
